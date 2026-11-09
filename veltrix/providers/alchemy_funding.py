"""Read-only EVM first-funder graph adapter using Alchemy Transfers API.

Base is supported because Alchemy documents external, internal and ERC-20
transfer categories there. BSC is intentionally excluded: its documented
coverage does not include internal transfers, so it cannot certify a complete
first-funder graph for arbitrary smart-contract wallets.
"""
import hashlib
import json
import time

import requests

from ..developer_concentration import FundingGraphSnapshot, FundingRelationship
from .base import ProviderError


class AlchemyBaseFundingGraphProvider:
    provider_id = "alchemy-base-funding-graph"
    supported_chains = frozenset({"base"})

    def __init__(self, api_key, *, session=None, timeout=20.0, clock_ms=None,
                 max_pages=100, max_wallets=2_000, page_size=1_000,
                 base_url="https://base-mainnet.g.alchemy.com/v2"):
        if not api_key:
            raise ValueError("Alchemy API key is required")
        if not 1 <= max_pages <= 1_000 or not 1 <= max_wallets <= 10_000:
            raise ValueError("Invalid funding graph limits")
        if not 1 <= page_size <= 1_000:
            raise ValueError("Alchemy page size must be between 1 and 1000")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_pages = max_pages
        self.max_wallets = max_wallets
        self.page_size = page_size
        self.endpoint = f"{base_url.rstrip('/')}/{api_key}"
        self._request_id = 0

    def _rpc(self, method, params):
        self._request_id += 1
        try:
            response = self.session.post(
                self.endpoint,
                json={"jsonrpc": "2.0", "id": self._request_id,
                      "method": method, "params": params},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Alchemy {method} request failed") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"Alchemy {method} returned an RPC error")
        return payload["result"]

    def _first_funder(self, wallet, end_block, now_ms):
        cursor = None
        first = None
        seen = set()
        previous_order = None
        for _ in range(self.max_pages):
            query = {
                "fromBlock": "0x0",
                "toBlock": hex(end_block),
                "toAddress": wallet,
                "category": ["external", "internal", "erc20"],
                "excludeZeroValue": True,
                "withMetadata": True,
                "order": "asc",
                "maxCount": hex(self.page_size),
            }
            if cursor:
                query["pageKey"] = cursor
            result = self._rpc("alchemy_getAssetTransfers", [query])
            if not isinstance(result, dict) or not isinstance(result.get("transfers"), list):
                raise ProviderError("Alchemy funding history response is malformed")
            for row in result["transfers"]:
                if not isinstance(row, dict):
                    raise ProviderError("Alchemy funding transfer row is malformed")
                sender = row.get("from")
                recipient = row.get("to")
                tx_hash = row.get("hash")
                block_num = row.get("blockNum")
                category = row.get("category")
                unique_id = row.get("uniqueId")
                if (not isinstance(sender, str) or not sender
                        or not isinstance(recipient, str) or recipient.lower() != wallet.lower()
                        or not isinstance(tx_hash, str) or not tx_hash
                        or not isinstance(unique_id, str) or not unique_id
                        or category not in {"external", "internal", "erc20"}
                        or not isinstance(block_num, str) or not block_num.startswith("0x")):
                    raise ProviderError("Alchemy funding transfer lacks auditable fields")
                try:
                    order = int(block_num, 16)
                except ValueError as exc:
                    raise ProviderError("Alchemy funding block number is malformed") from exc
                if order > end_block or (previous_order is not None and order < previous_order):
                    raise ProviderError("Alchemy funding history is out of order or beyond snapshot")
                previous_order = order
                if unique_id in seen:
                    raise ProviderError("Alchemy funding history contains duplicate transfers")
                seen.add(unique_id)
                if sender.lower() == "0x0000000000000000000000000000000000000000":
                    continue
                if first is None:
                    first = (sender, tx_hash)
            cursor = result.get("pageKey")
            if not cursor:
                return first
        if cursor:
            raise ProviderError("Alchemy funding history exceeded pagination bound")
        return first

    def fetch_snapshot(self, token_address, holder_wallets):
        if not isinstance(token_address, str) or not token_address:
            raise ValueError("Token address is required")
        wallets = tuple(dict.fromkeys(
            wallet.lower() for wallet in holder_wallets
            if isinstance(wallet, str) and wallet
        ))
        if not wallets:
            raise ValueError("At least one holder wallet is required")
        if len(wallets) > self.max_wallets:
            raise ProviderError("Holder set exceeds the funding graph wallet cap")
        finalized = self._rpc("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(finalized, dict) or not isinstance(finalized.get("number"), str):
            raise ProviderError("Alchemy RPC did not return a finalized block")
        try:
            end_block = int(finalized["number"], 16)
        except ValueError as exc:
            raise ProviderError("Alchemy finalized block number is malformed") from exc
        if end_block < 0:
            raise ProviderError("Alchemy finalized block number is invalid")

        now_ms = self.clock_ms()
        queue = list(wallets)
        queried = set()
        relationships = []
        while queue:
            wallet = queue.pop(0)
            key = wallet.lower()
            if key in queried:
                continue
            if len(queried) >= self.max_wallets:
                raise ProviderError("Funding ancestry exceeded the configured wallet cap")
            queried.add(key)
            first = self._first_funder(wallet, end_block, now_ms)
            if first is None:
                relationships.append(FundingRelationship(
                    wallet, None, None, now_ms,
                    hashlib.sha256(f"{wallet}:no-inbound:{end_block}".encode()).hexdigest(),
                ))
                continue
            funder, tx_hash = first
            relationships.append(FundingRelationship(
                wallet, funder, tx_hash, now_ms,
                hashlib.sha256(f"{wallet}:{funder}:{tx_hash}".encode()).hexdigest(),
            ))
            if funder.lower() not in queried:
                queue.append(funder)

        # A wallet may appear as a holder and as an intermediary; deduplication
        # above ensures one auditable first-funder edge for each address.
        covered = tuple(sorted(queried))
        if {x.wallet_address.lower() for x in relationships} != set(covered):
            raise ProviderError("Funding graph did not cover every queried wallet")
        digest = hashlib.sha256(json.dumps(
            [(x.wallet_address.lower(),
              x.funder_address.lower() if x.funder_address else None,
              x.transaction_id, x.evidence_id) for x in relationships],
            sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        return FundingGraphSnapshot(
            provider=self.provider_id,
            chain="base",
            token_address=token_address,
            observed_at_ms=now_ms,
            evidence_id=f"alchemy-funding:{digest}",
            block_ref=f"finalized-block:{end_block}",
            coverage_start_ms=0,
            coverage_end_ms=now_ms,
            covered_wallets=covered,
            relationships=tuple(relationships),
            coverage_complete=True,
        )
