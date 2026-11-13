"""Read-only GoPlus token-risk connector.

The connector deliberately leaves developer concentration and bundled-supply
fields unknown unless a provider response explicitly documents those values.
GoPlus top-holder data is not a substitute for a complete developer/bundle audit.
"""
import hashlib
import json
import time
import requests

from .intelligence import TokenSafetyObservation
from .base import ProviderError


GOPLUS_CHAIN_IDS = {"base": "8453", "bsc": "56", "solana": "solana"}


class GoPlusTokenSafetyProvider:
    provider_id = "goplus"

    def __init__(self, access_token="", base_url="https://api.gopluslabs.io",
                 timeout=12.0, session=None, clock_ms=None):
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def fetch_safety_observation(self, chain, token_address):
        chain_key = str(chain).lower()
        chain_id = GOPLUS_CHAIN_IDS.get(chain_key)
        if not chain_id or not isinstance(token_address, str) or not token_address.strip():
            raise ProviderError(f"GoPlus does not support configured chain/address: {chain}")
        if chain_key == "solana" and not self.access_token:
            raise ProviderError("GoPlus Solana security endpoint requires an access token")
        url = f"{self.base_url}/api/v1/solana/token_security" if chain_key == "solana" else f"{self.base_url}/api/v1/token_security/{chain_id}"
        headers = {"accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            response = self.session.get(
                url, params={"contract_addresses": token_address},
                headers=headers, timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"GoPlus request failed ({type(exc).__name__})") from None
        if not isinstance(payload, dict) or str(payload.get("code")) != "1":
            raise ProviderError("GoPlus returned an unsuccessful or malformed response")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProviderError("GoPlus response has no result map")
        item = result.get(token_address) or result.get(token_address.lower())
        if not isinstance(item, dict):
            raise ProviderError("GoPlus did not return the requested token")
        locked = self._liquidity_lock_status(item)
        # Do not infer creator/developer or bundled supply from generic top holders.
        evidence_id = hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return TokenSafetyObservation(
            provider=self.provider_id,
            token_address=token_address,
            evidence_id=f"goplus:{evidence_id}",
            observed_at_ms=self.clock_ms(),
            liquidity_locked=locked,
            dev_hold_pct=None,
            bundled_supply_pct=None,
            creator_hold_pct=self._creator_hold_pct(item) if chain_key in {"base", "bsc"} else None,
        )

    @staticmethod
    def _creator_hold_pct(item):
        """Map GoPlus EVM creator_percent (0..1 fraction) to percentage points.

        This is retained as creator concentration only, not treated as complete
        developer-wallet concentration. Missing or invalid values remain unknown.
        """
        raw = item.get("creator_percent")
        try:
            fraction = float(raw)
        except (TypeError, ValueError):
            return None
        if not 0 <= fraction <= 1 or not fraction < float("inf"):
            return None
        return fraction * 100.0

    @staticmethod
    def _liquidity_lock_status(item):
        holders = item.get("lp_holders")
        count = item.get("lp_holder_count")
        if not isinstance(holders, list) or not holders:
            return None
        try:
            reported_count = int(count)
        except (TypeError, ValueError):
            return None
        # API may return only the top 10 LP holders. Require complete coverage.
        if reported_count != len(holders):
            return None
        statuses = [holder.get("is_locked") for holder in holders if isinstance(holder, dict)]
        if len(statuses) != len(holders):
            return None
        if all(str(status) == "1" for status in statuses):
            return True
        if any(str(status) == "0" for status in statuses):
            return False
        return None
