"""Helius Parsed Events based Solana first-funder ancestry graph.

The adapter scans full native-SOL transfer history for each holder and
intermediary. Parser errors, pagination truncation, or unresolved pages fail
closed; bounded lookbacks are never represented as complete ancestry.
"""
import hashlib
import json
import time

import requests

from ..developer_concentration import FundingGraphSnapshot, FundingRelationship
from .base import ProviderError
from .helius_data import unwrap_parsed_transaction


class HeliusSolanaFundingGraphProvider:
    provider_id = "helius-solana-funding-graph"
    chain = "solana"
    NATIVE_SOL_MINT = "So11111111111111111111111111111111111111111"

    def __init__(self, api_key, *, session=None, timeout=25.0, max_pages=100,
                 max_wallets=2_000, page_size=100, clock_ms=None,
                 base_url="https://mainnet.helius-rpc.com"):
        if not api_key:
            raise ValueError("Helius API key is required")
        if not 1 <= max_pages <= 10_000 or not 1 <= max_wallets <= 10_000:
            raise ValueError("Invalid Solana funding graph bounds")
        if not 1 <= page_size <= 100:
            raise ValueError("Helius Parsed Events Scan limit must be between 1 and 100")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_wallets = max_wallets
        self.page_size = page_size
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.endpoint = f"{base_url.rstrip('/')}/v1/parsed-events/scan"

    def _history(self, wallet, end_ms):
        cursor = None
        pages = 0
        inbound = []
        scanned_through = 0
        stopped_on = None
        while pages < self.max_pages:
            body = {
                "address": wallet,
                "match": {"mints": [{"mint": self.NATIVE_SOL_MINT}]},
                "tokenAccounts": "balanceChanged",
                "time": {"gte": 0, "lte": end_ms // 1000},
                "limit": self.page_size,
                "returnShape": "transactions",
            }
            if cursor:
                body["paginationToken"] = cursor
            try:
                response = self.session.post(
                    self.endpoint, params={"api-key": self.api_key}, json=body,
                    headers={"Content-Type": "application/json"}, timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ProviderError("Helius Solana funding history request failed") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ProviderError("Helius Solana funding history response is malformed")
            scan = payload.get("scan") or {}
            through = (scan.get("scannedThrough") or {}).get("blockTime")
            if isinstance(through, (int, float)):
                scanned_through = max(scanned_through, int(through * 1000))
            stopped_on = scan.get("stoppedOn", stopped_on)
            for raw in payload["data"]:
                tx = unwrap_parsed_transaction(raw)
                if tx is None:
                    raise ProviderError("Helius returned an unparsed transaction in funding history")
                stamp = tx.get("blockTime", tx.get("timestamp"))
                signature = tx.get("signature")
                if not isinstance(stamp, (int, float)) or not isinstance(signature, str) or not signature:
                    raise ProviderError("Helius funding transaction lacks timestamp or signature")
                observed = int(stamp * 1000)
                if observed > end_ms:
                    continue
                transfers = tx.get("nativeTransfers")
                if not isinstance(transfers, list):
                    raise ProviderError("Helius funding transaction omits native transfer evidence")
                for transfer in transfers:
                    if not isinstance(transfer, dict):
                        raise ProviderError("Helius native transfer row is malformed")
                    recipient = transfer.get("toUserAccount")
                    sender = transfer.get("fromUserAccount")
                    amount = transfer.get("amount")
                    if recipient == wallet and isinstance(sender, str) and sender != wallet:
                        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
                            raise ProviderError("Helius native transfer amount is invalid")
                        inbound.append((observed, sender, signature))
            pages += 1
            cursor = payload.get("paginationToken")
            if not cursor:
                if stopped_on != "windowEnd" or scanned_through < end_ms:
                    raise ProviderError("Helius did not certify complete wallet history coverage")
                return inbound, pages, scanned_through
        if cursor:
            raise ProviderError("Helius Solana funding history exceeded pagination bound")
        raise ProviderError("Helius Solana funding history did not reach the requested end")

    def fetch_snapshot(self, token_address, holder_wallets):
        if not isinstance(token_address, str) or not token_address:
            raise ValueError("Token address is required")
        initial = tuple(dict.fromkeys(x for x in holder_wallets if isinstance(x, str) and x))
        if not initial:
            raise ValueError("At least one holder wallet is required")
        if len(initial) > self.max_wallets:
            raise ProviderError("Holder set exceeds Solana funding graph wallet cap")
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
            inbound, pages, _ = self._history(wallet, now)
            total_pages += pages
            if inbound:
                observed, funder, signature = min(inbound, key=lambda row: (row[0], row[2]))
                evidence = hashlib.sha256(
                    f"{wallet}:{funder}:{signature}:{observed}".encode()
                ).hexdigest()
                relationships.append(FundingRelationship(
                    wallet, funder, signature, now, f"helius-sol-funding:{evidence}"
                ))
                if funder not in queried:
                    queue.append(funder)
            else:
                evidence = hashlib.sha256(f"{wallet}:no-inbound-sol:{now}".encode()).hexdigest()
                relationships.append(FundingRelationship(
                    wallet, None, None, now, f"helius-sol-funding:{evidence}"
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
            observed_at_ms=now, evidence_id=f"helius-sol-funding-graph:{digest}",
            block_ref=f"helius-parsed-events-pages:{total_pages}",
            coverage_start_ms=0, coverage_end_ms=now, covered_wallets=covered,
            relationships=tuple(relationships), coverage_complete=True,
        )
