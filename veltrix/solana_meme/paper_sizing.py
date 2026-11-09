"""Exact raw-token sizing for Solana paper-exit sell quotes.

The calculation uses Decimal arithmetic and rounds token units down. It never
places an order or implies that a route can execute.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math

from .candidates import canonical_mint


@dataclass(frozen=True)
class SolanaPaperExitSize:
    mint: str
    notional_usd: float
    token_price_usd: float
    decimals: int
    total_supply_raw: int
    input_amount_raw: int
    supply_fraction: float
    effective_notional_usd: float


def _positive_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive finite number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return parsed


def calculate_paper_exit_size(
    mint: str,
    *,
    notional_usd: float,
    token_price_usd: float,
    decimals: int,
    total_supply_raw: int,
    min_supply_fraction: float = 0.000001,
    max_supply_fraction: float = 0.05,
) -> SolanaPaperExitSize:
    """Convert a USD paper position into an exact raw-token sell amount.

    The result is bounded by finalized raw supply. Decimal rounding is always
    downward so the quote request cannot exceed the requested notional.
    """
    canonical = canonical_mint(mint)
    notional = _positive_decimal(notional_usd, "notional_usd")
    price = _positive_decimal(token_price_usd, "token_price_usd")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 38:
        raise ValueError("decimals must be an integer between 0 and 38")
    if (isinstance(total_supply_raw, bool) or not isinstance(total_supply_raw, int)
            or total_supply_raw <= 0):
        raise ValueError("total_supply_raw must be a positive integer")
    minimum = _positive_decimal(min_supply_fraction, "min_supply_fraction")
    maximum = _positive_decimal(max_supply_fraction, "max_supply_fraction")
    if minimum > maximum or maximum > 1:
        raise ValueError("supply-fraction bounds must satisfy 0 < min <= max <= 1")

    scale = Decimal(10) ** decimals
    raw = int(((notional / price) * scale).to_integral_value(rounding=ROUND_DOWN))
    if raw <= 0:
        raise ValueError("requested notional is below one raw token unit")
    if raw > total_supply_raw:
        raise ValueError("paper exit amount exceeds total token supply")
    fraction = Decimal(raw) / Decimal(total_supply_raw)
    if fraction < minimum or fraction > maximum:
        raise ValueError("paper exit amount is outside configured supply-fraction bounds")
    effective_notional = (Decimal(raw) / scale) * price
    return SolanaPaperExitSize(
        mint=canonical,
        notional_usd=float(notional),
        token_price_usd=float(price),
        decimals=decimals,
        total_supply_raw=total_supply_raw,
        input_amount_raw=raw,
        supply_fraction=float(fraction),
        effective_notional_usd=float(effective_notional),
    )
