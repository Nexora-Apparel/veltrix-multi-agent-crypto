"""Read-only Helius Parsed Events Scan adapter for Solana wallet swaps.

Only successful parsed swap transactions with an explicit inbound transfer of a
target mint are considered. USD notional is estimated from received token quantity
times an injected spot-price resolver, not historical execution value.
"""
import time
import requests

from .base import ProviderError
from .intelligence import SmartWalletObservation


class HeliusWalletProvider:
    provider_id = "helius"

    def __init__(self, api_key, tracked_wallets=(), price_usd_resolver=None,
                 base_url="https://mainnet.helius-rpc.com", timeout=20.0,
                 session=None, clock_ms=None, limit=100, max_pages=2):
        if not api_key:
            raise ValueError("Helius API key is required")
        if not callable(price_usd_resolver):
            raise ValueError("A token USD spot-price resolver is required")
        if not 1 <= limit <= 100 or max_pages < 1:
            raise ValueError("Invalid Helius scan limits")
        self.api_key = api_key
        self.tracked_wallets = tuple(dict.fromkeys(w for w in tracked_wallets if w))
        self.price_usd_resolver = price_usd_resolver
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.limit, self.max_pages = limit, max_pages

    @classmethod
    def from_settings(cls, settings, price_usd_resolver):
        return cls(
            api_key=settings.helius_api_key,
            tracked_wallets=settings.tracked_smart_wallets,
            price_usd_resolver=price_usd_resolver,
        )

    def fetch_wallet_observations(self, chain, token_addresses, since_ms):
        if str(chain).lower() != "solana":
            raise ProviderError("Helius Parsed Events currently supports Solana only")
        targets = tuple(dict.fromkeys(token_addresses))
        if not targets or not self.tracked_wallets:
            return ()
        now_ms = self.clock_ms()
        observations, seen = [], set()
        endpoint = f"{self.base_url}/v1/parsed-events/scan"
        for wallet in self.tracked_wallets:
            cursor = None
            for _ in range(self.max_pages):
                body = {
                    "address": wallet,
                    "match": {"mints": [{"mint": mint} for mint in targets]},
                    "tokenAccounts": "balanceChanged",
                    "time": {"gte": max(0, since_ms // 1000)},
                    "limit": self.limit,
                    "returnShape": "transactions",
                }
                if cursor:
                    body["paginationToken"] = cursor
                try:
                    response = self.session.post(
                        endpoint, params={"api-key": self.api_key},
                        json=body, headers={"Content-Type": "application/json"},
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (requests.RequestException, ValueError) as exc:
                    raise ProviderError(f"Helius Parsed Events request failed: {exc}") from exc
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ProviderError("Helius returned an unexpected Parsed Events response")
                for tx in payload["data"]:
                    self._append_swap_observations(
                        tx, wallet, set(targets), since_ms, now_ms, observations, seen
                    )
                cursor = payload.get("paginationToken")
                if not cursor:
                    break
        return tuple(observations)

    def _append_swap_observations(self, tx, wallet, targets, since_ms, now_ms, output, seen):
        if not isinstance(tx, dict):
            return
        summary = tx.get("summary") or {}
        if not isinstance(summary, dict) or str(summary.get("type", "")).lower() != "swap":
            return
        signature = tx.get("signature")
        timestamp = tx.get("blockTime", tx.get("timestamp"))
        if not isinstance(signature, str) or not signature:
            return
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            return
        observed_at_ms = int(timestamp * 1000)
        if observed_at_ms < since_ms or observed_at_ms > now_ms:
            return
        transfers = tx.get("tokenTransfers")
        if not isinstance(transfers, list):
            return
        for transfer in transfers:
            if not isinstance(transfer, dict):
                continue
            mint = transfer.get("mint")
            if mint not in targets or transfer.get("toUserAccount") != wallet:
                continue
            if transfer.get("fromUserAccount") == wallet:
                continue
            try:
                amount = float(transfer.get("tokenAmount"))
                price = float(self.price_usd_resolver(mint))
            except (TypeError, ValueError, RuntimeError):
                continue
            if (amount <= 0 or price <= 0 or not amount < float("inf")
                    or not price < float("inf")):
                continue
            notional = amount * price
            if notional <= 0 or not notional < float("inf"):
                continue
            key = (wallet, signature, mint)
            if key in seen:
                continue
            seen.add(key)
            output.append(SmartWalletObservation(
                provider=self.provider_id,
                chain="solana",
                token_address=mint,
                wallet_address=wallet,
                side="BUY",
                notional_usd=notional,
                transaction_id=signature,
                observed_at_ms=observed_at_ms,
            ))
