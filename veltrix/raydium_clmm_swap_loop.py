"""Deterministic Raydium swap-loop pre-step transition, pinned to swap_internal."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_dynamic_fee import (
    advance_dynamic_fee_index,
    RaydiumDynamicFeeSwapState,
    RaydiumSpacingBound,
    compute_total_fee_rate,
    get_spacing_bounded_price,
    update_volatility_accumulator,
)
from veltrix.raydium_clmm_fee_accounting import (
    RaydiumFeeAccountingState,
    apply_raydium_swap_amounts,
)
from veltrix.raydium_clmm_limit_orders import (
    RaydiumLimitOrderMatch,
    match_raydium_limit_order,
)
from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmPoolState,
    RaydiumClmmRewardInfo,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumTickArrayMutationResult,
    apply_raydium_tick_array_mutation,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTick,
    RaydiumClmmTickArray,
)
from veltrix.raydium_clmm_tick_crossing import (
    RaydiumRewardGrowth,
    RaydiumTickCrossingResult,
    cross_tick_and_apply_liquidity,
)
from veltrix.raydium_clmm_tick_cursor import RaydiumInitializedTickCursor
from veltrix.raydium_clmm_swap_step_math import (
    RaydiumSwapStepResult,
    compute_swap_step,
)


class RaydiumSwapLoopError(ValueError):
    """Raised when a pinned Raydium swap-loop pre-step cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapLoopPreStepResult:
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None
    total_fee_rate: int
    spacing_bound: RaydiumSpacingBound
    swap_step: RaydiumSwapStepResult
    fee_accounting_state: RaydiumFeeAccountingState
    price_changed: bool


def compute_raydium_swap_loop_pre_step(
    fee_state: RaydiumFeeAccountingState,
    *,
    sqrt_price_current_x64: int,
    target_price_x64: int,
    base_fee_rate: int,
    zero_for_one: bool,
    is_base_input: bool,
    is_fee_on_input: bool,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None = None,
) -> RaydiumClmmSwapLoopPreStepResult:
    """Execute source lines 635–668: volatility -> total fee -> spacing bound -> swap math -> amount accounting.

    FIFO matching is deliberately not performed here. The returned total fee
    rate is the pre-index-advance rate and must be reused for a boundary limit
    order match before the caller advances the dynamic-fee spacing index.
    """
    if not isinstance(fee_state, RaydiumFeeAccountingState):
        raise RaydiumSwapLoopError("Raydium fee-accounting state is required")
    for value, field in (
        (zero_for_one, "zero_for_one"),
        (is_base_input, "is_base_input"),
        (is_fee_on_input, "is_fee_on_input"),
    ):
        if not isinstance(value, bool):
            raise RaydiumSwapLoopError(f"{field} must be boolean")
    if isinstance(base_fee_rate, bool) or not isinstance(base_fee_rate, int) or not 0 <= base_fee_rate < 1_000_000:
        raise RaydiumSwapLoopError("base fee rate must be in [0, 1,000,000)")
    for value, field in (
        (sqrt_price_current_x64, "current sqrt price"),
        (target_price_x64, "target sqrt price"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < (1 << 128):
            raise RaydiumSwapLoopError(f"{field} must be a positive u128")
    for value, field in (
        (protocol_fee_rate, "protocol fee rate"),
        (fund_fee_rate, "fund fee rate"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
            raise RaydiumSwapLoopError(f"{field} must be in [0, 1,000,000]")
    if protocol_fee_rate + fund_fee_rate > 1_000_000:
        raise RaydiumSwapLoopError("protocol and fund fee rates exceed fee denominator")
    if isinstance(fee_state.liquidity, bool) or not isinstance(fee_state.liquidity, int) or not 0 <= fee_state.liquidity < (1 << 128):
        raise RaydiumSwapLoopError("fee-accounting liquidity is outside u128")
    if dynamic_fee_state is None:
        total_fee_rate = base_fee_rate
        spacing_bound = RaydiumSpacingBound(
            reached_target=True,
            sqrt_price_x64=target_price_x64,
            bounded_tick=None,
        )
        updated_dynamic_state = None
    else:
        if not isinstance(dynamic_fee_state, RaydiumDynamicFeeSwapState):
            raise RaydiumSwapLoopError("dynamic-fee swap state is malformed")
        if dynamic_fee_state.base_fee_rate != base_fee_rate:
            raise RaydiumSwapLoopError("dynamic-fee state base rate differs from the requested pool base fee")
        if dynamic_fee_state.tick_spacing <= 0:
            raise RaydiumSwapLoopError("dynamic-fee tick spacing must be positive")
        if dynamic_fee_state.info.enabled is not True:
            raise RaydiumSwapLoopError("dynamic-fee swap state must contain enabled configuration")
        if fee_state.liquidity < 0:
            raise RaydiumSwapLoopError("fee-accounting liquidity is invalid")
        updated_info = update_volatility_accumulator(
            dynamic_fee_state.info,
            tick_spacing_index=dynamic_fee_state.tick_spacing_index,
        )
        updated_dynamic_state = replace(
            dynamic_fee_state,
            info=updated_info,
        )
        total_fee_rate = compute_total_fee_rate(
            base_fee_rate=base_fee_rate,
            info=updated_info,
            tick_spacing=dynamic_fee_state.tick_spacing,
        )
        spacing_bound = get_spacing_bounded_price(
            updated_dynamic_state,
            target_price_x64=target_price_x64,
            zero_for_one=zero_for_one,
            liquidity=fee_state.liquidity,
        )

    if not 0 <= spacing_bound.sqrt_price_x64 < (1 << 128):
        raise RaydiumSwapLoopError("bounded sqrt price is outside u128")
    price_changed = sqrt_price_current_x64 != spacing_bound.sqrt_price_x64
    if price_changed:
        step = compute_swap_step(
            sqrt_price_current_x64,
            spacing_bound.sqrt_price_x64,
            fee_state.liquidity,
            fee_state.amount_specified_remaining,
            total_fee_rate,
            is_base_input=is_base_input,
            zero_for_one=zero_for_one,
            is_fee_on_input=is_fee_on_input,
        )
        next_fee_state = apply_raydium_swap_amounts(
            fee_state,
            amount_in=step.amount_in,
            amount_out=step.amount_out,
            fee_amount=step.fee_amount,
            is_base_input=is_base_input,
            is_fee_on_input=is_fee_on_input,
            protocol_fee_rate=protocol_fee_rate,
            fund_fee_rate=fund_fee_rate,
        )
    else:
        step = RaydiumSwapStepResult(
            sqrt_price_next_x64=spacing_bound.sqrt_price_x64,
            amount_in=0,
            amount_out=0,
            fee_amount=0,
            reached_target=False,
        )
        next_fee_state = fee_state

    return RaydiumClmmSwapLoopPreStepResult(
        dynamic_fee_state=updated_dynamic_state,
        total_fee_rate=total_fee_rate,
        spacing_bound=spacing_bound,
        swap_step=step,
        fee_accounting_state=next_fee_state,
        price_changed=price_changed,
    )


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapLoopBoundaryResult:
    boundary_reached: bool
    pool: RaydiumClmmPoolState
    tick_array: RaydiumClmmTickArray
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None
    fee_accounting_state: RaydiumFeeAccountingState
    tick: RaydiumClmmTick
    tick_current: int
    liquidity_next: int
    limit_order_match: RaydiumLimitOrderMatch
    crossing: RaydiumTickCrossingResult | None
    mutation: RaydiumTickArrayMutationResult | None
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None


def _is_fee_on_token0(fee_on: int, zero_for_one: bool) -> bool:
    if fee_on == 0:
        return zero_for_one
    if fee_on == 1:
        return True
    if fee_on == 2:
        return False
    raise RaydiumSwapLoopError("pool fee_on mode is invalid")


def _unfilled_limit_order_amount(tick: RaydiumClmmTick) -> int:
    for value, field in (
        (tick.orders_amount, "orders_amount"),
        (tick.part_filled_orders_remaining, "part_filled_orders_remaining"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= (1 << 64) - 1:
            raise RaydiumSwapLoopError(f"{field} must fit u64")
    total = tick.orders_amount + tick.part_filled_orders_remaining
    if total > (1 << 64) - 1:
        raise RaydiumSwapLoopError("limit-order unfilled amount overflows u64")
    return total


def apply_raydium_swap_loop_boundary(
    pre_step: RaydiumClmmSwapLoopPreStepResult,
    *,
    pool: RaydiumClmmPoolState,
    tick_array: RaydiumClmmTickArray,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None,
    cursor: RaydiumInitializedTickCursor,
    current_tick: int,
    current_sqrt_price_x64: int,
    tick_boundary_price_x64: int,
    updated_reward_infos: tuple[RaydiumClmmRewardInfo, ...],
    recent_epoch: int,
    zero_for_one: bool,
    is_base_input: bool,
    is_fee_on_input: bool,
    protocol_fee_rate: int,
    fund_fee_rate: int,
) -> RaydiumClmmSwapLoopBoundaryResult:
    """Mirror swap_internal's boundary FIFO -> count/bitmap -> cross -> tick-write order."""
    if not isinstance(pre_step, RaydiumClmmSwapLoopPreStepResult):
        raise RaydiumSwapLoopError("completed swap-loop pre-step result is required")
    if not isinstance(pool, RaydiumClmmPoolState) or not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumSwapLoopError("decoded PoolState and TickArrayState are required")
    if not isinstance(cursor, RaydiumInitializedTickCursor):
        raise RaydiumSwapLoopError("selected initialized-tick cursor is required")
    for value, field in (
        (zero_for_one, "zero_for_one"),
        (is_base_input, "is_base_input"),
        (is_fee_on_input, "is_fee_on_input"),
    ):
        if not isinstance(value, bool):
            raise RaydiumSwapLoopError(f"{field} must be boolean")
    if tick_array.pool_address != pool.pool_address or cursor.tick_array_start_index != tick_array.start_tick_index:
        raise RaydiumSwapLoopError("selected tick cursor does not belong to the supplied pool/tick array")
    if cursor.tick.tick_index != tick_array.start_tick_index + cursor.tick_offset * pool.tick_spacing:
        raise RaydiumSwapLoopError("selected tick cursor offset is inconsistent with its array slot")
    if not isinstance(updated_reward_infos, tuple) or len(updated_reward_infos) != 3 or any(
        not isinstance(item, RaydiumClmmRewardInfo) for item in updated_reward_infos
    ):
        raise RaydiumSwapLoopError("three updated Raydium reward records are required")

    for value, field in (
        (current_sqrt_price_x64, "current sqrt price"),
        (tick_boundary_price_x64, "tick boundary sqrt price"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < (1 << 128):
            raise RaydiumSwapLoopError(f"{field} must be a positive u128")
    if zero_for_one and tick_boundary_price_x64 > current_sqrt_price_x64:
        raise RaydiumSwapLoopError("leftward tick boundary price is above the current price")
    if not zero_for_one and tick_boundary_price_x64 < current_sqrt_price_x64:
        raise RaydiumSwapLoopError("rightward tick boundary price is below the current price")
    tick_next = cursor.tick.tick_index
    if pre_step.swap_step.sqrt_price_next_x64 != tick_boundary_price_x64:
        return RaydiumClmmSwapLoopBoundaryResult(
            boundary_reached=False,
            pool=pool,
            tick_array=tick_array,
            bitmap_extension=bitmap_extension,
            fee_accounting_state=pre_step.fee_accounting_state,
            tick=cursor.tick,
            tick_current=current_tick,
            liquidity_next=pre_step.fee_accounting_state.liquidity,
            limit_order_match=RaydiumLimitOrderMatch(),
            crossing=None,
            mutation=None,
            dynamic_fee_state=pre_step.dynamic_fee_state,
        )

    before_unfilled = _unfilled_limit_order_amount(cursor.tick)
    matched_tick, match = match_raydium_limit_order(
        cursor.tick,
        swap_amount=pre_step.fee_accounting_state.amount_specified_remaining,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        fee_rate=pre_step.total_fee_rate,
        is_fee_on_input=is_fee_on_input,
        sqrt_price_x64=tick_boundary_price_x64,
    )
    fee_state = pre_step.fee_accounting_state
    if match.amount_in or match.amount_out or match.amm_fee_amount:
        fee_state = apply_raydium_swap_amounts(
            fee_state,
            amount_in=match.amount_in,
            amount_out=match.amount_out,
            fee_amount=match.amm_fee_amount,
            is_base_input=is_base_input,
            is_fee_on_input=is_fee_on_input,
            protocol_fee_rate=protocol_fee_rate,
            fund_fee_rate=fund_fee_rate,
        )
    after_unfilled = _unfilled_limit_order_amount(matched_tick)
    if (
        fee_state.amount_specified_remaining != 0
        and after_unfilled != before_unfilled
        and after_unfilled != 0
    ):
        raise RaydiumSwapLoopError(
            "boundary FIFO partial fill left both swap amount and tick order amount outstanding"
        )

    crossing = None
    liquidity_next = fee_state.liquidity
    final_tick = matched_tick
    if matched_tick.liquidity_gross > 0 and (
        matched_tick.orders_amount == 0
        and matched_tick.part_filled_orders_remaining == 0
    ):
        fee_on_token0 = _is_fee_on_token0(pool.fee_on, zero_for_one)
        fee_growth_0 = (
            fee_state.fee_growth_global_x64
            if fee_on_token0 else pool.fee_growth_global_0_x64
        )
        fee_growth_1 = (
            pool.fee_growth_global_1_x64
            if fee_on_token0 else fee_state.fee_growth_global_x64
        )
        reward_growths = tuple(
            RaydiumRewardGrowth(
                initialized=reward.initialized,
                reward_growth_global_x64=reward.reward_growth_global_x64,
            )
            for reward in updated_reward_infos
        )
        crossing = cross_tick_and_apply_liquidity(
            matched_tick,
            current_liquidity=fee_state.liquidity,
            fee_growth_global_0_x64=fee_growth_0,
            fee_growth_global_1_x64=fee_growth_1,
            reward_infos=reward_growths,
            zero_for_one=zero_for_one,
        )
        final_tick = crossing.tick
        liquidity_next = crossing.liquidity_after

    mutation = apply_raydium_tick_array_mutation(
        pool,
        tick_array,
        tick_index=tick_next,
        updated_tick=final_tick,
        recent_epoch=recent_epoch,
        bitmap_extension=bitmap_extension,
    )
    has_limit_orders = (
        final_tick.orders_amount > 0
        or final_tick.part_filled_orders_remaining > 0
    )
    tick_current = (
        tick_next - 1
        if (zero_for_one and not has_limit_orders)
        or (not zero_for_one and has_limit_orders)
        else tick_next
    )

    dynamic_fee_state = pre_step.dynamic_fee_state
    if dynamic_fee_state is not None:
        dynamic_fee_state = advance_dynamic_fee_index(
            dynamic_fee_state,
            tick=tick_current,
            tick_next=tick_next,
            sqrt_price_x64=pre_step.swap_step.sqrt_price_next_x64,
            sqrt_price_next_x64=tick_boundary_price_x64,
            zero_for_one=zero_for_one,
            is_skipped_tick_spacing=pre_step.spacing_bound.reached_target,
        )

    return RaydiumClmmSwapLoopBoundaryResult(
        boundary_reached=True,
        pool=mutation.pool,
        tick_array=mutation.tick_array,
        bitmap_extension=mutation.bitmap_extension,
        fee_accounting_state=fee_state,
        tick=final_tick,
        tick_current=tick_current,
        liquidity_next=liquidity_next,
        limit_order_match=match,
        crossing=crossing,
        mutation=mutation,
        dynamic_fee_state=dynamic_fee_state,
    )
