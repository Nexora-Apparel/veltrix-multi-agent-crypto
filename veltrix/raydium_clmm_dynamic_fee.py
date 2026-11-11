"""Integer-exact Raydium CLMM dynamic-fee state transitions.

Ported from pool_fee.rs and instructions/swap.rs at the pinned source commit.
This module models fee-state arithmetic only; it does not replay a complete swap.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_FEE_RATE_DENOMINATOR,
    RaydiumClmmDynamicFeeInfo,
)
from veltrix.raydium_clmm_tick_math import (
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    get_sqrt_price_at_tick,
)


SOURCE_COMMIT = RAYDIUM_CLMM_SOURCE_COMMIT
VOLATILITY_ACCUMULATOR_SCALE = 10_000
REDUCTION_FACTOR_DENOMINATOR = 10_000
DYNAMIC_FEE_CONTROL_DENOMINATOR = 100_000
MAX_FEE_RATE_NUMERATOR = 100_000
U16_MAX = (1 << 16) - 1
U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1


class RaydiumDynamicFeeError(ValueError):
    """Raised when a dynamic-fee state or transition is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumDynamicFeeSwapState:
    info: RaydiumClmmDynamicFeeInfo
    tick_spacing: int
    base_fee_rate: int
    tick_spacing_index: int


@dataclass(frozen=True, slots=True)
class RaydiumSpacingBound:
    reached_target: bool
    sqrt_price_x64: int
    bounded_tick: int | None


def _integer(value: int, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RaydiumDynamicFeeError(f"{name} must be an integer in [0, {maximum}]")
    return value


def tick_spacing_index_from_tick(tick_index: int, tick_spacing: int) -> int:
    if isinstance(tick_index, bool) or not isinstance(tick_index, int) or not -(1 << 31) <= tick_index < (1 << 31):
        raise RaydiumDynamicFeeError("tick index must fit i32")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= U16_MAX:
        raise RaydiumDynamicFeeError("tick spacing must be a positive u16")
    # Rust signed integer division truncates toward zero, unlike Python //.
    quotient = abs(tick_index) // tick_spacing
    if tick_index < 0:
        quotient = -quotient
        if tick_index % tick_spacing:
            quotient -= 1
    return quotient


def update_dynamic_fee_reference(
    info: RaydiumClmmDynamicFeeInfo,
    *,
    tick_spacing_index: int,
    current_timestamp: int,
) -> RaydiumClmmDynamicFeeInfo:
    if not isinstance(info, RaydiumClmmDynamicFeeInfo) or not info.enabled:
        raise RaydiumDynamicFeeError("enabled Raydium dynamic-fee information is required")
    if isinstance(tick_spacing_index, bool) or not isinstance(tick_spacing_index, int) or not -(1 << 31) <= tick_spacing_index < (1 << 31):
        raise RaydiumDynamicFeeError("tick-spacing index must fit i32")
    _integer(current_timestamp, "current timestamp", U64_MAX)
    elapsed = max(0, current_timestamp - info.last_update_timestamp)
    if elapsed < info.filter_period:
        return info
    if elapsed < info.decay_period:
        return replace(
            info,
            tick_spacing_index_reference=tick_spacing_index,
            volatility_reference=(info.volatility_accumulator * info.reduction_factor)
                // REDUCTION_FACTOR_DENOMINATOR,
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
        raise RaydiumDynamicFeeError("enabled Raydium dynamic-fee information is required")
    if isinstance(tick_spacing_index, bool) or not isinstance(tick_spacing_index, int) or not -(1 << 31) <= tick_spacing_index < (1 << 31):
        raise RaydiumDynamicFeeError("tick-spacing index must fit i32")
    delta = abs(info.tick_spacing_index_reference - tick_spacing_index)
    accumulator = min(
        info.volatility_reference + delta * VOLATILITY_ACCUMULATOR_SCALE,
        info.max_volatility_accumulator,
    )
    return replace(info, volatility_accumulator=accumulator)


def compute_dynamic_fee_rate(
    info: RaydiumClmmDynamicFeeInfo,
    *,
    tick_spacing: int,
) -> int:
    if not isinstance(info, RaydiumClmmDynamicFeeInfo) or not info.enabled:
        raise RaydiumDynamicFeeError("enabled Raydium dynamic-fee information is required")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= U16_MAX:
        raise RaydiumDynamicFeeError("tick spacing must be a positive u16")
    crossed = info.volatility_accumulator * tick_spacing
    if crossed > U32_MAX:
        raise RaydiumDynamicFeeError("volatility accumulator multiplication exceeds pinned u32 bound")
    squared = crossed * crossed
    numerator = info.dynamic_fee_control * squared
    denominator = (
        DYNAMIC_FEE_CONTROL_DENOMINATOR
        * VOLATILITY_ACCUMULATOR_SCALE
        * VOLATILITY_ACCUMULATOR_SCALE
    )
    fee_rate = (numerator + denominator - 1) // denominator
    return min(fee_rate, MAX_FEE_RATE_NUMERATOR)


def compute_total_fee_rate(
    *,
    base_fee_rate: int,
    info: RaydiumClmmDynamicFeeInfo,
    tick_spacing: int,
) -> int:
    _integer(base_fee_rate, "base fee rate", RAYDIUM_FEE_RATE_DENOMINATOR - 1)
    if info.enabled:
        return min(
            base_fee_rate + compute_dynamic_fee_rate(info, tick_spacing=tick_spacing),
            MAX_FEE_RATE_NUMERATOR,
        )
    return base_fee_rate


def initialize_dynamic_fee_swap_state(
    *,
    info: RaydiumClmmDynamicFeeInfo,
    tick: int,
    tick_spacing: int,
    base_fee_rate: int,
    block_timestamp: int,
) -> RaydiumDynamicFeeSwapState:
    if not info.enabled:
        raise RaydiumDynamicFeeError("dynamic-fee swap state cannot be initialized from disabled configuration")
    _integer(base_fee_rate, "base fee rate", RAYDIUM_FEE_RATE_DENOMINATOR - 1)
    index = tick_spacing_index_from_tick(tick, tick_spacing)
    updated = update_dynamic_fee_reference(
        info, tick_spacing_index=index, current_timestamp=block_timestamp
    )
    return RaydiumDynamicFeeSwapState(updated, tick_spacing, base_fee_rate, index)


def advance_dynamic_fee_index(
    state: RaydiumDynamicFeeSwapState,
    *,
    tick: int,
    tick_next: int,
    sqrt_price_x64: int,
    sqrt_price_next_x64: int,
    zero_for_one: bool,
    is_skipped_tick_spacing: bool,
) -> RaydiumDynamicFeeSwapState:
    if not isinstance(state, RaydiumDynamicFeeSwapState):
        raise RaydiumDynamicFeeError("Raydium dynamic-fee swap state is required")
    if not isinstance(zero_for_one, bool) or not isinstance(is_skipped_tick_spacing, bool):
        raise RaydiumDynamicFeeError("swap direction and skipped-spacing flags must be booleans")
    for value, name in ((tick, "tick"), (tick_next, "next tick")):
        if isinstance(value, bool) or not isinstance(value, int) or not RAYDIUM_MIN_TICK <= value <= RAYDIUM_MAX_TICK:
            raise RaydiumDynamicFeeError(f"{name} is outside the Raydium tick range")
    _integer(sqrt_price_x64, "sqrt price", (1 << 128) - 1)
    _integer(sqrt_price_next_x64, "next sqrt price", (1 << 128) - 1)
    index = state.tick_spacing_index
    info = state.info
    if is_skipped_tick_spacing:
        tick_index = tick_next if sqrt_price_x64 == sqrt_price_next_x64 else tick
        index = tick_spacing_index_from_tick(tick_index, state.tick_spacing)
        if not zero_for_one and tick_index % state.tick_spacing == 0:
            index -= 1
        if info.volatility_accumulator != info.max_volatility_accumulator:
            info = update_volatility_accumulator(info, tick_spacing_index=index)
    index += -1 if zero_for_one else 1
    if not -(1 << 31) <= index < (1 << 31):
        raise RaydiumDynamicFeeError("advanced tick-spacing index overflows i32")
    return RaydiumDynamicFeeSwapState(info, state.tick_spacing, state.base_fee_rate, index)


def get_spacing_bounded_price(
    state: RaydiumDynamicFeeSwapState,
    *,
    target_price_x64: int,
    zero_for_one: bool,
    liquidity: int,
) -> RaydiumSpacingBound:
    if not isinstance(state, RaydiumDynamicFeeSwapState):
        raise RaydiumDynamicFeeError("Raydium dynamic-fee swap state is required")
    if not isinstance(zero_for_one, bool):
        raise RaydiumDynamicFeeError("swap direction must be boolean")
    _integer(target_price_x64, "target sqrt price", (1 << 128) - 1)
    _integer(liquidity, "liquidity", (1 << 128) - 1)
    info = state.info
    if liquidity == 0 or info.volatility_accumulator == info.max_volatility_accumulator:
        return RaydiumSpacingBound(True, target_price_x64, None)
    spacing = state.tick_spacing
    bounded_tick = (
        state.tick_spacing_index * spacing
        if zero_for_one
        else (state.tick_spacing_index + 1) * spacing
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
