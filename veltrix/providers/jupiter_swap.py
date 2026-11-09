"""Read-only Jupiter Swap API V2 quote provider for token-to-wSOL sell routes.

This adapter deliberately omits the taker parameter. Jupiter documents that
/order still returns quote data without a taker and returns no transaction.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

import requests

from .base import ProviderError
from ..solana_meme.candidates import canonical_mint
from ..solana_meme.sellability import (
    SolanaSellQuoteObservation, WRAPPED_SOL_MINT,
)


class JupiterSwapV2QuoteProvider:
    provider_id = "jupiter-swap-v2-order"
    endpoint = "https://api.jup.ag/swap/v2/order"

    def __init__(
        self,
        *,
        api_key: str = "",
        session: Any | None = None,
        timeout_seconds: float = 12.0,
        minimum_request_interval_seconds: float = 2.1,
        quote_ttl_ms: int = 5_000,
        clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if (isinstance(minimum_request_interval_seconds, bool)
                or not math.isfinite(minimum_request_interval_seconds)
                or minimum_request_interval_seconds < 2.0):
            raise ValueError("keyless request interval must be at least 2 seconds")
        if isinstance(quote_ttl_ms, bool) or not isinstance(quote_ttl_ms, int) or quote_ttl_ms <= 0:
            raise ValueError("quote_ttl_ms must be a positive integer")
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.session = session or requests
        self.timeout_seconds = timeout_seconds
        self.minimum_request_interval_seconds = minimum_request_interval_seconds
        self.quote_ttl_ms = quote_ttl_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._last_request_started: float | None = None

    def _throttle(self) -> None:
        with self._lock:
            now = self.monotonic()
            if self._last_request_started is not None:
                wait = self.minimum_request_interval_seconds - (now - self._last_request_started)
                if wait > 0:
                    self.sleep(wait)
                    now = self.monotonic()
            self._last_request_started = now

    @staticmethod
    def _raw_amount(value: object, field: str) -> int:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ProviderError(f"Jupiter {field} is missing or malformed")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"Jupiter {field} is malformed") from exc
        if result <= 0:
            raise ProviderError(f"Jupiter {field} must be positive")
        return result

    @staticmethod
    def _price_impact_bps(body: dict) -> float:
        # Swap V2 expresses priceImpact in percentage points; legacy
        # priceImpactPct is a decimal ratio. Prefer the current V2 field.
        if "priceImpact" in body:
            raw = body["priceImpact"]
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ProviderError("Jupiter priceImpact is malformed") from exc
            bps = abs(value) * 100.0
        else:
            raw = body.get("priceImpactPct")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ProviderError("Jupiter price impact is missing or malformed") from exc
            bps = abs(value) * 10_000.0
        if not math.isfinite(bps):
            raise ProviderError("Jupiter price impact is non-finite")
        return bps

    def fetch_sell_quote(
        self, mint: str, input_amount_raw: int,
    ) -> SolanaSellQuoteObservation:
        canonical = canonical_mint(mint)
        if (isinstance(input_amount_raw, bool) or not isinstance(input_amount_raw, int)
                or input_amount_raw <= 0):
            raise ValueError("input_amount_raw must be a positive integer")
        params = {
            "inputMint": canonical,
            "outputMint": WRAPPED_SOL_MINT,
            "amount": str(input_amount_raw),
            "swapMode": "ExactIn",
        }
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        self._throttle()
        try:
            response = self.session.get(
                self.endpoint, params=params, headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise ProviderError(
                f"Jupiter Swap V2 quote request failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(body, dict) or body.get("error") or body.get("errorCode") is not None:
            raise ProviderError("Jupiter Swap V2 returned an error quote")
        if body.get("inputMint") != canonical or body.get("outputMint") != WRAPPED_SOL_MINT:
            raise ProviderError("Jupiter Swap V2 returned a mismatched token pair")
        in_amount = self._raw_amount(body.get("inAmount"), "inAmount")
        if in_amount != input_amount_raw:
            raise ProviderError("Jupiter Swap V2 returned a mismatched input amount")
        route_plan = body.get("routePlan")
        if not isinstance(route_plan, list):
            raise ProviderError("Jupiter Swap V2 routePlan is malformed")
        observed = self.clock_ms()
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise ProviderError("local quote timestamp is invalid")
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ProviderError("Jupiter Swap V2 quote provenance is missing")

        if not route_plan:
            return SolanaSellQuoteObservation(
                provider=self.provider_id, evidence_id=request_id,
                mint=canonical, output_mint=WRAPPED_SOL_MINT,
                input_amount_raw=in_amount, quoted_output_raw=None,
                minimum_output_raw=None, route_plan_count=0,
                price_impact_bps=None, observed_at_ms=observed,
                expires_at_ms=observed + self.quote_ttl_ms,
            )

        output = self._raw_amount(body.get("outAmount"), "outAmount")
        minimum = self._raw_amount(
            body.get("otherAmountThreshold"), "otherAmountThreshold",
        )
        impact = self._price_impact_bps(body)
        return SolanaSellQuoteObservation(
            provider=self.provider_id, evidence_id=request_id,
            mint=canonical, output_mint=WRAPPED_SOL_MINT,
            input_amount_raw=in_amount, quoted_output_raw=output,
            minimum_output_raw=minimum, route_plan_count=len(route_plan),
            price_impact_bps=impact, observed_at_ms=observed,
            expires_at_ms=observed + self.quote_ttl_ms,
        )
