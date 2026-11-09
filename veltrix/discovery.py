"""Candidate discovery from DexScreener's latest token-profile feed.

This feed is promotional/profile-driven, not a complete new-pair stream.
Candidates are never considered safe or tradeable based on discovery alone.
"""
from dataclasses import dataclass
from typing import Iterable
from .providers.base import ProviderError
from .providers.dexscreener import CHAIN_IDS

@dataclass(frozen=True)
class TokenCandidate:
    chain: str
    token_address: str
    source: str
    discovered_at_ms: int

class DexScreenerDiscovery:
    name = "dexscreener-token-profiles"

    def __init__(self, base_url="https://api.dexscreener.com", timeout=10.0, session=None):
        import requests
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def discover(self, allowed_chains: Iterable[str]) -> list[TokenCandidate]:
        allowed = {CHAIN_IDS[c.lower()] for c in allowed_chains if c.lower() in CHAIN_IDS}
        if not allowed:
            return []
        try:
            response = self.session.get(f"{self.base_url}/token-profiles/latest/v1", timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise ProviderError(f"DexScreener discovery request failed: {exc}") from exc
        if not isinstance(payload, list):
            raise ProviderError("Unexpected token profile response shape")
        import time
        now = int(time.time() * 1000)
        seen = set()
        candidates = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            chain, address = item.get("chainId"), item.get("tokenAddress")
            if chain not in allowed or not isinstance(address, str) or not address.strip():
                continue
            key = (chain, address)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(TokenCandidate(chain, address, self.name, now))
        return candidates
