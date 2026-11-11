"""One source-ordered Raydium CLMM inner-loop iteration.

Composes swap.rs:633-793 from independently tested primitives. The caller owns
the outer tick-array loop and commits TickArrayState only after the boundary
transition has completed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_dynamic_fee import RaydiumDynamicFeeSwapState
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_limit_orders import RaydiumLimitOrderMatch
from veltrix.raydium_clmm_limit_order_boundary import (
    RaydiumLimitOrderBoundaryError,
    apply_raydium_limit_order_boundary,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_price_step import (
    RaydiumClmmSwapPriceStep,
    RaydiumSwapPriceStepError,
    compute_raydium_clmm_swap_price_step,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_boundary_crossing import (
    RaydiumTickBoundaryCrossingError,
    cross_raydium_clmm_tick_at_boundary,
)
from veltrix.raydium_clmm_swap_tick_price_finalization import (
    RaydiumSwapTickPriceFinalizationError,
    finalize_raydium_swap_tick_price,
)


class RaydiumClmmSwapInnerIterationError(ValueError):
    """Raised when a composed Raydium inner-loop iteration violates source invariants."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapInnerIterationResult:
    fee_state: RaydiumFeeAccountingState
    tick_state: RaydiumClmmTick
    tick: int
    sqrt_price_x64: int
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None
    liquidity_next: int
    price_step: RaydiumClmmSwapPriceStep
    limit_order_match: RaydiumLimitOrderMatch
    unfilled_amount_before: int
    unfilled_amount_after: int
    reached_initialized_tick: bool
    crossed_tick: bool
    should_break: bool


def _unfilled_amount(tick: RaydiumClmmTick) -> int:
    values = (tick.orders_amount, tick.part_filled_orders_remaining)
    if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 64) for value in values):
        raise RaydiumClmmSwapInnerIterationError("tick limit-order amounts must fit u64")
    total = sum(values)
    if total >= (1 << 64):
        raise RaydiumClmmSwapInnerIterationError("tick unfilled limit-order amount overflows u64")
    return total


def _is_fee_on_input(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return True
    if pool.fee_on == 1:
        return zero_for_one
    if pool.fee_on == 2:
        return not zero_for_one
    return True


def run_raydium_clmm_swap_inner_iteration(
    pool: RaydiumClmmPoolState,
    fee_state: RaydiumFeeAccountingState,
    *,
    current_tick: int,
    next_tick: int,
    next_tick_state: RaydiumClmmTick,
    sqrt_price_current_x64: int,
    sqrt_price_next_x64: int,
    target_price_x64: int,
    base_fee_rate: int,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    zero_for_one: bool,
    is_base_input: bool,
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None = None,
) -> RaydiumClmmSwapInnerIterationResult:
    """Run one inner-loop iteration in the pinned source operation order."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmSwapInnerIterationError("decoded Raydium PoolState is required")
    if not isinstance(fee_state, RaydiumFeeAccountingState):
        raise RaydiumClmmSwapInnerIterationError("Raydium fee-accounting state is required")
    if not isinstance(next_tick_state, RaydiumClmmTick) or next_tick_state.initialized is not True:
        raise RaydiumClmmSwapInnerIterationError("next initialized Raydium tick is required")
    if next_tick_state.tick_index != next_tick:
        raise RaydiumClmmSwapInnerIterationError("next tick index differs from decoded TickState")
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool):
        raise RaydiumClmmSwapInnerIterationError("swap direction and input mode must be boolean")

    is_fee_on_input = _is_fee_on_input(pool, zero_for_one)
    try:
        price_step = compute_raydium_clmm_swap_price_step(
            fee_state,
            sqrt_price_current_x64=sqrt_price_current_x64,
            target_price_x64=target_price_x64,
            base_fee_rate=base_fee_rate,
            protocol_fee_rate=protocol_fee_rate,
            fund_fee_rate=fund_fee_rate,
            zero_for_one=zero_for_one,
            is_base_input=is_base_input,
            is_fee_on_input=is_fee_on_input,
            dynamic_fee_state=dynamic_fee_state,
        )
    except RaydiumSwapPriceStepError as exc:
        raise RaydiumClmmSwapInnerIterationError(f"price/fee stage failed: {exc}") from exc

    fee_state_after = price_step.fee_state
    tick_after = next_tick_state
    match = RaydiumLimitOrderMatch()
    unfilled_before = _unfilled_amount(next_tick_state)
    unfilled_after = unfilled_before
    liquidity_next = fee_state.liquidity
    crossed = False
    reached_tick = price_step.swap_result.sqrt_price_next_x64 == sqrt_price_next_x64

    if reached_tick:
        try:
            boundary = apply_raydium_limit_order_boundary(
                next_tick_state,
                fee_state_after,
                fee_rate=price_step.fee_rate,
                sqrt_price_x64=sqrt_price_next_x64,
                zero_for_one=zero_for_one,
                is_base_input=is_base_input,
                is_fee_on_input=is_fee_on_input,
                protocol_fee_rate=protocol_fee_rate,
                fund_fee_rate=fund_fee_rate,
            )
        except RaydiumLimitOrderBoundaryError as exc:
            raise RaydiumClmmSwapInnerIterationError(f"limit-order stage failed: {exc}") from exc
        tick_after = boundary.tick
        fee_state_after = boundary.fee_state
        match = boundary.match
        unfilled_before = boundary.unfilled_amount_before
        unfilled_after = boundary.unfilled_amount_after

        if (
            fee_state_after.amount_specified_remaining != 0
            and unfilled_after != unfilled_before
            and unfilled_after != 0
        ):
            raise RaydiumClmmSwapInnerIterationError(
                "source residual-order invariant failed: partial order progress left swap amount remaining"
            )

        try:
            crossing = cross_raydium_clmm_tick_at_boundary(
                pool,
                tick_after,
                current_liquidity=fee_state_after.liquidity,
                fee_growth_global_x64=fee_state_after.fee_growth_global_x64,
                zero_for_one=zero_for_one,
            )
        except RaydiumTickBoundaryCrossingError as exc:
            raise RaydiumClmmSwapInnerIterationError(f"tick crossing stage failed: {exc}") from exc
        tick_after = crossing.tick
        liquidity_next = crossing.liquidity_after
        crossed = crossing.crossed

    try:
        finalized = finalize_raydium_swap_tick_price(
            price_step,
            current_tick=current_tick,
            next_tick=next_tick,
            sqrt_price_next_x64=sqrt_price_next_x64,
            updated_tick_state=tick_after,
            reached_initialized_tick=reached_tick,
            zero_for_one=zero_for_one,
        )
    except RaydiumSwapTickPriceFinalizationError as exc:
        raise RaydiumClmmSwapInnerIterationError(f"tick/price finalization failed: {exc}") from exc

    should_break = (
        fee_state_after.amount_specified_remaining == 0
        or finalized.sqrt_price_x64 == target_price_x64
    )
    if should_break:
        fee_state_after = replace(fee_state_after, liquidity=liquidity_next)

    return RaydiumClmmSwapInnerIterationResult(
        fee_state=fee_state_after,
        tick_state=tick_after,
        tick=finalized.tick,
        sqrt_price_x64=finalized.sqrt_price_x64,
        dynamic_fee_state=finalized.dynamic_fee_state,
        liquidity_next=liquidity_next,
        price_step=price_step,
        limit_order_match=match,
        unfilled_amount_before=unfilled_before,
        unfilled_amount_after=unfilled_after,
        reached_initialized_tick=reached_tick,
        crossed_tick=crossed,
        should_break=should_break,
    )
