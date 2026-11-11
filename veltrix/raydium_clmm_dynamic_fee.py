"""Pinned Raydium CLMM dynamic-fee state transitions and fee calculation."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_DYNAMIC_FEE_CONTROL_DENOMINATOR,
    RAYDIUM_DYNAMIC_FEE_REDUCTION_DENOMINATOR,
    RAYDIUM_FEE_RATE_DENOMINATOR,
    RaydiumClmmDynamicFeeInfo,
)
from veltrix.raydium_clmm_tick_math import (
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    RAYDIUM_MAX_SQRT_PRICE_X64,
    RAYDIUM_MIN_SQRT_PRICE_X64,
    get_sqrt_price_at_tick,
)


MAX_FEE_RATE_NUMERATOR = 100_000
VOLATILITY_ACCUMULATOR_SCALE = 10_000


class RaydiumDynamicFeeError(ValueError):
    """Raised for invalid or overflowing pinned dynamic-fee state."""


@dataclass(frozen=True, slots=True)
class RaydiumSpacingBound:
    fully_reached_target: bool
    target_sqrt_price_x64: int
    bounded_tick: int | None


def _u32(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise RaydiumDynamicFeeError(f"{name} must fit u32")
    return value


def tick_spacing_index_from_tick(tick_index: int, tick_spacing: int) -> int:
    if isinstance(tick_index, bool) or not isinstance(tick_index, int) or not -(1 << 31) <= tick_index < (1 << 31):
        raise RaydiumDynamicFeeError("tick_index must fit i32")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumDynamicFeeError("tick_spacing must fit non-zero u16")
    quotient = abs(tick_index) // tick_spacing
    if tick_index < 0:
        quotient = -quotient
        if tick_index % tick_spacing != 0:
            quotient -= 1
        return quotient
    return quotient


def update_dynamic_fee_reference(
    info: RaydiumClmmDynamicFeeInfo,
    *,
    tick_spacing_index: int,
    current_timestamp: int,
) -> RaydiumClmmDynamicFeeInfo:
    if not isinstance(info, RaydiumClmmDynamicFeeInfo) or not info.enabled:
        raise RaydiumDynamicFeeError("enabled DynamicFeeInfo is required")
    if isinstance(tick_spacing_index, bool) or not isinstance(tick_spacing_index, int) or not -(1 << 31) <= tick_spacing_index < (1 << 31):
        raise RaydiumDynamicFeeError("tick_spacing_index must fit i32")
    if isinstance(current_timestamp, bool) or not isinstance(current_timestamp, int) or not 0 <= current_timestamp <= 0xFFFFFFFFFFFFFFFF:
        raise RaydiumDynamicFeeError("current_timestamp must fit u64")
    elapsed = max(0, current_timestamp - info.last_update_timestamp)
    if elapsed < info.filter_period:
        return info
    if elapsed < info.decay_period:
        return replace(
            info,
            tick_spacing_index_reference=tick_spacing_index,
            volatility_reference=(
                info.volatility_accumulator * info.reduction_factor
            ) // RAYDIUM_DYNAMIC_FEE_REDUCTION_DENOMINATOR,
            last_update_timestamp=current_timestamp,
        )
    return replace(
        info,
        tick_spacing_index_reference=tick_spacing_index,
        volatility_reference=0,
        last_update_timestamp=current_timestamp,
    )


def update_volatility_accumulator(
    info: RaydiumClmmDynamicFeeInfo,
    *,
    tick_spacing_index: int,
) -> RaydiumClmmDynamicFeeInfo:
    if not isinstance(info, RaydiumClmmDynamicFeeInfo) or not info.enabled:
        raise RaydiumDynamicFeeError("enabled DynamicFeeInfo is required")
    if isinstance(tick_spacing_index, bool) or not isinstance(tick_spacing_index, int) or not -(1 << 31) <= tick_spacing_index < (1 << 31):
        raise RaydiumDynamicFeeError("tick_spacing_index must fit i32")
    index_delta = abs(info.tick_spacing_index_reference - tick_spacing_index)
    raw = info.volatility_reference + index_delta * VOLATILITY_ACCUMULATOR_SCALE
    return replace(
        info,
        volatility_accumulator=min(raw, info.max_volatility_accumulator),
    )


def compute_dynamic_fee_rate(
    info: RaydiumClmmDynamicFeeInfo,
    *,
    tick_spacing: int,
) -> int:
    if not isinstance(info, RaydiumClmmDynamicFeeInfo) or not info.enabled:
        raise RaydiumDynamicFeeError("enabled DynamicFeeInfo is required")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumDynamicFeeError("tick_spacing must fit non-zero u16")
    crossed = info.volatility_accumulator * tick_spacing
    if crossed > 0xFFFFFFFF:
        raise RaydiumDynamicFeeError("volatility accumulator multiplied by tick spacing overflows u32")
    squared = crossed * crossed
    denominator = (
        RAYDIUM_DYNAMIC_FEE_CONTROL_DENOMINATOR
        * VOLATILITY_ACCUMULATOR_SCALE
        * VOLATILITY_ACCUMULATOR_SCALE
    )
    numerator = info.dynamic_fee_control * squared
    dynamic_rate = (numerator + denominator - 1) // denominator
    return min(dynamic_rate, MAX_FEE_RATE_NUMERATOR)


def get_total_fee_rate(
    base_fee_rate: int,
    *,
    dynamic_fee_info: RaydiumClmmDynamicFeeInfo,
    tick_spacing: int,
) -> int:
    base_fee_rate = _u32(base_fee_rate, "base_fee_rate")
    if base_fee_rate >= RAYDIUM_FEE_RATE_DENOMINATOR:
        raise RaydiumDynamicFeeError("base fee rate exceeds the fee denominator")
    if not isinstance(dynamic_fee_info, RaydiumClmmDynamicFeeInfo):
        raise RaydiumDynamicFeeError("DynamicFeeInfo is required")
    if not dynamic_fee_info.enabled:
        return base_fee_rate
    return min(
        base_fee_rate + compute_dynamic_fee_rate(dynamic_fee_info, tick_spacing=tick_spacing),
        MAX_FEE_RATE_NUMERATOR,
    )


def update_dynamic_fee_index(
    info: RaydiumClmmDynamicFeeInfo,
    *,
    current_index: int,
    tick: int,
    tick_next: int,
    sqrt_price_x64: int,
    sqrt_price_next_x64: int,
    tick_spacing: int,
    zero_for_one: bool,
    is_skipped_tick_spacing: bool,
) -> tuple[RaydiumClmmDynamicFeeInfo, int]:
    if not isinstance(info, RaydiumClmmDynamicFeeInfo) or not info.enabled:
        raise RaydiumDynamicFeeError("enabled DynamicFeeInfo is required")
    if not isinstance(zero_for_one, bool) or not isinstance(is_skipped_tick_spacing, bool):
        raise RaydiumDynamicFeeError("swap direction and skipped-spacing flags must be booleans")
    if isinstance(current_index, bool) or not isinstance(current_index, int) or not -(1 << 31) <= current_index < (1 << 31):
        raise RaydiumDynamicFeeError("current_index must fit i32")
    if isinstance(tick, bool) or not isinstance(tick, int) or not -(1 << 31) <= tick < (1 << 31):
        raise RaydiumDynamicFeeError("tick must fit i32")
    if isinstance(tick_next, bool) or not isinstance(tick_next, int) or not -(1 << 31) <= tick_next < (1 << 31):
        raise RaydiumDynamicFeeError("tick_next must fit i32")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumDynamicFeeError("tick_spacing must fit non-zero u16")
    next_info = info
    next_index = current_index
    if is_skipped_tick_spacing:
        tick_index = tick_next if sqrt_price_x64 == sqrt_price_next_x64 else tick
        next_index = tick_spacing_index_from_tick(tick_index, tick_spacing)
        if not zero_for_one and tick_index % tick_spacing == 0:
            next_index -= 1
        if info.volatility_accumulator != info.max_volatility_accumulator:
            next_info = update_volatility_accumulator(info, tick_spacing_index=next_index)
    next_index += -1 if zero_for_one else 1
    if not -0x80000000 <= next_index <= 0x7FFFFFFF:
        raise RaydiumDynamicFeeError("dynamic fee tick-spacing index overflows i32")
    return next_info, next_index


def get_spacing_bounded_price(
    *,
    target_price_x64: int,
    tick_spacing_index: int,
    tick_spacing: int,
    liquidity: int,
    dynamic_fee_info: RaydiumClmmDynamicFeeInfo,
    zero_for_one: bool,
) -> RaydiumSpacingBound:
    if isinstance(zero_for_one, bool) is False:
        raise RaydiumDynamicFeeError("zero_for_one must be a boolean")
    if isinstance(target_price_x64, bool) or not isinstance(target_price_x64, int) or not RAYDIUM_MIN_SQRT_PRICE_X64 <= target_price_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumDynamicFeeError("target price is outside the pinned Raydium sqrt-price range")
    if isinstance(liquidity, bool) or not isinstance(liquidity, int) or liquidity < 0:
        raise RaydiumDynamicFeeError("liquidity must be a non-negative integer")
    if isinstance(tick_spacing_index, bool) or not isinstance(tick_spacing_index, int) or not -(1 << 31) <= tick_spacing_index < (1 << 31):
        raise RaydiumDynamicFeeError("tick_spacing_index must fit i32")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumDynamicFeeError("tick_spacing must fit non-zero u16")
    if not isinstance(dynamic_fee_info, RaydiumClmmDynamicFeeInfo):
        raise RaydiumDynamicFeeError("DynamicFeeInfo is required")
    if not dynamic_fee_info.enabled or liquidity == 0 or (
        dynamic_fee_info.volatility_accumulator == dynamic_fee_info.max_volatility_accumulator
    ):
        return RaydiumSpacingBound(True, target_price_x64, None)
    bounded_tick = (
        tick_spacing_index * tick_spacing
        if zero_for_one else (tick_spacing_index + 1) * tick_spacing
    )
    bounded_tick = max(RAYDIUM_MIN_TICK, min(RAYDIUM_MAX_TICK, bounded_tick))
    bounded_price = get_sqrt_price_at_tick(bounded_tick)
    if zero_for_one:
        if target_price_x64 > bounded_price:
            return RaydiumSpacingBound(False, target_price_x64, None)
    else:
        if target_price_x64 < bounded_price:
            return RaydiumSpacingBound(False, target_price_x64, None)
    return RaydiumSpacingBound(False, bounded_price, bounded_tick)
