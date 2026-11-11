"""Complete source-ordered Raydium CLMM swap loop over verified tick arrays.

Ports the outer swap_internal loop at Raydium CLMM commit
6d7a0f82d2f293c553c69aaf943377367c6c4cc6. It remains an immutable replay
primitive; final account serialization and vault settlement are separate stages.
"""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmPoolState,
    RaydiumClmmRewardInfo,
)
from veltrix.raydium_clmm_rewards import update_raydium_reward_infos
from veltrix.raydium_clmm_swap_boundary import (
    apply_raydium_swap_boundary,
)
from veltrix.raydium_clmm_swap_loop import (
    RaydiumNextTickSelection,
    RaydiumSwapLoopSelectionError,
    select_next_initialized_tick,
)
from veltrix.raydium_clmm_swap_loop_state import (
    RaydiumSwapLoopState,
    advance_raydium_dynamic_fee_after_tick,
    apply_prepared_raydium_swap_step,
    initialize_raydium_swap_loop_state,
    prepare_raydium_swap_step,
    set_raydium_swap_loop_target,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumTickArrayMutationSession,
    create_raydium_tick_array_mutation_session,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    raydium_current_tick_array_start,
)


class RaydiumSwapLoopError(ValueError):
    """Raised when the pinned outer swap loop cannot advance safely."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapLoopResult:
    state: RaydiumSwapLoopState
    mutation_session: RaydiumTickArrayMutationSession
    updated_reward_infos: tuple[RaydiumClmmRewardInfo, ...]
    step_count: int
    boundary_count: int
    stop_reason: str


def execute_raydium_swap_loop(
    pool: RaydiumClmmPoolState,
    tick_arrays: tuple[RaydiumClmmTickArray, ...],
    traversal: RaydiumClmmBitmapTraversalResult,
    *,
    amount_specified: int,
    trade_fee_rate: int,
    zero_for_one: bool,
    is_base_input: bool,
    block_timestamp: int,
    sqrt_price_limit_x64: int,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    recent_epoch: int,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
    max_iterations: int = 4096,
) -> RaydiumSwapLoopResult:
    """Run the source-ordered outer swap loop until amount or price limit is met.

    The verified traversal must include every initialized tick array supplied in
    source CPI order. Missing next arrays, stalled steps and iteration exhaustion
    fail closed rather than fabricating liquidity or silently returning partial state.
    """
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumSwapLoopError("decoded Raydium PoolState is required")
    if not isinstance(traversal, RaydiumClmmBitmapTraversalResult):
        raise RaydiumSwapLoopError("verified Raydium bitmap traversal result is required")
    if isinstance(recent_epoch, bool) or not isinstance(recent_epoch, int) or not 0 <= recent_epoch < (1 << 64):
        raise RaydiumSwapLoopError("recent_epoch must fit u64")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= 100_000:
        raise RaydiumSwapLoopError("max_iterations must be between 1 and 100000")

    try:
        session = create_raydium_tick_array_mutation_session(
            pool, tick_arrays, bitmap_extension=bitmap_extension
        )
        updated_rewards = update_raydium_reward_infos(
            pool.reward_infos,
            curr_timestamp=block_timestamp,
            liquidity=pool.liquidity,
        )
        state = initialize_raydium_swap_loop_state(
            pool,
            amount_specified=amount_specified,
            trade_fee_rate=trade_fee_rate,
            zero_for_one=zero_for_one,
            is_base_input=is_base_input,
            block_timestamp=block_timestamp,
        )
    except (TypeError, ValueError) as exc:
        raise RaydiumSwapLoopError(str(exc)) from exc

    first_array_contains_pool_tick = (
        raydium_current_tick_array_start(state.tick, pool.tick_spacing)
        == session.tick_arrays[0].start_tick_index
    )
    current_array_index = 0
    step_count = 0
    boundary_count = 0

    while (
        state.fee_state.amount_specified_remaining != 0
        and state.sqrt_price_x64 != sqrt_price_limit_x64
    ):
        if step_count >= max_iterations:
            raise RaydiumSwapLoopError("outer swap loop exceeded its bounded iteration count")

        try:
            selection: RaydiumNextTickSelection = select_next_initialized_tick(
                tick_arrays=session.tick_arrays,
                traversal=traversal,
                current_tick=state.tick,
                current_tick_array_index=current_array_index,
                tick_spacing=pool.tick_spacing,
                zero_for_one=zero_for_one,
                first_tick_array_contains_pool_tick=first_array_contains_pool_tick,
            )
            current_array_index = selection.tick_array_index
            first_array_contains_pool_tick = selection.first_tick_array_contains_pool_tick
            targeted = set_raydium_swap_loop_target(
                state,
                next_tick=selection.tick.tick_index,
                sqrt_price_limit_x64=sqrt_price_limit_x64,
            )
            prepared = prepare_raydium_swap_step(targeted)
            before_remaining = state.fee_state.amount_specified_remaining
            before_sqrt_price = state.sqrt_price_x64

            if prepared.result.sqrt_price_next_x64 == targeted.sqrt_price_next_x64:
                boundary = apply_raydium_swap_boundary(
                    prepared,
                    tick_array=session.tick_arrays[current_array_index],
                    next_tick=selection.tick,
                    recent_epoch=recent_epoch,
                    updated_reward_infos=updated_rewards,
                    protocol_fee_rate=protocol_fee_rate,
                    fund_fee_rate=fund_fee_rate,
                    mutation_session=session,
                    tick_array_index=current_array_index,
                )
                if boundary.mutation_session is None:
                    raise RaydiumSwapLoopError("boundary transition did not return its authenticated working set")
                session = boundary.mutation_session
                state = boundary.state
                boundary_count += 1
            else:
                state = apply_prepared_raydium_swap_step(
                    prepared,
                    protocol_fee_rate=protocol_fee_rate,
                    fund_fee_rate=fund_fee_rate,
                )
                state = advance_raydium_dynamic_fee_after_tick(
                    state,
                    tick_after_transition=state.tick,
                    is_skipped_tick_spacing=prepared.is_skipped_tick_spacing,
                )

            step_count += 1
            if (
                state.fee_state.amount_specified_remaining == before_remaining
                and state.sqrt_price_x64 == before_sqrt_price
            ):
                raise RaydiumSwapLoopError("swap loop made no amount or price progress")
        except RaydiumSwapLoopSelectionError as exc:
            raise RaydiumSwapLoopError(str(exc)) from exc
        except RaydiumSwapLoopError:
            raise
        except (TypeError, ValueError) as exc:
            raise RaydiumSwapLoopError(str(exc)) from exc

    stop_reason = (
        "amount_consumed"
        if state.fee_state.amount_specified_remaining == 0
        else "price_limit_reached"
    )
    return RaydiumSwapLoopResult(
        state=state,
        mutation_session=session,
        updated_reward_infos=updated_rewards,
        step_count=step_count,
        boundary_count=boundary_count,
        stop_reason=stop_reason,
    )
