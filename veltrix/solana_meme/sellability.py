"""Read-only Solana sell-route evidence; never certifies executable sellability.

A successful aggregator quote is only evidence that a route was observed at one
point in time. It is not an on-chain simulation, transfer test, or proof that a
transaction can land and settle.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Protocol, Callable

from .candidates import canonical_mint

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class SolanaSellQuoteObservation:
    """Normalized exact-in quote for selling one token mint into wrapped SOL."""

    provider: str
    evidence_id: str
    mint: str
    output_mint: str
    input_amount_raw: int
    quoted_output_raw: int | None
    minimum_output_raw: int | None
    route_plan_count: int
    price_impact_bps: float | None
    observed_at_ms: int
    expires_at_ms: int


class SellQuoteProvider(Protocol):
    provider_id: str

    def fetch_sell_quote(self, mint: str, input_amount_raw: int) -> SolanaSellQuoteObservation:
        ...


@dataclass(frozen=True)
class SolanaSellabilityEvidence:
    mint: str
    observed_at_ms: int
    provider: str | None
    evidence_id: str | None
    input_amount_raw: int
    input_supply_fraction: float | None
    quoted_output_raw: int | None
    minimum_output_raw: int | None
    route_plan_count: int
    price_impact_bps: float | None
    quote_expires_at_ms: int | None
    state: str
    errors: tuple[str, ...]
    execution_proven: bool = False

    @property
    def route_observed(self) -> bool:
        return self.state == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"


class SolanaSellabilityEvaluator:
    """Validate a bounded sell-side route observation without signing or sending."""

    def __init__(
        self,
        provider: SellQuoteProvider,
        *,
        max_age_ms: int = 15_000,
        min_supply_fraction: float = 0.005,
        max_supply_fraction: float = 0.05,
        max_price_impact_bps: float = 300.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")
        if (not math.isfinite(min_supply_fraction)
                or not math.isfinite(max_supply_fraction)
                or not 0 < min_supply_fraction <= max_supply_fraction <= 1):
            raise ValueError("supply fractions must satisfy 0 < min <= max <= 1")
        if not math.isfinite(max_price_impact_bps) or max_price_impact_bps <= 0:
            raise ValueError("max_price_impact_bps must be finite and positive")
        self.provider = provider
        self.max_age_ms = max_age_ms
        self.min_supply_fraction = min_supply_fraction
        self.max_supply_fraction = max_supply_fraction
        self.max_price_impact_bps = max_price_impact_bps
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _positive_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return value

    def evaluate(
        self,
        mint: str,
        *,
        total_supply_raw: int,
        input_amount_raw: int,
        now_ms: int | None = None,
    ) -> SolanaSellabilityEvidence:
        canonical = canonical_mint(mint)
        supply = self._positive_int(total_supply_raw, "total_supply_raw")
        amount = self._positive_int(input_amount_raw, "input_amount_raw")
        if amount > supply:
            raise ValueError("sell quote amount cannot exceed total supply")
        fraction = amount / supply
        now = self.clock_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_ms must be a non-negative integer")

        errors: list[str] = []
        observation = None
        try:
            candidate = self.provider.fetch_sell_quote(canonical, amount)
            if not isinstance(candidate, SolanaSellQuoteObservation):
                raise ValueError("provider returned an untyped quote")
            if candidate.provider != getattr(self.provider, "provider_id", None):
                raise ValueError("provider identity mismatch")
            if not isinstance(candidate.evidence_id, str) or not candidate.evidence_id.strip():
                raise ValueError("quote provenance is missing")
            if canonical_mint(candidate.mint) != canonical:
                raise ValueError("quote input mint mismatch")
            if canonical_mint(candidate.output_mint) != WRAPPED_SOL_MINT:
                raise ValueError("quote output is not wrapped SOL")
            if self._positive_int(candidate.input_amount_raw, "quote input amount") != amount:
                raise ValueError("quote input amount mismatch")
            for field in ("observed_at_ms", "expires_at_ms", "route_plan_count"):
                value = getattr(candidate, field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{field} is invalid")
            if candidate.observed_at_ms > now or now - candidate.observed_at_ms > self.max_age_ms:
                raise ValueError("sell quote is stale or future-dated")
            if candidate.expires_at_ms <= now or candidate.expires_at_ms <= candidate.observed_at_ms:
                raise ValueError("sell quote has expired or invalid expiry")
            if candidate.route_plan_count == 0:
                return SolanaSellabilityEvidence(
                    canonical, now, candidate.provider, candidate.evidence_id,
                    amount, fraction, None, None, 0, None,
                    candidate.expires_at_ms, "NO_SELL_ROUTE_OBSERVED",
                    ("provider returned no sell route",),
                )
            quoted = self._positive_int(candidate.quoted_output_raw, "quoted output")
            minimum = self._positive_int(candidate.minimum_output_raw, "minimum output")
            if minimum > quoted:
                raise ValueError("minimum output exceeds quoted output")
            impact = candidate.price_impact_bps
            if (isinstance(impact, bool) or not isinstance(impact, (int, float))
                    or not math.isfinite(impact) or impact < 0):
                raise ValueError("price impact is invalid")
            if not self.min_supply_fraction <= fraction <= self.max_supply_fraction:
                raise ValueError("sell quote size is outside the configured supply-fraction band")
            if impact > self.max_price_impact_bps:
                raise ValueError("sell quote price impact exceeds configured maximum")
            observation = candidate
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:240]}")

        if observation is None:
            return SolanaSellabilityEvidence(
                canonical, now, None, None, amount, fraction, None, None, 0,
                None, None, "UNVERIFIED", tuple(errors),
            )
        return SolanaSellabilityEvidence(
            canonical, now, observation.provider, observation.evidence_id,
            amount, fraction, observation.quoted_output_raw,
            observation.minimum_output_raw, observation.route_plan_count,
            float(observation.price_impact_bps), observation.expires_at_ms,
            "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN", (),
        )
