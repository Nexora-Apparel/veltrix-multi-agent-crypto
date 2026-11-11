"""Raydium CLMM post-price-step tick assignment and dynamic-fee index advance."""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeError,
    RaydiumDynamicFeeSwapState,
    advance_dynamic_fee_index,
)
from veltrix.raydium_clmm_swap_price_step import RaydiumClmmSwapPriceStep
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import (
    RaydiumTickMathError,
    get_sqrt_price_at_tick,
    get_tick_at_sqrt_price,
)


class RaydiumSwapTickPriceFinalizationError(ValueError):
    """Raised when a Raydium swap iteration cannot finalize tick/price state."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapTickPriceState:
    tick: int
    sqrt_price_x64: int
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None


def finalize_raydium_swap_tick_price(
    price_step: RaydiumClmmSwapPriceStep,
    *,
    current_tick: int,
    next_tick: int,
    sqrt_price_next_x64: int,
    updated_tick_state: RaydiumClmmTick,
    reached_initialized_tick: bool,
    zero_for_one: bool,
) -> RaydiumSwapTickPriceState:
    """Match swap.rs:744-778 after the boundary's limit-order/crossing operations."""
    if not isinstance(price_step, RaydiumClmmSwapPriceStep):
        raise RaydiumSwapTickPriceFinalizationError("completed Raydium price step is required")
    if not isinstance(updated_tick_state, RaydiumClmmTick):
        raise RaydiumSwapTickPriceFinalizationError("updated initialized TickState is required")
    if not isinstance(reached_initialized_tick, bool) or not isinstance(zero_for_one, bool):
        raise RaydiumSwapTickPriceFinalizationError("boundary and swap direction flags must be boolean")
    for value, name in ((current_tick, "current tick"), (next_tick, "next tick")):
        if isinstance(value, bool) or not isinstance(value, int) or not -(1 << 31) <= value < (1 << 31):
            raise RaydiumSwapTickPriceFinalizationError(f"{name} must fit i32")
    if isinstance(sqrt_price_next_x64, bool) or not isinstance(sqrt_price_next_x64, int) or not 0 < sqrt_price_next_x64 < (1 << 128):
        raise RaydiumSwapTickPriceFinalizationError("next tick sqrt price must fit positive u128")
    try:
        canonical_next_price = get_sqrt_price_at_tick(next_tick)
    except RaydiumTickMathError as exc:
        raise RaydiumSwapTickPriceFinalizationError(f"next tick is invalid: {exc}") from exc
    if canonical_next_price != sqrt_price_next_x64:
        raise RaydiumSwapTickPriceFinalizationError("next tick sqrt price differs from canonical tick math")

    sqrt_price = price_step.swap_result.sqrt_price_next_x64
    if reached_initialized_tick:
        if sqrt_price != sqrt_price_next_x64:
            raise RaydiumSwapTickPriceFinalizationError("initialized tick was marked reached at a different sqrt price")
        has_limit_orders = (
            updated_tick_state.orders_amount > 0
            or updated_tick_state.part_filled_orders_remaining > 0
        )
        if (zero_for_one and not has_limit_orders) or (not zero_for_one and has_limit_orders):
            tick = next_tick - 1
        else:
            tick = next_tick
    elif price_step.price_changed:
        if (
            price_step.spacing_bound.bounded_tick is not None
            and sqrt_price == price_step.spacing_bound.sqrt_price_x64
        ):
            tick = price_step.spacing_bound.bounded_tick
        else:
            try:
                tick = get_tick_at_sqrt_price(sqrt_price)
            except RaydiumTickMathError as exc:
                raise RaydiumSwapTickPriceFinalizationError(f"updated sqrt price has no valid tick: {exc}") from exc
    else:
        tick = current_tick

    dynamic_state = price_step.dynamic_fee_state
    if dynamic_state is not None:
        try:
            dynamic_state = advance_dynamic_fee_index(
                dynamic_state,
                tick=tick,
                tick_next=next_tick,
                sqrt_price_x64=sqrt_price,
                sqrt_price_next_x64=sqrt_price_next_x64,
                zero_for_one=zero_for_one,
                is_skipped_tick_spacing=price_step.spacing_bound.reached_target,
            )
        except RaydiumDynamicFeeError as exc:
            raise RaydiumSwapTickPriceFinalizationError(f"dynamic-fee index advance failed: {exc}") from exc

    return RaydiumSwapTickPriceState(
        tick=tick,
        sqrt_price_x64=sqrt_price,
        dynamic_fee_state=dynamic_state,
    )
