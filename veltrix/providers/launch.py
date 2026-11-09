"""Cross-chain launch indexing and provenance-aware safety submission.

The adapters intentionally require injected funding-trace resolvers. A token
transfer or a synchronized buy is not enough to claim a common funder.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import time
from typing import Callable, Iterable, Protocol

import requests

from ..launch_bundles import LaunchBundleAnalyzer, LaunchBuyEvidence, LaunchBundleReport
from ..composite_safety import SafetyEvidenceBundle
from ..safety import SafetyEvidence, SafetyReport
from .base import ProviderError


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ERC20_TOTAL_SUPPLY = "0x18160ddd"
ERC20_DECIMALS = "0x313ce567"


@dataclass(frozen=True)
class FundingTrace:
    provider: str
    chain: str
    wallet_address: str
    funder_address: str
    observed_at_ms: int
    evidence_id: str
    transaction_ids: tuple[str, ...]
    coverage_complete: bool


@dataclass(frozen=True)
class LaunchIndexBatch:
    provider: str
    chain: str
    token_address: str
    launch_at_ms: int
    window_end_ms: int
    total_supply: float | None
    supply_source: str | None
    supply_block_ref: str | None
    supply_observed_at_ms: int | None
    observations: tuple[LaunchBuyEvidence, ...]
    coverage_complete: bool
    scanned_through_ms: int
    pages_scanned: int
    rejected: tuple[str, ...]
    provenance: tuple[str, ...]


class LaunchTransactionIndexer(Protocol):
    provider_id: str
    supported_chains: frozenset[str]

    def index_launch(
        self, token_address: str, launch_at_ms: int, window_end_ms: int, **kwargs
    ) -> LaunchIndexBatch: ...


class FundingTraceResolver(Protocol):
    provider_id: str

    def trace_funder(self, chain: str, wallet_address: str, before_ms: int) -> FundingTrace | None: ...


def _evidence_id(*parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()



class HeliusNativeFundingResolver:
    """Trace a Solana buyer's recent inbound native-SOL funding transaction."""

    provider_id = "helius-funder"
    NATIVE_SOL_MINT = "So11111111111111111111111111111111111111111"

    def __init__(self, api_key, *, base_url="https://mainnet.helius-rpc.com",
                 session=None, timeout=20, max_pages=10, lookback_ms=86_400_000,
                 clock_ms=None):
        if not api_key or max_pages < 1 or lookback_ms <= 0:
            raise ValueError("Helius funding resolver requires key and positive bounds")
        self.api_key, self.base_url = api_key, base_url.rstrip("/")
        self.session, self.timeout = session or requests.Session(), timeout
        self.max_pages, self.lookback_ms = max_pages, lookback_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def trace_funder(self, chain, wallet_address, before_ms):
        if chain != "solana":
            return None
        start_ms = max(0, before_ms - self.lookback_ms)
        cursor, pages, incoming, scanned_through = None, 0, [], 0
        reached_end = False
        while pages < self.max_pages:
            body = {
                "address": wallet_address,
                "match": {"mints": [{"mint": self.NATIVE_SOL_MINT}]},
                "tokenAccounts": "balanceChanged",
                "time": {"gte": start_ms // 1000, "lte": before_ms // 1000},
                "limit": 100, "returnShape": "transactions",
            }
            if cursor:
                body["paginationToken"] = cursor
            try:
                response = self.session.post(
                    f"{self.base_url}/v1/parsed-events/scan",
                    params={"api-key": self.api_key}, json=body,
                    headers={"Content-Type": "application/json"}, timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ProviderError("Helius funding trace request failed") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ProviderError("Helius funding trace response malformed")
            scan = payload.get("scan") or {}
            through = (scan.get("scannedThrough") or {}).get("blockTime")
            if isinstance(through, (int, float)):
                scanned_through = max(scanned_through, int(through * 1000))
            for tx in payload["data"]:
                if not isinstance(tx, dict):
                    continue
                ts = tx.get("blockTime", tx.get("timestamp"))
                if not isinstance(ts, (int, float)):
                    continue
                observed = int(ts * 1000)
                if not start_ms <= observed <= before_ms:
                    continue
                for transfer in tx.get("nativeTransfers", []):
                    if (isinstance(transfer, dict)
                            and transfer.get("toUserAccount") == wallet_address
                            and isinstance(transfer.get("fromUserAccount"), str)
                            and transfer.get("fromUserAccount") != wallet_address):
                        incoming.append((observed, transfer["fromUserAccount"], tx.get("signature")))
            pages += 1
            cursor = payload.get("paginationToken")
            if not cursor:
                reached_end = scan.get("stoppedOn") == "windowEnd" and scanned_through >= before_ms
                break
        if not reached_end or not incoming:
            return None
        observed, funder, signature = max(incoming, key=lambda row: row[0])
        if not isinstance(signature, str) or not signature:
            return None
        return FundingTrace(
            provider=self.provider_id, chain="solana", wallet_address=wallet_address,
            funder_address=funder, observed_at_ms=observed,
            evidence_id=_evidence_id(self.provider_id, wallet_address, funder, signature, start_ms),
            transaction_ids=(signature,), coverage_complete=True,
        )


class EvmQuoteFundingResolver:
    """Trace direct inbound transfers of configured quote assets on Base or BSC.

    This does not claim to cover native-coin funding or unconfigured ERC-20 assets.
    """

    def __init__(self, chain, rpc_url, quote_token_addresses, *,
                 session=None, timeout=20, block_chunk_size=2_000,
                 lookback_blocks=20_000, clock_ms=None):
        if chain not in {"base", "bsc"} or not rpc_url:
            raise ValueError("EVM funding resolver requires Base/BSC RPC URL")
        self.provider_id = f"{chain}-quote-funder"
        self.chain, self.rpc_url = chain, rpc_url
        self.quote_tokens = tuple(x.lower() for x in quote_token_addresses)
        self.session, self.timeout = session or requests.Session(), timeout
        self.block_chunk_size, self.lookback_blocks = block_chunk_size, lookback_blocks
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._request_id = 0

    def _rpc(self, method, params):
        self._request_id += 1
        try:
            response = self.session.post(
                self.rpc_url, json={"jsonrpc":"2.0","id":self._request_id,
                                    "method":method,"params":params},
                headers={"Content-Type":"application/json"}, timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"{self.chain} funding RPC failed: {method}") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"{self.chain} funding RPC returned invalid {method}")
        return payload["result"]

    def trace_funder(self, chain, wallet_address, before_ms):
        if chain != self.chain or not self.quote_tokens:
            return None
        latest = self._rpc("eth_blockNumber", [])
        end_block = int(latest, 16)
        start_block = max(0, end_block - self.lookback_blocks)
        wallet_topic = "0x" + wallet_address.lower().removeprefix("0x").rjust(64, "0")
        found, rejected = [], False
        for start in range(start_block, end_block + 1, self.block_chunk_size):
            end = min(end_block, start + self.block_chunk_size - 1)
            try:
                logs = self._rpc("eth_getLogs", [{
                    "address": list(self.quote_tokens), "fromBlock": hex(start),
                    "toBlock": hex(end), "topics": [TRANSFER_TOPIC, None, wallet_topic],
                }])
            except ProviderError:
                rejected = True
                break
            if not isinstance(logs, list):
                rejected = True
                break
            for log in logs:
                try:
                    funder = "0x" + log["topics"][1][-40:]
                    txid = log["transactionHash"]
                    block_number = int(log["blockNumber"], 16)
                    block = self._rpc("eth_getBlockByNumber", [hex(block_number), False])
                    observed = int(block["timestamp"], 16) * 1000
                    amount = int(log["data"], 16)
                except (KeyError, TypeError, ValueError):
                    rejected = True
                    continue
                if observed <= before_ms and amount > 0 and funder.lower() != wallet_address.lower():
                    found.append((observed, funder, txid))
        if rejected or not found:
            return None
        observed, funder, txid = max(found, key=lambda row: row[0])
        return FundingTrace(
            provider=self.provider_id, chain=chain, wallet_address=wallet_address,
            funder_address=funder, observed_at_ms=observed,
            evidence_id=_evidence_id(self.provider_id, wallet_address, funder, txid,
                                     start_block, end_block, self.quote_tokens),
            transaction_ids=(txid,), coverage_complete=True,
        )


class HeliusLaunchIndexer:
    """Helius Parsed Events Scan adapter for Solana launch-window swaps."""

    provider_id = "helius-launch"
    supported_chains = frozenset({"solana"})
    NATIVE_SOL_MINT = "So11111111111111111111111111111111111111111"

    def __init__(
        self,
        api_key: str,
        funding_resolver: FundingTraceResolver | None,
        supply_resolver: Callable[[str, int], tuple[float, str, str]] | None,
        *,
        base_url: str = "https://mainnet.helius-rpc.com",
        session=None,
        timeout: float = 25,
        limit: int = 100,
        max_pages: int = 20,
        clock_ms=None,
    ):
        if not api_key:
            raise ValueError("Helius API key is required")
        if not 1 <= limit <= 100 or max_pages < 1:
            raise ValueError("Invalid Helius scan limits")
        self.api_key = api_key
        self.funding_resolver = funding_resolver
        self.supply_resolver = supply_resolver
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.limit = limit
        self.max_pages = max_pages
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _scan_page(self, token: str, start_ms: int, end_ms: int, cursor: str | None):
        body = {
            "address": token,
            "match": {"mints": [{"mint": token, "scope": "transaction"}]},
            "time": {"gte": start_ms // 1000, "lte": end_ms // 1000},
            "limit": self.limit,
            "returnShape": "transactions",
        }
        if cursor:
            body["paginationToken"] = cursor
        try:
            response = self.session.post(
                f"{self.base_url}/v1/parsed-events/scan",
                params={"api-key": self.api_key}, json=body,
                headers={"Content-Type": "application/json"}, timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError("Helius launch scan request failed") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError("Helius launch scan returned an invalid response")
        scan = payload.get("scan")
        if not isinstance(scan, dict):
            raise ProviderError("Helius launch scan omitted coverage summary")
        return payload, scan

    def index_launch(self, token_address, launch_at_ms, window_end_ms, **kwargs):
        if not isinstance(token_address, str) or not token_address:
            raise ValueError("Solana token mint is required")
        if not 0 <= launch_at_ms < window_end_ms <= self.clock_ms():
            raise ValueError("Invalid or future launch window")
        observations, rejected, provenance = [], [], []
        cursor = None
        pages = 0
        scanned_through = 0
        reached_window_end = False
        while pages < self.max_pages:
            payload, scan = self._scan_page(token_address, launch_at_ms, window_end_ms, cursor)
            pages += 1
            scanned = scan.get("scannedThrough") or {}
            block_time = scanned.get("blockTime")
            if isinstance(block_time, (int, float)) and not isinstance(block_time, bool):
                scanned_through = max(scanned_through, int(block_time * 1000))
            provenance.append(
                f"helius-scan:{scan.get('stoppedOn', 'unknown')}:{scan.get('scannedTransactions', 0)}"
            )
            for tx in payload["data"]:
                self._extract_swap(tx, token_address, launch_at_ms, window_end_ms,
                                   observations, rejected)
            cursor = payload.get("paginationToken")
            if not cursor:
                reached_window_end = (
                    scan.get("stoppedOn") == "windowEnd"
                    and scanned_through >= window_end_ms
                )
                break
        if cursor:
            rejected.append("Helius pagination limit reached before launch window was exhausted")
        if not reached_window_end:
            rejected.append("Helius scan did not prove complete launch-window coverage")

        total_supply = supply_source = supply_ref = supply_observed = None
        if self.supply_resolver is not None:
            try:
                total_supply, supply_source, supply_ref = self.supply_resolver(
                    token_address, launch_at_ms
                )
                supply_observed = self.clock_ms()
                if not math.isfinite(float(total_supply)) or float(total_supply) <= 0:
                    raise ValueError("invalid historical supply")
            except Exception:
                total_supply = supply_source = supply_ref = supply_observed = None
                rejected.append("historical Solana mint supply could not be verified")
        else:
            rejected.append("historical Solana mint supply resolver is not configured")

        coverage = reached_window_end and not rejected and total_supply is not None
        return LaunchIndexBatch(
            provider=self.provider_id, chain="solana", token_address=token_address,
            launch_at_ms=launch_at_ms, window_end_ms=window_end_ms,
            total_supply=float(total_supply) if total_supply is not None else None,
            supply_source=supply_source, supply_block_ref=supply_ref,
            supply_observed_at_ms=supply_observed,
            observations=tuple(observations), coverage_complete=coverage,
            scanned_through_ms=scanned_through, pages_scanned=pages,
            rejected=tuple(rejected), provenance=tuple(provenance),
        )

    def _extract_swap(self, tx, token, launch_ms, end_ms, output, rejected):
        if not isinstance(tx, dict):
            rejected.append("malformed Helius transaction record")
            return
        summary = tx.get("summary") or {}
        if not isinstance(summary, dict) or str(summary.get("type", "")).lower() != "swap":
            return
        signature = tx.get("signature")
        timestamp = tx.get("blockTime", tx.get("timestamp"))
        if not isinstance(signature, str) or not isinstance(timestamp, (int, float)):
            rejected.append("Helius swap missing signature or block time")
            return
        observed_ms = int(timestamp * 1000)
        if not launch_ms <= observed_ms <= end_ms:
            rejected.append("Helius swap timestamp outside requested launch window")
            return
        transfers = tx.get("tokenTransfers")
        if not isinstance(transfers, list):
            rejected.append("Helius swap omitted token transfer details")
            return
        for transfer in transfers:
            if not isinstance(transfer, dict) or transfer.get("mint") != token:
                continue
            wallet = transfer.get("toUserAccount")
            if not isinstance(wallet, str) or not wallet or wallet == transfer.get("fromUserAccount"):
                continue
            try:
                amount = float(transfer.get("tokenAmount"))
            except (TypeError, ValueError):
                rejected.append("Helius launch swap has invalid token amount")
                continue
            if not math.isfinite(amount) or amount <= 0:
                rejected.append("Helius launch swap has non-positive token amount")
                continue
            trace = None
            if self.funding_resolver is not None:
                try:
                    trace = self.funding_resolver.trace_funder("solana", wallet, observed_ms)
                except Exception:
                    trace = None
            output.append(LaunchBuyEvidence(
                provider=self.provider_id, chain="solana", token_address=token,
                wallet_address=wallet,
                funder_address=trace.funder_address if trace and trace.coverage_complete else "",
                transaction_id=signature, token_amount=amount, observed_at_ms=observed_ms,
                funder_provider=trace.provider if trace else "",
                funder_evidence_id=trace.evidence_id if trace else "",
            ))


class EvmRpcLaunchIndexer:
    """Base/BSC ERC-20 launch indexer using eth_getLogs and transaction receipts.

    Caller supplies the launch and window-end block bounds. The indexer obtains
    totalSupply/decimals at the launch block, verifies swap receipts against
    allowlisted quote-token transfers, and requires an independent funding tracer.
    """

    supported_chains = frozenset({"base", "bsc"})

    def __init__(
        self,
        chain: str,
        rpc_url: str,
        funding_resolver: FundingTraceResolver | None,
        *,
        quote_token_addresses: Iterable[str],
        session=None,
        timeout: float = 25,
        block_chunk_size: int = 2_000,
        clock_ms=None,
    ):
        chain = chain.lower()
        if chain not in self.supported_chains:
            raise ValueError("EVM launch indexer supports Base or BSC")
        if not rpc_url:
            raise ValueError(f"{chain} RPC URL is required")
        if block_chunk_size < 1:
            raise ValueError("block_chunk_size must be positive")
        self.chain = chain
        self.provider_id = f"{chain}-rpc-launch"
        self.rpc_url = rpc_url
        self.funding_resolver = funding_resolver
        self.quote_tokens = frozenset(x.lower() for x in quote_token_addresses)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.block_chunk_size = block_chunk_size
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._request_id = 0
        self._block_cache = {}

    def _rpc(self, method, params):
        self._request_id += 1
        try:
            response = self.session.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": self._request_id,
                      "method": method, "params": params},
                headers={"Content-Type": "application/json"}, timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"{self.chain} JSON-RPC request failed: {method}") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"{self.chain} JSON-RPC returned an invalid {method} result")
        return payload["result"]

    def _block(self, number):
        if number not in self._block_cache:
            block = self._rpc("eth_getBlockByNumber", [hex(number), False])
            if not isinstance(block, dict) or not isinstance(block.get("timestamp"), str):
                raise ProviderError("EVM block timestamp unavailable")
            self._block_cache[number] = block
        return self._block_cache[number]

    def _historical_supply(self, token, launch_block):
        supply_raw = self._rpc("eth_call", [
            {"to": token, "data": ERC20_TOTAL_SUPPLY}, hex(launch_block)
        ])
        decimals_raw = self._rpc("eth_call", [
            {"to": token, "data": ERC20_DECIMALS}, hex(launch_block)
        ])
        if not isinstance(supply_raw, str) or not supply_raw.startswith("0x"):
            raise ProviderError("ERC-20 totalSupply unavailable at launch block")
        if not isinstance(decimals_raw, str) or not decimals_raw.startswith("0x"):
            raise ProviderError("ERC-20 decimals unavailable at launch block")
        raw_supply, decimals = int(supply_raw, 16), int(decimals_raw, 16)
        if raw_supply <= 0 or not 0 <= decimals <= 36:
            raise ProviderError("Invalid historical ERC-20 supply or decimals")
        return raw_supply / (10 ** decimals), decimals

    def index_launch(self, token_address, launch_at_ms, window_end_ms, *,
                     launch_block: int, window_end_block: int, **kwargs):
        if not isinstance(token_address, str) or not token_address.startswith("0x"):
            raise ValueError("EVM token contract address is required")
        if not (isinstance(launch_block, int) and isinstance(window_end_block, int)
                and 0 <= launch_block <= window_end_block):
            raise ValueError("Valid launch and window-end block bounds are required")
        now = self.clock_ms()
        if not 0 <= launch_at_ms < window_end_ms <= now:
            raise ValueError("Invalid or future launch window")
        token = token_address.lower()
        launch_block_data = self._block(launch_block)
        end_block_data = self._block(window_end_block)
        actual_launch_ms = int(launch_block_data["timestamp"], 16) * 1000
        actual_end_ms = int(end_block_data["timestamp"], 16) * 1000
        if actual_launch_ms > launch_at_ms or actual_end_ms < window_end_ms:
            raise ProviderError("Supplied EVM block bounds do not cover requested launch window")
        total_supply, decimals = self._historical_supply(token, launch_block)
        logs = []
        rejected = []
        for start in range(launch_block, window_end_block + 1, self.block_chunk_size):
            end = min(window_end_block, start + self.block_chunk_size - 1)
            try:
                page = self._rpc("eth_getLogs", [{
                    "address": token,
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "topics": [TRANSFER_TOPIC],
                }])
            except ProviderError:
                rejected.append(f"eth_getLogs failed for block range {start}-{end}")
                continue
            if not isinstance(page, list):
                rejected.append(f"eth_getLogs returned malformed block range {start}-{end}")
                continue
            logs.extend(page)

        observations = []
        seen_tx = set()
        for log in logs:
            try:
                tx_hash = log["transactionHash"]
                block_number = int(log["blockNumber"], 16)
                topics = log["topics"]
                raw_amount = int(log["data"], 16)
                sender = "0x" + topics[1][-40:]
                buyer = "0x" + topics[2][-40:]
            except (KeyError, TypeError, ValueError, IndexError):
                rejected.append("malformed ERC-20 Transfer log")
                continue
            if buyer.lower() == ZERO_ADDRESS or sender.lower() == ZERO_ADDRESS:
                continue
            block = self._block(block_number)
            observed_ms = int(block["timestamp"], 16) * 1000
            if not launch_at_ms <= observed_ms <= window_end_ms:
                continue
            try:
                receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
                tx = self._rpc("eth_getTransactionByHash", [tx_hash])
            except ProviderError:
                rejected.append(f"transaction receipt unavailable for {tx_hash}")
                continue
            if not isinstance(receipt, dict) or not isinstance(tx, dict):
                rejected.append(f"transaction details unavailable for {tx_hash}")
                continue
            if not self._is_paid_buy(receipt, tx, buyer):
                continue
            key = tx_hash.lower()
            if key in seen_tx:
                continue
            seen_tx.add(key)
            amount = raw_amount / (10 ** decimals)
            trace = None
            if self.funding_resolver is not None:
                try:
                    trace = self.funding_resolver.trace_funder(self.chain, buyer, observed_ms)
                except Exception:
                    trace = None
            observations.append(LaunchBuyEvidence(
                provider=self.provider_id, chain=self.chain, token_address=token,
                wallet_address=buyer,
                funder_address=trace.funder_address if trace and trace.coverage_complete else "",
                transaction_id=tx_hash, token_amount=amount, observed_at_ms=observed_ms,
                funder_provider=trace.provider if trace else "",
                funder_evidence_id=trace.evidence_id if trace else "",
            ))

        supply_observed = self.clock_ms()
        coverage = not rejected and actual_end_ms >= window_end_ms
        if self.funding_resolver is None:
            rejected.append("independent EVM funding-trace provider is not configured")
            coverage = False
        return LaunchIndexBatch(
            provider=self.provider_id, chain=self.chain, token_address=token,
            launch_at_ms=launch_at_ms, window_end_ms=window_end_ms,
            total_supply=total_supply, supply_source=self.provider_id,
            supply_block_ref=hex(launch_block), supply_observed_at_ms=supply_observed,
            observations=tuple(observations), coverage_complete=coverage,
            scanned_through_ms=actual_end_ms, pages_scanned=math.ceil(
                (window_end_block - launch_block + 1) / self.block_chunk_size
            ), rejected=tuple(rejected),
            provenance=(f"eth_getLogs:{hex(launch_block)}-{hex(window_end_block)}",
                        f"eth_call:totalSupply@{hex(launch_block)}",
                        f"erc20_decimals:{decimals}"),
        )

    def _is_paid_buy(self, receipt, tx, buyer):
        buyer = buyer.lower()
        if str(tx.get("from", "")).lower() != buyer:
            return False
        try:
            native_value = int(tx.get("value", "0x0"), 16)
        except (TypeError, ValueError):
            native_value = 0
        if native_value > 0:
            return True
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            return False
        for log in logs:
            try:
                if str(log.get("address", "")).lower() not in self.quote_tokens:
                    continue
                topics = log.get("topics") or []
                if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                    continue
                sender = "0x" + topics[1][-40:]
                raw_amount = int(log.get("data", "0x0"), 16)
            except (TypeError, ValueError, IndexError):
                continue
            if sender.lower() == buyer and raw_amount > 0:
                return True
        return False


class LaunchIntelligenceService:
    """Index launch activity, calculate bundle supply, and submit to Sniper gate."""

    def __init__(
        self,
        indexers: Iterable[LaunchTransactionIndexer],
        analyzer: LaunchBundleAnalyzer,
        intelligence_adapters,
        trusted_launch_providers: Iterable[str],
        trusted_funding_providers: Iterable[str],
        launch_window_ms: int = 120_000,
    ):
        self.indexers = {chain: provider for provider in indexers
                         for chain in provider.supported_chains}
        self.analyzer = analyzer
        self.adapters = intelligence_adapters
        self.trusted_launch = frozenset(trusted_launch_providers)
        self.trusted_funding = frozenset(trusted_funding_providers)
        if launch_window_ms <= 0:
            raise ValueError("launch_window_ms must be positive")
        self.launch_window_ms = launch_window_ms

    def evaluate(
        self, *, chain: str, token_address: str, launch_at_ms: int, now_ms: int,
        liquidity_evidence: SafetyEvidence | None,
        developer_evidence: SafetyEvidence | None,
        indexer_kwargs: dict | None = None,
    ) -> tuple[LaunchBundleReport, SafetyReport]:
        chain = chain.lower()
        indexer = self.indexers.get(chain)
        if indexer is None:
            raise ProviderError(f"No launch indexer configured for {chain}")
        batch = indexer.index_launch(
            token_address, launch_at_ms, launch_at_ms + self.launch_window_ms,
            **(indexer_kwargs or {}),
        )
        rejected = list(batch.rejected)
        if batch.provider not in self.trusted_launch:
            rejected.append("launch indexer is not explicitly trusted")
        token_matches = (
            batch.token_address.lower() == token_address.lower()
            if chain in {"base", "bsc"} else batch.token_address == token_address
        )
        if batch.chain != chain or not token_matches:
            rejected.append("launch indexer returned wrong chain or token")
        if batch.scanned_through_ms < batch.window_end_ms:
            rejected.append("indexer did not reach launch-window end")
        if batch.supply_source != batch.provider or not batch.supply_block_ref:
            rejected.append("launch-time supply provenance is missing")
        if (not isinstance(batch.supply_observed_at_ms, int)
                or batch.supply_observed_at_ms > now_ms
                or now_ms - batch.supply_observed_at_ms > 300_000):
            rejected.append("launch-time supply evidence is stale or invalid")
        for item in batch.observations:
            if item.funder_provider not in self.trusted_funding:
                rejected.append(f"funding trace for {item.wallet_address} is not trusted")
            if not item.funder_evidence_id or not item.funder_address:
                rejected.append(f"funding trace for {item.wallet_address} is incomplete")

        effective_coverage = batch.coverage_complete and not rejected
        if not batch.observations and not self.trusted_funding:
            rejected.append("no trusted funding tracer configured for a zero-buy launch window")
            effective_coverage = False
        report = self.analyzer.analyze(
            provider=batch.provider, chain=chain, token_address=token_address,
            launch_at_ms=launch_at_ms, total_supply=batch.total_supply,
            observations=batch.observations, coverage_complete=effective_coverage,
            now_ms=now_ms,
        )
        report = LaunchBundleReport(
            **{
                **report.__dict__,
                "evidence_id": _evidence_id(
                    batch.provider, chain, token_address, batch.launch_at_ms,
                    batch.window_end_ms, batch.scanned_through_ms,
                    tuple(x.transaction_id for x in batch.observations),
                    batch.supply_block_ref, batch.provenance,
                ),
                "observed_at_ms": now_ms,
                "scanned_through_ms": batch.scanned_through_ms,
                "supply_source": batch.supply_source or "",
                "supply_block_ref": batch.supply_block_ref or "",
                "supply_observed_at_ms": batch.supply_observed_at_ms or 0,
                "pages_scanned": batch.pages_scanned,
                "funder_providers": tuple(sorted({
                    x.funder_provider for x in batch.observations if x.funder_provider
                })),
                "provenance": tuple(batch.provenance),
                "rejected": tuple(report.rejected) + tuple(rejected),
                "coverage_complete": effective_coverage and report.coverage_complete,
                "bundled_supply_pct": (
                    report.bundled_supply_pct if effective_coverage and not rejected else None
                ),
            }
        )
        evidence_bundle = SafetyEvidenceBundle(
            liquidity=liquidity_evidence,
            developer_holdings=developer_evidence,
            launch_bundle=report,
        )
        safety = self.adapters.validate_composite_safety(
            token_address, chain, evidence_bundle, now_ms
        )
        return report, safety


def build_launch_intelligence_service(
    settings, intelligence_adapters, *,
    solana_supply_resolver=None, funding_resolvers=None,
):
    """Build only providers whose credentials/endpoints are explicitly configured.

    Solana historical supply remains an injected resolver because a current
    getTokenSupply response cannot certify supply at a past launch slot.
    """
    funding_resolvers = funding_resolvers or {}
    indexers = []

    if settings.helius_api_key:
        sol_funder = funding_resolvers.get("solana") or HeliusNativeFundingResolver(
            settings.helius_api_key,
        )
        indexers.append(HeliusLaunchIndexer(
            settings.helius_api_key, sol_funder, solana_supply_resolver,
            max_pages=settings.launch_index_max_pages,
        ))

    for chain, rpc_url, quote_tokens in (
        ("base", settings.base_rpc_url, settings.alchemy_quote_tokens_base),
        ("bsc", settings.bsc_rpc_url, settings.alchemy_quote_tokens_bsc),
    ):
        if not rpc_url:
            continue
        funder = funding_resolvers.get(chain) or EvmQuoteFundingResolver(
            chain, rpc_url, quote_tokens,
            block_chunk_size=settings.launch_block_chunk_size,
            lookback_blocks=settings.funding_lookback_blocks,
        )
        indexers.append(EvmRpcLaunchIndexer(
            chain, rpc_url, funder, quote_token_addresses=quote_tokens,
            block_chunk_size=settings.launch_block_chunk_size,
        ))

    analyzer = LaunchBundleAnalyzer(
        trusted_providers=settings.trusted_bundle_providers,
        launch_window_ms=settings.launch_window_ms,
        trusted_funder_providers=settings.trusted_funding_providers,
    )
    return LaunchIntelligenceService(
        indexers=indexers, analyzer=analyzer,
        intelligence_adapters=intelligence_adapters,
        trusted_launch_providers=settings.trusted_bundle_providers,
        trusted_funding_providers=settings.trusted_funding_providers,
        launch_window_ms=settings.launch_window_ms,
    )
