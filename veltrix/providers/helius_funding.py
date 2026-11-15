"""Helius all-asset first-funder ancestry using finalized transfer history.

The adapter uses getTransfersByAddress so native SOL, SPL and Token-2022
transfers participate in the same wallet graph. Pagination must be exhausted;
unfinalized, malformed, capped or unresolved history fails closed.
"""
import hashlib
import json
import time

import requests

from ..developer_concentration import FundingGraphSnapshot, FundingRelationship
from .base import ProviderError


class HeliusSolanaFundingGraphProvider:
    provider_id = "helius-solana-funding-graph"
    chain = "solana"

    def __init__(self, api_key, *, session=None, timeout=25.0, max_pages=100,
                 max_wallets=2_000, page_size=100, max_total_pages=1_000,
                 clock_ms=None, base_url="https://mainnet.helius-rpc.com"):
        if not api_key:
            raise ValueError("Helius API key is required")
        if not 1 <= max_pages <= 10_000 or not 1 <= max_wallets <= 10_000:
            raise ValueError("Invalid Solana funding graph bounds")
        if not 1 <= page_size <= 100 or max_total_pages < 1:
            raise ValueError("Invalid Helius transfer page or total-page bound")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_wallets = max_wallets
        self.page_size = page_size
        self.max_total_pages = max_total_pages
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.endpoint = base_url.rstrip("/") + "/"
        self._request_id = 0

    def _rpc(self, method, params):
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id,
                "method": method, "params": params}
        try:
            response = self.session.post(
                self.endpoint, params={"api-key": self.api_key}, json=body,
                headers={"Content-Type": "application/json"}, timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Helius {method} request failed") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"Helius {method} response is malformed")
        return payload["result"]

    def _history(self, wallet, finalized_slot, page_budget):
        cursor = None
        pages = 0
        inbound = []
        previous_order = None
        while pages < min(self.max_pages, page_budget):
            query = {
                "direction": "in",
                "sortOrder": "asc",
                "commitment": "finalized",
                "limit": self.page_size,
                "solMode": "merged",
                "filters": {"slot": {"lte": finalized_slot}},
            }
            if cursor:
                query["paginationToken"] = cursor
            result = self._rpc("getTransfersByAddress", [wallet, query])
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ProviderError("Helius transfer history response is malformed")
            for row in result["data"]:
                if not isinstance(row, dict):
                    raise ProviderError("Helius transfer history row is malformed")
                kind = row.get("type")
                slot = row.get("slot")
                stamp = row.get("blockTime")
                signature = row.get("signature")
                sender = row.get("fromUserAccount")
                recipient = row.get("toUserAccount")
                status = row.get("confirmationStatus")
                if (not isinstance(slot, int) or isinstance(slot, bool) or slot < 0
                        or slot > finalized_slot
                        or not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0
                        or not isinstance(signature, str) or not signature
                        or status != "finalized"):
                    raise ProviderError("Helius transfer row lacks finalized slot or transaction provenance")
                order = (slot, int(row.get("transactionIdx", 0) or 0),
                         int(row.get("instructionIdx", 0) or 0),
                         int(row.get("innerInstructionIdx", 0) or 0))
                if previous_order is not None and order < previous_order:
                    raise ProviderError("Helius transfer history is not in ascending chain order")
                previous_order = order
                # transferFee is a withheld-fee accounting row, not a new
                # wallet-to-wallet funding event. Mints, burns, wraps and
                # unwraps are likewise excluded from first-funder ancestry.
                if kind != "transfer":
                    continue
                if recipient != wallet:
                    raise ProviderError("Helius inbound transfer is attributed to another recipient")
                if not isinstance(sender, str) or not sender or sender == wallet:
                    continue
                amount = row.get("amount")
                try:
                    raw_amount = int(amount)
                except (TypeError, ValueError) as exc:
                    raise ProviderError("Helius transfer amount is malformed") from exc
                if raw_amount <= 0:
                    continue
                mint = row.get("mint")
                inbound.append((order, stamp, sender, signature, str(mint or "native")))
            pages += 1
            cursor = result.get("paginationToken")
            if not cursor:
                return inbound, pages
        if cursor:
            raise ProviderError("Helius Solana transfer history exceeded pagination bound")
        raise ProviderError("Helius Solana transfer history did not terminate")

    def fetch_snapshot(self, token_address, holder_wallets):
        if not isinstance(token_address, str) or not token_address:
            raise ValueError("Token address is required")
        initial = tuple(dict.fromkeys(
            x for x in holder_wallets if isinstance(x, str) and x
        ))
        if not initial:
            raise ValueError("At least one holder wallet is required")
        if len(initial) > self.max_wallets:
            raise ProviderError("Holder set exceeds Solana funding graph wallet cap")
        finalized_slot = self._rpc("getSlot", [{"commitment": "finalized"}])
        if not isinstance(finalized_slot, int) or isinstance(finalized_slot, bool) or finalized_slot < 0:
            raise ProviderError("Helius did not return a valid finalized slot")
        now = self.clock_ms()
        queue = list(initial)
        queried = set()
        relationships = []
        total_pages = 0
        while queue:
            wallet = queue.pop(0)
            if wallet in queried:
                continue
            if len(queried) >= self.max_wallets:
                raise ProviderError("Solana funding ancestry exceeded wallet cap")
            queried.add(wallet)
            remaining_pages = self.max_total_pages - total_pages
            if remaining_pages <= 0:
                raise ProviderError("Solana funding graph exceeded total transfer-page budget")
            inbound, pages = self._history(wallet, finalized_slot, remaining_pages)
            total_pages += pages
            if inbound:
                order, _, funder, signature, mint = min(inbound, key=lambda row: row[0])
                evidence = hashlib.sha256(
                    f"{wallet}:{funder}:{signature}:{order}:{mint}".encode()
                ).hexdigest()
                relationships.append(FundingRelationship(
                    wallet, funder, signature, now, f"helius-transfer-funding:{evidence}"
                ))
                if funder not in queried:
                    queue.append(funder)
            else:
                evidence = hashlib.sha256(
                    f"{wallet}:no-inbound-transfer:{finalized_slot}".encode()
                ).hexdigest()
                relationships.append(FundingRelationship(
                    wallet, None, None, now, f"helius-transfer-funding:{evidence}"
                ))
        covered = tuple(sorted(queried))
        if {x.wallet_address for x in relationships} != set(covered):
            raise ProviderError("Solana funding graph does not cover every queried wallet")
        digest = hashlib.sha256(json.dumps(
            [(x.wallet_address, x.funder_address, x.transaction_id, x.evidence_id)
             for x in relationships], sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        return FundingGraphSnapshot(
            provider=self.provider_id, chain="solana", token_address=token_address,
            observed_at_ms=now, evidence_id=f"helius-transfer-funding-graph:{digest}",
            block_ref=f"helius-finalized-slot:{finalized_slot};pages:{total_pages}",
            coverage_start_ms=0, coverage_end_ms=now, covered_wallets=covered,
            relationships=tuple(relationships), coverage_complete=True,
        )
