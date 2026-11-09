"""Read-only Alchemy multichain wallet-transfer adapter for Base and BNB Chain.

A BUY is emitted only when the same transaction contains both an inbound target
ERC-20 transfer and an outbound allowlisted stablecoin transfer from the wallet.
Plain deposits, airdrops, bridges and token transfers are not classified as buys.
"""
from datetime import datetime, timezone
import time
import requests

from .base import ProviderError
from .intelligence import SmartWalletObservation


class AlchemyWalletProvider:
    provider_id = "alchemy"
    NETWORKS = {"base": "base-mainnet", "bsc": "bnb-mainnet"}

    def __init__(self, api_key, tracked_wallets=(), quote_token_addresses=None,
                 base_url="https://api.g.alchemy.com/data/v1",
                 timeout=20.0, session=None, clock_ms=None,
                 max_count=100, max_pages=2):
        if not api_key:
            raise ValueError("Alchemy API key is required")
        if not 1 <= max_count <= 1000 or max_pages < 1:
            raise ValueError("Invalid Alchemy pagination limits")
        self.api_key = api_key
        self.tracked_wallets = tuple(dict.fromkeys(
            w.lower() for w in tracked_wallets if isinstance(w, str) and w
        ))
        self.quote_token_addresses = {
            chain: frozenset(
                address.lower() for address in addresses if isinstance(address, str) and address
            )
            for chain, addresses in (quote_token_addresses or {}).items()
        }
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_count, self.max_pages = max_count, max_pages

    @classmethod
    def from_settings(cls, settings):
        return cls(
            api_key=settings.alchemy_api_key,
            tracked_wallets=settings.tracked_evm_wallets,
            quote_token_addresses={
                "base": settings.alchemy_quote_tokens_base,
                "bsc": settings.alchemy_quote_tokens_bsc,
            },
        )

    def fetch_wallet_observations(self, chain, token_addresses, since_ms):
        chain = str(chain).lower()
        network = self.NETWORKS.get(chain)
        if not network:
            raise ProviderError("Alchemy wallet adapter supports Base and BSC only")
        quote_addresses = self.quote_token_addresses.get(chain, frozenset())
        targets = {
            token.lower() for token in token_addresses
            if isinstance(token, str) and token and token.lower() not in quote_addresses
        }
        if not targets or not self.tracked_wallets or not quote_addresses:
            return ()
        now_ms = self.clock_ms()
        if since_ms < 0 or since_ms > now_ms:
            raise ValueError("since_ms must be between zero and current time")
        start = datetime.fromtimestamp(since_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
        end = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
        endpoint = f"{self.base_url}/{self.api_key}/transfers/by-address"
        observations = []
        seen = set()
        for wallet in self.tracked_wallets:
            cursor = None
            for _ in range(self.max_pages):
                body = {
                    "address": wallet,
                    "direction": "both",
                    "networks": [network],
                    "fromTimestamp": start,
                    "toTimestamp": end,
                    "category": ["external", "erc20"],
                    "order": "desc",
                    "maxCount": self.max_count,
                }
                if cursor:
                    body["pageKey"] = cursor
                try:
                    response = self.session.post(
                        endpoint, json=body,
                        headers={"Content-Type": "application/json"},
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (requests.RequestException, ValueError) as exc:
                    raise ProviderError(f"Alchemy transfers request failed: {exc}") from exc
                if not isinstance(payload, dict) or not isinstance(payload.get("transfers"), list):
                    raise ProviderError("Alchemy returned an unexpected transfers response")
                partial = (payload.get("error") or {}).get("partialErrors", [])
                if partial:
                    raise ProviderError("Alchemy returned partial network/category data")
                self._normalize_transactions(
                    payload["transfers"], chain, network, wallet, targets,
                    quote_addresses, since_ms, now_ms, observations, seen,
                )
                cursor = payload.get("pageKey")
                if not cursor:
                    break
        return tuple(observations)

    @staticmethod
    def _normalize_transactions(transfers, chain, network, wallet, targets,
                                quote_addresses, since_ms, now_ms, output, seen):
        grouped = {}
        for transfer in transfers:
            if not isinstance(transfer, dict) or transfer.get("network") != network:
                continue
            tx_hash = transfer.get("hash")
            if not isinstance(tx_hash, str) or not tx_hash:
                continue
            timestamp = transfer.get("blockTimestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                observed_ms = int(observed_at.timestamp() * 1000)
            except (ValueError, OverflowError):
                continue
            if observed_ms < since_ms or observed_ms > now_ms:
                continue
            from_address = str(transfer.get("from") or "").lower()
            to_address = str(transfer.get("to") or "").lower()
            contract = str(transfer.get("contractAddress") or "").lower()
            fungible = transfer.get("fungible") or {}
            try:
                amount = float(fungible.get("value"))
            except (TypeError, ValueError):
                continue
            if amount <= 0 or not amount < float("inf"):
                continue
            row = grouped.setdefault(tx_hash, {"inbound": [], "outbound_quote": []})
            if (transfer.get("category") == "erc20" and to_address == wallet
                    and from_address != wallet and contract in targets):
                row["inbound"].append((contract, observed_ms))
            if (from_address == wallet and to_address != wallet
                    and contract in quote_addresses
                    and transfer.get("category") == "erc20"
                    and str(fungible.get("asset") or "").upper() in {"USDC", "USDT", "DAI"}):
                row["outbound_quote"].append(amount)
        for tx_hash, legs in grouped.items():
            if not legs["inbound"] or not legs["outbound_quote"]:
                continue
            # Avoid assigning one transaction's full quote spend to multiple mints.
            tokens = {token for token, _ in legs["inbound"]}
            if len(tokens) != 1:
                continue
            token = next(iter(tokens))
            notional = sum(legs["outbound_quote"])
            observed_ms = max(timestamp for mint, timestamp in legs["inbound"] if mint == token)
            key = (chain, wallet, tx_hash, token)
            if key in seen:
                continue
            seen.add(key)
            output.append(SmartWalletObservation(
                provider="alchemy", chain=chain, token_address=token,
                wallet_address=wallet, side="BUY", notional_usd=notional,
                transaction_id=tx_hash, observed_at_ms=observed_ms,
            ))
