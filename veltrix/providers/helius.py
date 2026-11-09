"""Read-only Helius Solana wallet-history adapter.

Only successful SWAP records with an inbound target-mint transfer are considered.
USD notional is estimated from received token quantity times an injected spot-price
resolver; it is not the historical execution value. Keep this distinction in logs.
"""
import time
import requests

from .base import ProviderError
from .intelligence import SmartWalletObservation


class HeliusWalletProvider:
    provider_id = "helius"

    def __init__(self, api_key, tracked_wallets=(), price_usd_resolver=None,
                 base_url="https://api.helius.xyz", timeout=15.0, session=None,
                 clock_ms=None, limit=100):
        if not api_key:
            raise ValueError("Helius API key is required")
        if not callable(price_usd_resolver):
            raise ValueError("A token USD spot-price resolver is required")
        if not 1 <= limit <= 100:
            raise ValueError("Helius page limit must be between 1 and 100")
        self.api_key = api_key
        self.tracked_wallets = tuple(dict.fromkeys(w for w in tracked_wallets if w))
        self.price_usd_resolver = price_usd_resolver
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.limit = limit

    def fetch_wallet_observations(self, chain, token_addresses, since_ms):
        if str(chain).lower() != "solana":
            raise ProviderError("HeliusWalletProvider currently supports Solana only")
        targets = set(token_addresses)
        if not targets or not self.tracked_wallets:
            return ()
        now_ms = self.clock_ms()
        observations, seen = [], set()
        for wallet in self.tracked_wallets:
            url = f"{self.base_url}/v0/addresses/{wallet}/transactions"
            try:
                response = self.session.get(
                    url,
                    params={"api-key": self.api_key, "limit": self.limit, "sort-order": "desc"},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ProviderError(f"Helius wallet history request failed: {exc}") from exc
            if not isinstance(payload, list):
                raise ProviderError("Helius returned an unexpected wallet-history shape")
            for tx in payload:
                if not isinstance(tx, dict) or tx.get("type") != "SWAP":
                    continue
                if tx.get("transactionError") not in (None, {}, False):
                    continue
                signature = tx.get("signature")
                timestamp = tx.get("timestamp")
                if not isinstance(signature, str) or not signature:
                    continue
                if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                    continue
                observed_at_ms = int(timestamp * 1000)
                if observed_at_ms < since_ms or observed_at_ms > now_ms:
                    continue
                transfers = tx.get("tokenTransfers")
                if not isinstance(transfers, list):
                    continue
                for transfer in transfers:
                    if not isinstance(transfer, dict):
                        continue
                    mint = transfer.get("mint")
                    if mint not in targets or transfer.get("toUserAccount") != wallet:
                        continue
                    if transfer.get("fromUserAccount") == wallet:
                        continue
                    raw_amount = transfer.get("tokenAmount")
                    try:
                        amount = float(raw_amount)
                    except (TypeError, ValueError):
                        continue
                    if amount <= 0 or not amount < float("inf"):
                        continue
                    try:
                        price = float(self.price_usd_resolver(mint))
                    except (TypeError, ValueError, RuntimeError):
                        continue
                    if price <= 0 or not price < float("inf"):
                        continue
                    notional = amount * price
                    if notional <= 0 or notional >= float("inf"):
                        continue
                    key = (wallet, signature, mint)
                    if key in seen:
                        continue
                    seen.add(key)
                    observations.append(SmartWalletObservation(
                        provider=self.provider_id,
                        chain="solana",
                        token_address=mint,
                        wallet_address=wallet,
                        side="BUY",
                        notional_usd=notional,
                        transaction_id=signature,
                        observed_at_ms=observed_at_ms,
                    ))
        return tuple(observations)
