import time
import requests
from .base import MarketEvent, ProviderError

CHAIN_IDS = {"solana": "solana", "base": "base", "bsc": "bsc"}

class DexScreenerProvider:
    """Read-only token-pair snapshots; pair data is not a token safety audit."""
    name = "dexscreener"
    def __init__(self, base_url="https://api.dexscreener.com", timeout=10.0, session=None):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        self.session = session or requests.Session()
    def fetch_token_pairs(self, chain, token_addresses):
        chain_id = CHAIN_IDS.get(str(chain).lower())
        if chain_id is None: raise ProviderError(f"Unsupported chain: {chain}")
        addresses = [a.strip() for a in token_addresses if isinstance(a, str) and a.strip()]
        events = []
        for offset in range(0, len(addresses), 30):
            url = f"{self.base_url}/tokens/v1/{chain_id}/" + ",".join(addresses[offset:offset + 30])
            try:
                response = self.session.get(url, timeout=self.timeout); response.raise_for_status(); payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ProviderError(f"DexScreener request failed: {exc}") from exc
            if not isinstance(payload, list): raise ProviderError("Unexpected DexScreener response shape")
            observed = int(time.time() * 1000)
            for pair in payload:
                event = self._normalize(pair, chain_id, observed)
                if event is not None: events.append(event)
        return events
    @staticmethod
    def _num(value, field):
        try: result = float(value)
        except (TypeError, ValueError) as exc: raise ProviderError(f"Invalid numeric field: {field}") from exc
        if result < 0 or not result < float("inf"): raise ProviderError(f"Out-of-range numeric field: {field}")
        return result
    @classmethod
    def _normalize(cls, pair, chain, observed):
        if not isinstance(pair, dict) or pair.get("chainId") != chain: return None
        base = pair.get("baseToken") or {}; address, pair_address = base.get("address"), pair.get("pairAddress")
        if not address or not pair_address: return None
        try: m5 = float((pair.get("priceChange") or {}).get("m5", 0) or 0)
        except (TypeError, ValueError) as exc: raise ProviderError("Invalid priceChange.m5") from exc
        if not abs(m5) < float("inf"): raise ProviderError("Invalid priceChange.m5")
        created = pair.get("pairCreatedAt")
        return MarketEvent(chain, str(address), str(pair_address), base.get("symbol"), cls._num(pair.get("priceUsd"), "priceUsd"), cls._num((pair.get("liquidity") or {}).get("usd", 0), "liquidity.usd"), cls._num((pair.get("volume") or {}).get("h1", 0), "volume.h1"), m5, observed, int(created) if created is not None else None, "dexscreener")
