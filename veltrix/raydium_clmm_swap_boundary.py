"""Source-ordered Raydium CLMM initialized-tick boundary transition.

Ports the boundary branch in instructions/swap.rs at source commit
6d7a0f82d2f293c553c69aaf943377367c6c4cc6. This component handles one
initialized tick boundary; the complete outer swap loop and account serialization
remain separate stages.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_fee_accounting import apply_raydium_swap_amounts
from veltrix.raydium_clmm_limit_orders import (
    RaydiumLimitOrderMatch,
    match_raydium_limit_order,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_POOL_REWARD_COUNT,
    RaydiumClmmRewardInfo,
)
from veltrix.raydium_clmm_swap_loop_state import (
    RaydiumPreparedSwapStep,
    RaydiumSwapLoopState,
    advance_raydium_dynamic_fee_after_tick,
    apply_prepared_raydium_swap_step,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumTickArrayMutationResult,
    apply_raydium_tick_array_mutation,
    apply_raydium_tick_array_mutation_chained,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTick,
    RaydiumClmmTickArray,
)
from veltrix.raydium_clmm_tick_crossing import (
    RaydiumRewardGrowth,
    cross_tick_and_apply_liquidity,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


class RaydiumSwapBoundaryError(ValueError):
    """Raised when an initialized-tick boundary transition is inconsistent."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapBoundaryResult:
    state: RaydiumSwapLoopState
    mutation: RaydiumTickArrayMutationResult
    limit_order_match: RaydiumLimitOrderMatch
    limit_order_unfilled_before: int
    limit_order_unfilled_after: int
    crossed_liquidity: bool


def _fee_on_token0(state: RaydiumSwapLoopState) -> bool:
    if state.pool.fee_on == 0:
        return state.zero_for_one
    if state.pool.fee_on == 1:
        return True
    if state.pool.fee_on == 2:
        return False
    raise RaydiumSwapBoundaryError("pool fee_on mode is invalid")


def apply_raydium_swap_boundary(
    prepared: RaydiumPreparedSwapStep,
    *,
    tick_array: RaydiumClmmTickArray,
    next_tick: RaydiumClmmTick,
    recent_epoch: int,
    updated_reward_infos: tuple[RaydiumClmmRewardInfo, ...],
    protocol_fee_rate: int,
    fund_fee_rate: int,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
    mutation_context: RaydiumTickArrayMutationResult | None = None,
    prior_array_mutation: RaydiumTickArrayMutationResult | None = None,
) -> RaydiumSwapBoundaryResult:
    """Apply AMM amounts, FIFO matching, conditional tick crossing and mutation.

    The boundary is accepted only when the prepared step lands exactly on the
    source-selected initialized tick. Liquidity is changed after FIFO matching,
    only when the tick has liquidity and no unfilled limit orders remain.
    """
    if not isinstance(prepared, RaydiumPreparedSwapStep):
        raise RaydiumSwapBoundaryError("prepared Raydium swap step is required")
    state = prepared.state
    if not isinstance(state, RaydiumSwapLoopState):
        raise RaydiumSwapBoundaryError("Raydium swap-loop state is required")
    if not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumSwapBoundaryError("decoded tick-array state is required")
    if not isinstance(next_tick, RaydiumClmmTick):
        raise RaydiumSwapBoundaryError("decoded initialized tick is required")
    if next_tick.initialized is not True:
        raise RaydiumSwapBoundaryError("boundary transition requires an initialized tick")
    if next_tick.tick_index != state.tick_next:
        raise RaydiumSwapBoundaryError("selected tick differs from the prepared next-tick target")
    if get_sqrt_price_at_tick(next_tick.tick_index) != state.sqrt_price_next_x64:
        raise RaydiumSwapBoundaryError("selected tick sqrt price differs from the prepared boundary")
    if prepared.result.sqrt_price_next_x64 != state.sqrt_price_next_x64:
        raise RaydiumSwapBoundaryError("prepared swap step did not reach the initialized-tick boundary")
    if tick_array.pool_address != state.pool.pool_address:
        raise RaydiumSwapBoundaryError("tick array belongs to a different pool")
    if tick_array.tick_spacing != state.pool.tick_spacing:
        raise RaydiumSwapBoundaryError("tick-array spacing differs from PoolState")
    if len(updated_reward_infos) != RAYDIUM_POOL_REWARD_COUNT or any(
        not isinstance(reward, RaydiumClmmRewardInfo) for reward in updated_reward_infos
    ):
        raise RaydiumSwapBoundaryError("exactly three updated PoolState reward records are required")

    # swap.rs applies the AMM step before matching limit orders at this boundary.
    applied = apply_prepared_raydium_swap_step(
        prepared,
        protocol_fee_rate=protocol_fee_rate,
        fund_fee_rate=fund_fee_rate,
    )
    unfilled_before = next_tick.orders_amount + next_tick.part_filled_orders_remaining
    matched_tick, match = match_raydium_limit_order(
        next_tick,
        swap_amount=applied.fee_state.amount_specified_remaining,
        zero_for_one=state.zero_for_one,
        is_base_input=state.is_base_input,
        fee_rate=prepared.total_fee_rate,
        is_fee_on_input=state.is_fee_on_input,
        sqrt_price_x64=state.sqrt_price_next_x64,
    )
    if match.amount_in or match.amount_out or match.amm_fee_amount:
        fee_state = apply_raydium_swap_amounts(
            applied.fee_state,
            amount_in=match.amount_in,
            amount_out=match.amount_out,
            fee_amount=match.amm_fee_amount,
            is_base_input=state.is_base_input,
            is_fee_on_input=state.is_fee_on_input,
            protocol_fee_rate=protocol_fee_rate,
            fund_fee_rate=fund_fee_rate,
        )
        applied = replace(applied, fee_state=fee_state)

    unfilled_after = matched_tick.orders_amount + matched_tick.part_filled_orders_remaining
    if (
        applied.fee_state.amount_specified_remaining != 0
        and unfilled_after != unfilled_before
        and unfilled_after != 0
    ):
        raise RaydiumSwapBoundaryError(
            "source boundary invariant: remaining swap amount requires FIFO orders to be fully consumed"
        )
    has_limit_orders = unfilled_after > 0
    crossed_liquidity = matched_tick.liquidity_gross > 0 and not has_limit_orders
    liquidity_next = applied.fee_state.liquidity
    final_tick = matched_tick

    if crossed_liquidity:
        fee_on_token0 = _fee_on_token0(applied)
        fee_growth_0 = (
            applied.fee_state.fee_growth_global_x64
            if fee_on_token0 else applied.pool.fee_growth_global_0_x64
        )
        fee_growth_1 = (
            applied.pool.fee_growth_global_1_x64
            if fee_on_token0 else applied.fee_state.fee_growth_global_x64
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
            current_liquidity=applied.fee_state.liquidity,
            fee_growth_global_0_x64=fee_growth_0,
            fee_growth_global_1_x64=fee_growth_1,
            reward_infos=reward_growths,
            zero_for_one=state.zero_for_one,
        )
        final_tick = crossing.tick
        liquidity_next = crossing.liquidity_after

    # update_tick_state is applied after the crossing decision in the pinned source.
    if mutation_context is None:
        if prior_array_mutation is not None:
            raise RaydiumSwapBoundaryError("array mutation lineage requires a global mutation context")
        mutation = apply_raydium_tick_array_mutation(
            applied.pool,
            tick_array,
            tick_index=final_tick.tick_index,
            updated_tick=final_tick,
            recent_epoch=recent_epoch,
            bitmap_extension=bitmap_extension,
        )
    else:
        if applied.pool != mutation_context.pool:
            raise RaydiumSwapBoundaryError("boundary PoolState differs from the latest mutation lineage")
        if bitmap_extension is not None and bitmap_extension != mutation_context.bitmap_extension:
            raise RaydiumSwapBoundaryError("boundary bitmap extension differs from the latest mutation lineage")
        mutation = apply_raydium_tick_array_mutation_chained(
            mutation_context,
            tick_array,
            tick_index=final_tick.tick_index,
            updated_tick=final_tick,
            recent_epoch=recent_epoch,
            prior_array_mutation=prior_array_mutation,
        )

    tick_after = (
        state.tick_next - 1
        if (state.zero_for_one and not has_limit_orders)
        or (not state.zero_for_one and has_limit_orders)
        else state.tick_next
    )
    advanced = advance_raydium_dynamic_fee_after_tick(
        applied,
        tick_after_transition=tick_after,
        is_skipped_tick_spacing=prepared.is_skipped_tick_spacing,
    )
    # swap.rs assigns liquidity_next only after the boundary's inner loop exits.
    advanced = replace(
        advanced,
        pool=mutation.pool,
        fee_state=replace(advanced.fee_state, liquidity=liquidity_next),
    )
    return RaydiumSwapBoundaryResult(
        state=advanced,
        mutation=mutation,
        limit_order_match=match,
        limit_order_unfilled_before=unfilled_before,
        limit_order_unfilled_after=unfilled_after,
        crossed_liquidity=crossed_liquidity,
    )
