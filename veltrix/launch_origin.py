"""Chain-specific launch-origin resolution with bounded, auditable scans.

An origin is accepted only when the mint initialization or first deployed code
can be located and the entire configured launch window has elapsed on-chain.
"""
from dataclasses import dataclass
import time

import requests

from .providers.base import ProviderError


@dataclass(frozen=True)
class LaunchOrigin:
    chain: str
    token_address: str
    launch_at_ms: int
    launch_block: int | None
    window_end_block: int | None
    window_end_ms: int
    provider: str
    evidence_id: str
    complete: bool


class _JsonRpc:
    def __init__(self, url, *, session=None, timeout=20):
        if not url:
            raise ValueError("RPC URL is required")
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.request_id = 0

    def call(self, method, params):
        self.request_id += 1
        try:
            response = self.session.post(
                self.url,
                json={"jsonrpc": "2.0", "id": self.request_id,
                      "method": method, "params": params},
                headers={"Content-Type": "application/json"}, timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"launch-origin RPC request failed: {method}") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"launch-origin RPC returned invalid {method}")
        return payload["result"]


class SolanaLaunchOriginResolver:
    """Find the oldest mint-address transaction and verify mint initialization."""

    provider_id = "helius-launch-origin"
    chain = "solana"

    def __init__(self, api_key, *, session=None, base_url="https://mainnet.helius-rpc.com",
                 timeout=20, page_size=1000, max_pages=20):
        if not api_key or not 1 <= page_size <= 1000 or max_pages < 1:
            raise ValueError("Helius launch-origin resolver requires bounded pagination")
        self.url = f"{base_url.rstrip('/')}/?api-key={api_key}"
        self.session = session or requests.Session()
        self.timeout, self.page_size, self.max_pages = timeout, page_size, max_pages
        self.request_id = 0

    def _rpc(self, method, params):
        self.request_id += 1
        try:
            response = self.session.post(
                self.url, json={"jsonrpc": "2.0", "id": self.request_id,
                                "method": method, "params": params},
                headers={"Content-Type": "application/json"}, timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Helius launch-origin request failed: {method}") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"Helius launch-origin returned invalid {method}")
        return payload["result"]

    def resolve(self, token_address, launch_window_ms, *, now_ms=None):
        if not isinstance(token_address, str) or not token_address:
            raise ValueError("Solana mint address is required")
        if launch_window_ms <= 0:
            raise ValueError("launch window must be positive")
        cursor, pages, oldest_page, complete = None, 0, None, False
        while pages < self.max_pages:
            config = {"limit": self.page_size}
            if cursor:
                config["before"] = cursor
            rows = self._rpc("getSignaturesForAddress", [token_address, config])
            if not isinstance(rows, list):
                raise ProviderError("Helius signature history has invalid shape")
            pages += 1
            if not rows:
                complete = True
                break
            oldest_page = rows
            cursor = rows[-1].get("signature") if isinstance(rows[-1], dict) else None
            if not cursor:
                raise ProviderError("Helius signature history contains malformed row")
            if len(rows) < self.page_size:
                complete = True
                break
        if not complete or not oldest_page:
            raise ProviderError("Solana mint origin exceeds bounded history; origin withheld")
        oldest = oldest_page[-1]
        signature = oldest.get("signature")
        slot = oldest.get("slot")
        block_time = oldest.get("blockTime")
        if not isinstance(signature, str) or not isinstance(slot, int) or not isinstance(block_time, int):
            raise ProviderError("Solana oldest mint transaction lacks slot or block time")
        tx = self._rpc("getTransaction", [signature, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
        }])
        if not isinstance(tx, dict):
            raise ProviderError("Solana mint-origin transaction unavailable")
        message = ((tx.get("transaction") or {}).get("message") or {})
        instructions = message.get("instructions", [])
        initialized = any(
            isinstance(instruction, dict)
            and instruction.get("program") in {"spl-token", "spl-token-2022"}
            and instruction.get("parsed", {}).get("type") in {"initializeMint", "initializeMint2"}
            and instruction.get("parsed", {}).get("info", {}).get("mint") == token_address
            for instruction in instructions
        )
        if not initialized:
            raise ProviderError("Oldest mint-address transaction does not prove mint initialization")
        launch_ms = block_time * 1000
        end_ms = launch_ms + launch_window_ms
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if end_ms > now_ms:
            raise ProviderError("Solana launch window has not elapsed")
        import hashlib
        evidence = hashlib.sha256(
            f"{self.provider_id}|{token_address}|{signature}|{slot}|{launch_ms}|{pages}".encode()
        ).hexdigest()
        return LaunchOrigin(
            chain=self.chain, token_address=token_address, launch_at_ms=launch_ms,
            launch_block=slot, window_end_block=None, window_end_ms=end_ms,
            provider=self.provider_id, evidence_id=evidence, complete=True,
        )


class EvmLaunchOriginResolver:
    """Locate first non-empty contract-code block and the elapsed window-end block.

    Uses binary search over historical eth_getCode/eth_getBlockByNumber. This
    supports ordinary persistent contracts; destroyed/redeployed contracts and
    chains that prune historical state are rejected rather than guessed.
    """

    def __init__(self, chain, rpc_url, *, session=None, timeout=20):
        if chain not in {"base", "bsc"}:
            raise ValueError("EVM launch-origin resolver supports Base and BSC")
        self.chain = chain
        self.provider_id = f"{chain}-rpc-launch-origin"
        self.rpc = _JsonRpc(rpc_url, session=session, timeout=timeout)

    def _block(self, number):
        result = self.rpc.call("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(result, dict) or not isinstance(result.get("timestamp"), str):
            raise ProviderError("EVM launch-origin block timestamp unavailable")
        return int(result["timestamp"], 16) * 1000

    def _has_code(self, token, number):
        code = self.rpc.call("eth_getCode", [token, hex(number)])
        if not isinstance(code, str):
            raise ProviderError("EVM historical contract code response malformed")
        return code not in {"0x", "0x0", ""}

    def _first_true(self, low, high, predicate):
        while low < high:
            mid = (low + high) // 2
            if predicate(mid):
                high = mid
            else:
                low = mid + 1
        return low

    def resolve(self, token_address, launch_window_ms, *, now_ms=None):
        if not isinstance(token_address, str) or not token_address.startswith("0x") or len(token_address) != 42:
            raise ValueError("EVM token contract address is invalid")
        if launch_window_ms <= 0:
            raise ValueError("launch window must be positive")
        latest = self.rpc.call("eth_blockNumber", [])
        if not isinstance(latest, str):
            raise ProviderError("EVM latest block number unavailable")
        head = int(latest, 16)
        if not self._has_code(token_address, head):
            raise ProviderError("No deployed contract code at candidate address")
        if self._has_code(token_address, 0):
            launch_block = 0
        else:
            launch_block = self._first_true(0, head, lambda block: self._has_code(token_address, block))
        launch_ms = self._block(launch_block)
        end_ms = launch_ms + launch_window_ms
        head_ms = self._block(head)
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if end_ms > min(now_ms, head_ms):
            raise ProviderError("EVM launch window has not elapsed on-chain")
        end_block = self._first_true(
            launch_block, head,
            lambda block: self._block(block) >= end_ms,
        )
        import hashlib
        evidence = hashlib.sha256(
            f"{self.provider_id}|{token_address.lower()}|{launch_block}|{end_block}|{launch_ms}|{end_ms}".encode()
        ).hexdigest()
        return LaunchOrigin(
            chain=self.chain, token_address=token_address.lower(),
            launch_at_ms=launch_ms, launch_block=launch_block,
            window_end_block=end_block, window_end_ms=end_ms,
            provider=self.provider_id, evidence_id=evidence, complete=True,
        )


def build_launch_origin_resolvers(settings):
    resolvers = {}
    if settings.helius_api_key:
        resolvers["solana"] = SolanaLaunchOriginResolver(settings.helius_api_key)
    if settings.base_rpc_url:
        resolvers["base"] = EvmLaunchOriginResolver("base", settings.base_rpc_url)
    if settings.bsc_rpc_url:
        resolvers["bsc"] = EvmLaunchOriginResolver("bsc", settings.bsc_rpc_url)
    return resolvers
