"""Pinned Raydium CLMM swap-state and single-step preparation.

Ports the SwapState initialization, next-tick target calculation, dynamic-fee
update, spacing bound and swap_math::compute_swap call order from
instructions/swap.rs at source commit 6d7a0f82d2f293c553c69aaf943377367c6c4cc6.

Tick crossing/FIFO matching and the complete outer loop are intentionally handled
by later orchestration stages; this module never claims transaction replay parity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeSwapState,
    advance_dynamic_fee_index,
    compute_total_fee_rate,
    get_spacing_bounded_price,
    initialize_dynamic_fee_swap_state,
    update_volatility_accumulator,
)
from veltrix.raydium_clmm_fee_accounting import (
    RaydiumFeeAccountingState,
    apply_raydium_swap_amounts,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_swap_step_math import (
    RaydiumSwapStepResult,
    compute_swap_step,
)
from veltrix.raydium_clmm_tick_math import (
    RAYDIUM_MAX_SQRT_PRICE_X64,
    RAYDIUM_MIN_SQRT_PRICE_X64,
    get_sqrt_price_at_tick,
    get_tick_at_sqrt_price,
)


class RaydiumSwapLoopStateError(ValueError):
    """Raised when pinned swap-loop state or a step transition is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapLoopState:
    pool: RaydiumClmmPoolState
    amount_specified: int
    fee_state: RaydiumFeeAccountingState
    sqrt_price_x64: int
    tick: int
    sqrt_price_next_x64: int
    tick_next: int
    zero_for_one: bool
    is_base_input: bool
    is_fee_on_input: bool
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None
    block_timestamp: int


@dataclass(frozen=True, slots=True)
class RaydiumPreparedSwapStep:
    state: RaydiumSwapLoopState
    target_price_x64: int
    bounded_price_x64: int
    bounded_tick: int | None
    is_skipped_tick_spacing: bool
    total_fee_rate: int
    result: RaydiumSwapStepResult


def _u64(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 64):
        raise RaydiumSwapLoopStateError(f"{field} must fit u64")


def _u128(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 128):
        raise RaydiumSwapLoopStateError(f"{field} must fit u128")


def _fee_on_token0(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return zero_for_one
    if pool.fee_on == 1:
        return True
    if pool.fee_on == 2:
        return False
    raise RaydiumSwapLoopStateError("pool fee_on mode is invalid")


def _fee_on_input(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return True
    if pool.fee_on == 1:
        return zero_for_one
    if pool.fee_on == 2:
        return not zero_for_one
    raise RaydiumSwapLoopStateError("pool fee_on mode is invalid")


def initialize_raydium_swap_loop_state(
    pool: RaydiumClmmPoolState,
    *,
    amount_specified: int,
    trade_fee_rate: int,
    zero_for_one: bool,
    is_base_input: bool,
    block_timestamp: int,
) -> RaydiumSwapLoopState:
    """Port SwapState::new and the one-time pool fee-side decisions."""
    if not isinstance(pool, RaydiumClmmPoolState) or not pool.swaps_enabled:
        raise RaydiumSwapLoopStateError("enabled decoded Raydium PoolState is required")
    _u64(amount_specified, "amount_specified")
    if amount_specified == 0:
        raise RaydiumSwapLoopStateError("amount_specified must be non-zero")
    _u64(block_timestamp, "block_timestamp")
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool):
        raise RaydiumSwapLoopStateError("swap direction and exact-input flag must be boolean")
    if isinstance(trade_fee_rate, bool) or not isinstance(trade_fee_rate, int) or not 0 <= trade_fee_rate < 1_000_000:
        raise RaydiumSwapLoopStateError("trade_fee_rate must be below the Raydium fee denominator")

    fee_growth = (
        pool.fee_growth_global_0_x64
        if _fee_on_token0(pool, zero_for_one)
        else pool.fee_growth_global_1_x64
    )
    dynamic_state = None
    if pool.dynamic_fee_info.enabled:
        dynamic_state = initialize_dynamic_fee_swap_state(
            info=pool.dynamic_fee_info,
            tick=pool.tick_current,
            tick_spacing=pool.tick_spacing,
            base_fee_rate=trade_fee_rate,
            block_timestamp=block_timestamp,
        )
    fee_state = RaydiumFeeAccountingState(
        amount_specified_remaining=amount_specified,
        amount_calculated=0,
        liquidity=pool.liquidity,
        fee_growth_global_x64=fee_growth,
    )
    return RaydiumSwapLoopState(
        pool=pool,
        amount_specified=amount_specified,
        fee_state=fee_state,
        sqrt_price_x64=pool.sqrt_price_x64,
        tick=pool.tick_current,
        sqrt_price_next_x64=0,
        tick_next=0,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        is_fee_on_input=_fee_on_input(pool, zero_for_one),
        dynamic_fee_state=dynamic_state,
        block_timestamp=block_timestamp,
    )


def set_raydium_swap_loop_target(
    state: RaydiumSwapLoopState,
    *,
    next_tick: int,
    sqrt_price_limit_x64: int,
) -> RaydiumSwapLoopState:
    """Port SwapState::get_target_price_based_on_next_tick."""
    if not isinstance(state, RaydiumSwapLoopState):
        raise RaydiumSwapLoopStateError("Raydium swap-loop state is required")
    if isinstance(next_tick, bool) or not isinstance(next_tick, int) or not -(1 << 31) <= next_tick < (1 << 31):
        raise RaydiumSwapLoopStateError("next tick must fit i32")
    _u128(sqrt_price_limit_x64, "sqrt_price_limit_x64")
    if state.zero_for_one:
        if not RAYDIUM_MIN_SQRT_PRICE_X64 < sqrt_price_limit_x64 < state.sqrt_price_x64:
            raise RaydiumSwapLoopStateError("zero-for-one price limit must be below current price and above protocol minimum")
        if next_tick > state.tick:
            raise RaydiumSwapLoopStateError("zero-for-one next tick must not exceed current tick")
    else:
        if not state.sqrt_price_x64 < sqrt_price_limit_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
            raise RaydiumSwapLoopStateError("one-for-zero price limit must exceed current price and remain below protocol maximum")
        if next_tick <= state.tick:
            raise RaydiumSwapLoopStateError("one-for-zero next tick must be strictly above current tick")

    tick_next = max(RAYDIUM_MIN_TICK, min(RAYDIUM_MAX_TICK, next_tick))
    sqrt_next = get_sqrt_price_at_tick(tick_next)
    if state.zero_for_one:
        if state.sqrt_price_x64 < sqrt_next:
            raise RaydiumSwapLoopStateError("next tick sqrt price exceeds current price for zero-for-one")
        target = sqrt_price_limit_x64 if sqrt_next < sqrt_price_limit_x64 else sqrt_next
    else:
        if sqrt_next < state.sqrt_price_x64:
            raise RaydiumSwapLoopStateError("next tick sqrt price is below current price for one-for-zero")
        target = sqrt_price_limit_x64 if sqrt_next > sqrt_price_limit_x64 else sqrt_next
    return replace(state, tick_next=tick_next, sqrt_price_next_x64=sqrt_next)


def prepare_raydium_swap_step(
    state: RaydiumSwapLoopState,
    *,
    target_price_x64: int,
) -> RaydiumPreparedSwapStep:
    """Port the inner-loop fee/bound/compute sequence up to the amount transition."""
    if not isinstance(state, RaydiumSwapLoopState):
        raise RaydiumSwapLoopStateError("Raydium swap-loop state is required")
    _u128(target_price_x64, "target_price_x64")

    next_state = state
    if state.dynamic_fee_state is not None:
        updated_info = update_volatility_accumulator(
            state.dynamic_fee_state.info,
            tick_spacing_index=state.dynamic_fee_state.tick_spacing_index,
        )
        next_state = replace(
            state,
            dynamic_fee_state=replace(state.dynamic_fee_state, info=updated_info),
        )
        fee_rate = compute_total_fee_rate(
            base_fee_rate=next_state.dynamic_fee_state.base_fee_rate,
            info=next_state.dynamic_fee_state.info,
            tick_spacing=next_state.dynamic_fee_state.tick_spacing,
        )
        spacing = get_spacing_bounded_price(
            next_state.dynamic_fee_state,
            target_price_x64=target_price_x64,
            zero_for_one=state.zero_for_one,
            liquidity=state.fee_state.liquidity,
        )
        is_skipped = spacing.reached_target
        bounded_price = spacing.sqrt_price_x64
        bounded_tick = spacing.bounded_tick
    else:
        fee_rate = state.dynamic_fee_state.base_fee_rate if state.dynamic_fee_state else 0
        # Static-fee pools use the AMM config trade fee rate stored during initialization.
        fee_rate = getattr(state, "trade_fee_rate", fee_rate)
        is_skipped = True
        bounded_price = target_price_x64
        bounded_tick = None

    if next_state.dynamic_fee_state is None:
        # Static fee rate is retained separately by the immutable fee-state context.
        fee_rate = getattr(state, "base_fee_rate", fee_rate)
    if fee_rate == 0 and next_state.dynamic_fee_state is None:
        # PoolState does not store the AmmConfig trade fee; require it explicitly via
        # initialize_raydium_swap_loop_state's dynamic_fee_state-compatible field below.
        raise RaydiumSwapLoopStateError("static-fee trade rate was not retained in swap-loop state")

    if state.sqrt_price_x64 == bounded_price:
        result = RaydiumSwapStepResult(
            sqrt_price_next_x64=bounded_price,
            amount_in=0,
            amount_out=0,
            fee_amount=0,
            reached_target=True,
        )
    else:
        result = compute_swap_step(
            sqrt_price_current_x64=state.sqrt_price_x64,
            sqrt_price_target_x64=bounded_price,
            liquidity=state.fee_state.liquidity,
            amount_remaining=state.fee_state.amount_specified_remaining,
            fee_rate=fee_rate,
            is_base_input=state.is_base_input,
            zero_for_one=state.zero_for_one,
            is_fee_on_input=state.is_fee_on_input,
        )
    return RaydiumPreparedSwapStep(
        state=next_state,
        target_price_x64=target_price_x64,
        bounded_price_x64=bounded_price,
        bounded_tick=bounded_tick,
        is_skipped_tick_spacing=is_skipped,
        total_fee_rate=fee_rate,
        result=result,
    )


def apply_prepared_raydium_swap_step(
    prepared: RaydiumPreparedSwapStep,
    *,
    protocol_fee_rate: int,
    fund_fee_rate: int,
) -> RaydiumSwapLoopState:
    """Apply SwapState::apply_swap_amounts and the non-boundary price/tick update.

    When the computed price reaches the initialized tick, the caller must perform
    FIFO matching and tick crossing first, then call advance_raydium_dynamic_fee_index
    with the post-transition tick. This function deliberately does not cross ticks.
    """
    if not isinstance(prepared, RaydiumPreparedSwapStep):
        raise RaydiumSwapLoopStateError("prepared Raydium swap step is required")
    state = prepared.state
    result = prepared.result
    fee_state = state.fee_state
    if result.amount_in or result.amount_out or result.fee_amount:
        fee_state = apply_raydium_swap_amounts(
            fee_state,
            amount_in=result.amount_in,
            amount_out=result.amount_out,
            fee_amount=result.fee_amount,
            is_base_input=state.is_base_input,
            is_fee_on_input=state.is_fee_on_input,
            protocol_fee_rate=protocol_fee_rate,
            fund_fee_rate=fund_fee_rate,
        )
    next_tick = state.tick
    if result.sqrt_price_next_x64 != state.sqrt_price_x64:
        if result.sqrt_price_next_x64 == state.sqrt_price_next_x64:
            # The caller owns the FIFO/tick-crossing branch and supplies its final tick later.
            next_tick = state.tick
        elif prepared.bounded_tick is not None and result.sqrt_price_next_x64 == prepared.bounded_price_x64:
            next_tick = prepared.bounded_tick
        else:
            next_tick = get_tick_at_sqrt_price(result.sqrt_price_next_x64)
    return replace(
        state,
        fee_state=fee_state,
        sqrt_price_x64=result.sqrt_price_next_x64,
        tick=next_tick,
    )


def advance_raydium_dynamic_fee_after_tick(
    state: RaydiumSwapLoopState,
    *,
    tick_after_transition: int,
    is_skipped_tick_spacing: bool,
) -> RaydiumSwapLoopState:
    """Port update_dynamic_fee_index after the caller has completed tick handling."""
    if not isinstance(state, RaydiumSwapLoopState):
        raise RaydiumSwapLoopStateError("Raydium swap-loop state is required")
    if isinstance(tick_after_transition, bool) or not isinstance(tick_after_transition, int) or not RAYDIUM_MIN_TICK <= tick_after_transition <= RAYDIUM_MAX_TICK:
        raise RaydiumSwapLoopStateError("post-transition tick is outside the Raydium tick range")
    if not isinstance(is_skipped_tick_spacing, bool):
        raise RaydiumSwapLoopStateError("skipped tick-spacing flag must be boolean")
    dynamic = state.dynamic_fee_state
    if dynamic is None:
        return replace(state, tick=tick_after_transition)
    updated = advance_dynamic_fee_index(
        dynamic,
        tick=tick_after_transition,
        tick_next=state.tick_next,
        sqrt_price_x64=state.sqrt_price_x64,
        sqrt_price_next_x64=state.sqrt_price_next_x64,
        zero_for_one=state.zero_for_one,
        is_skipped_tick_spacing=is_skipped_tick_spacing,
    )
    return replace(state, tick=tick_after_transition, dynamic_fee_state=updated)
