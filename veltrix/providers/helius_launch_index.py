"""Helius launch-window buy indexer for Solana Pump.fun tokens.

The adapter uses the mint address as the scan scope, Pump.fun buy instruction
filters, finalized mint supply, and the existing native-SOL funding graph.
It returns evidence only; shared funders are a heuristic, not proof of common
ownership or insider control.
"""
from __future__ import annotations

import hashlib
import json
import time

import requests

from ..launch_intelligence import LaunchCoverage, LaunchIndexSnapshot
from ..launch_bundles import LaunchBuyEvidence
from ..launch_origin import SolanaLaunchOriginResolver
from ..solana_meme.candidates import canonical_mint
from .helius_data import token_transfer_amount, unwrap_parsed_transaction
from .solana_mint import SolanaMintAuthorityProvider
from .base import ProviderError


PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_BUY_INSTRUCTIONS = (
    "buy", "buy_exact_sol_in", "buy_v2", "buy_exact_quote_in_v2",
)


class HeliusSolanaLaunchIndexProvider:
    provider_id = "helius-solana-launch-index"
    supported_chains = frozenset({"solana"})

    def __init__(
        self,
        api_key: str,
        rpc_url: str,
        funding_provider,
        *,
        session=None,
        timeout: float = 25.0,
        launch_window_ms: int = 120_000,
        max_scan_pages: int = 20,
        max_buyers: int = 500,
        max_scanned_transactions: int = 10_000,
        clock_ms=None,
        base_url: str = "https://mainnet.helius-rpc.com",
    ) -> None:
        if not api_key or not rpc_url:
            raise ValueError("Helius API key and Solana RPC URL are required")
        if launch_window_ms <= 0 or not 1 <= max_scan_pages <= 1_000:
            raise ValueError("Invalid launch window or scan page bound")
        if not 1 <= max_buyers <= 2_000 or not 1 <= max_scanned_transactions <= 10_000:
            raise ValueError("Invalid buyer or transaction scan budget")
        if getattr(funding_provider, "provider_id", None) != "helius-solana-funding-graph":
            raise ValueError("A Helius Solana funding graph provider is required")
        self.api_key = api_key
        self.rpc_url = rpc_url
        self.funding_provider = funding_provider
        self.session = session or requests.Session()
        self.timeout = timeout
        self.launch_window_ms = launch_window_ms
        self.max_scan_pages = max_scan_pages
        self.max_buyers = max_buyers
        self.max_scanned_transactions = max_scanned_transactions
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.base_url = base_url.rstrip("/")
        self.scan_url = f"{self.base_url}/v1/parsed-events/scan"
        self.origin_resolver = SolanaLaunchOriginResolver(
            api_key, session=self.session, base_url=self.base_url,
            timeout=timeout, max_pages=max_scan_pages,
        )
        self.mint_provider = SolanaMintAuthorityProvider(
            rpc_url, session=self.session, timeout_seconds=timeout,
            clock_ms=self.clock_ms,
        )

    @staticmethod
    def _is_pump_buy(tx: dict) -> bool:
        instructions = tx.get("instructions")
        if not isinstance(instructions, list):
            raise ProviderError("Parsed Events buy transaction omits instruction inventory")
        for item in instructions:
            if not isinstance(item, dict):
                continue
            name = item.get("instructionName") or item.get("name")
            program_id = item.get("programId") or item.get("program_id")
            program_name = str(item.get("programName") or "").lower()
            if name in PUMP_BUY_INSTRUCTIONS and (
                program_id == PUMP_FUN_PROGRAM_ID or program_name in {"pump", "pump_fun"}
            ):
                return True
        return False

    def _scan_launch_window(self, mint: str, start_ms: int, end_ms: int):
        cursor = None
        pages = 0
        rows = []
        scanned_through_ms = 0
        complete = False
        while pages < self.max_scan_pages:
            body = {
                "address": mint,
                "match": {
                    "programs": [{
                        "programId": PUMP_FUN_PROGRAM_ID,
                        "instructions": list(PUMP_BUY_INSTRUCTIONS),
                    }]
                },
                "time": {"gte": start_ms // 1000, "lte": end_ms // 1000},
                "limit": 100,
                "budget": {"maxScannedTransactions": self.max_scanned_transactions},
                "returnShape": "transactions",
            }
            if cursor:
                body["paginationToken"] = cursor
            try:
                response = self.session.post(
                    self.scan_url, params={"api-key": self.api_key}, json=body,
                    headers={"Content-Type": "application/json"}, timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ProviderError("Helius launch-window scan request failed") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ProviderError("Helius launch-window scan response is malformed")
            scan = payload.get("scan")
            if not isinstance(scan, dict):
                raise ProviderError("Helius launch-window scan omitted coverage summary")
            through = (scan.get("scannedThrough") or {}).get("blockTime")
            if isinstance(through, (int, float)) and not isinstance(through, bool):
                scanned_through_ms = max(scanned_through_ms, int(through * 1000))
            rows.extend(payload["data"])
            pages += 1
            cursor = payload.get("paginationToken")
            if not cursor:
                complete = (
                    scan.get("stoppedOn") == "windowEnd"
                    and scanned_through_ms >= end_ms
                )
                break
        if cursor:
            complete = False
        return rows, pages, scanned_through_ms, complete

    def fetch_launch_snapshot(
        self, chain: str, token_address: str, launch_window_ms: int | None = None,
    ) -> LaunchIndexSnapshot:
        if str(chain).lower() != "solana":
            raise ProviderError("Helius Solana launch index supports Solana only")
        mint = canonical_mint(token_address)
        window_ms = self.launch_window_ms if launch_window_ms is None else launch_window_ms
        if isinstance(window_ms, bool) or not isinstance(window_ms, int) or window_ms <= 0:
            raise ValueError("launch_window_ms must be a positive integer")
        now = self.clock_ms()
        origin = self.origin_resolver.resolve(mint, window_ms, now_ms=now)
        if (origin.chain != "solana" or origin.token_address != mint
                or origin.provider != self.origin_resolver.provider_id
                or origin.complete is not True
                or not origin.evidence_id
                or origin.launch_at_ms + window_ms != origin.window_end_ms):
            raise ProviderError("Solana launch-origin evidence is invalid or incomplete")
        end_ms = origin.window_end_ms
        mint_snapshot = self.mint_provider.fetch_snapshot(mint)
        if (mint_snapshot.mint != mint
                or mint_snapshot.provider != self.mint_provider.provider_id
                or not mint_snapshot.evidence_id
                or isinstance(mint_snapshot.supply_raw, bool)
                or not isinstance(mint_snapshot.supply_raw, int)
                or mint_snapshot.supply_raw <= 0
                or isinstance(mint_snapshot.decimals, bool)
                or not isinstance(mint_snapshot.decimals, int)
                or not 0 <= mint_snapshot.decimals <= 38
                or not isinstance(mint_snapshot.finalized_slot, int)
                or isinstance(mint_snapshot.finalized_slot, bool)
                or mint_snapshot.finalized_slot < 0):
            raise ProviderError("Finalized mint supply evidence is invalid")
        if (mint_snapshot.observed_at_ms > now
                or now - mint_snapshot.observed_at_ms > 300_000):
            raise ProviderError("Finalized mint supply evidence is stale or future-dated")
        total_supply = mint_snapshot.supply_raw / (10 ** mint_snapshot.decimals)

        raw_rows, pages, scanned_through_ms, scan_complete = self._scan_launch_window(
            mint, origin.launch_at_ms, end_ms,
        )
        buy_rows = []
        buyer_wallets = set()
        for raw in raw_rows:
            tx = unwrap_parsed_transaction(raw)
            if tx is None:
                raise ProviderError("Helius launch scan returned an unparsed transaction")
            if not self._is_pump_buy(tx):
                raise ProviderError("Helius launch scan returned a transaction outside the Pump.fun buy filter")
            stamp = tx.get("blockTime", tx.get("timestamp"))
            signature = tx.get("signature")
            if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                    or not isinstance(signature, str) or not signature):
                raise ProviderError("Pump.fun buy lacks timestamp or signature provenance")
            observed_at_ms = int(stamp * 1000)
            if observed_at_ms < origin.launch_at_ms or observed_at_ms > end_ms:
                raise ProviderError("Pump.fun buy is outside the requested launch window")
            transfers = tx.get("tokenTransfers")
            if not isinstance(transfers, list):
                raise ProviderError("Pump.fun buy omits token transfer inventory")
            recipients = {}
            for transfer in transfers:
                if not isinstance(transfer, dict) or transfer.get("mint") != mint:
                    continue
                wallet = transfer.get("toUserAccount")
                if not isinstance(wallet, str) or not wallet:
                    raise ProviderError("Pump.fun token transfer omits buyer wallet identity")
                amount = token_transfer_amount(transfer)
                recipients[wallet] = recipients.get(wallet, 0.0) + amount
            if len(recipients) != 1:
                raise ProviderError("Pump.fun buy must resolve to exactly one buyer wallet")
            wallet, amount = next(iter(recipients.items()))
            buy_rows.append((wallet, signature, observed_at_ms, amount))
            buyer_wallets.add(wallet)

        funding_by_wallet = {}
        funding_complete = True
        funding_snapshot = None
        if buyer_wallets:
            if len(buyer_wallets) > self.max_buyers:
                funding_complete = False
            else:
                funding_snapshot = self.funding_provider.fetch_snapshot(mint, sorted(buyer_wallets))
                funding_by_wallet = {
                    edge.wallet_address: edge for edge in funding_snapshot.relationships
                }
                funding_complete = (
                    funding_snapshot.provider == self.funding_provider.provider_id
                    and funding_snapshot.chain == "solana"
                    and funding_snapshot.token_address == mint
                    and bool(funding_snapshot.evidence_id)
                    and bool(funding_snapshot.block_ref)
                    and funding_snapshot.observed_at_ms <= now
                    and now - funding_snapshot.observed_at_ms <= 300_000
                    and funding_snapshot.coverage_complete is True
                    and set(buyer_wallets).issubset(funding_by_wallet)
                    and all(funding_by_wallet[w].funder_address for w in buyer_wallets)
                )

        observations = []
        for wallet, signature, observed_at_ms, amount in buy_rows:
            edge = funding_by_wallet.get(wallet)
            observations.append(LaunchBuyEvidence(
                provider=self.provider_id,
                chain="solana",
                token_address=mint,
                wallet_address=wallet,
                funder_address=edge.funder_address if edge and edge.funder_address else "",
                transaction_id=signature,
                token_amount=amount,
                observed_at_ms=observed_at_ms,
                funder_provider=(funding_snapshot.provider if funding_snapshot else ""),
                funder_evidence_id=(funding_snapshot.evidence_id if funding_snapshot else ""),
            ))

        complete = bool(
            origin.complete and scan_complete and funding_complete
            and mint_snapshot.token_program_id
            and mint_snapshot.finalized_slot >= 0
        )
        evidence_material = {
            "origin": origin.evidence_id,
            "mint": mint_snapshot.evidence_id,
            "scan_pages": pages,
            "scanned_through_ms": scanned_through_ms,
            "buys": [(x[1], x[0], x[2], x[3]) for x in buy_rows],
            "funding": funding_snapshot.evidence_id if funding_snapshot else None,
            "complete": complete,
        }
        evidence_id = hashlib.sha256(
            json.dumps(evidence_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        coverage = LaunchCoverage(
            provider=self.provider_id,
            chain="solana",
            token_address=mint,
            requested_start_ms=origin.launch_at_ms,
            requested_end_ms=end_ms,
            indexed_through_ms=scanned_through_ms,
            pages_expected=pages if complete else max(1, pages + (1 if pages >= self.max_scan_pages else 0)),
            pages_completed=pages,
            source_complete=complete,
            evidence_id=f"helius-solana-launch-window:{evidence_id}",
        )
        return LaunchIndexSnapshot(
            provider=self.provider_id,
            chain="solana",
            token_address=mint,
            launch_at_ms=origin.launch_at_ms,
            total_supply=total_supply,
            supply_decimals=mint_snapshot.decimals,
            observations=tuple(observations),
            coverage=coverage,
            observed_at_ms=now,
            supply_block_ref=f"finalized-slot:{mint_snapshot.finalized_slot}",
        )
