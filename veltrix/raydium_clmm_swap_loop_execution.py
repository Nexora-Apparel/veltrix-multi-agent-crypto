"""Bounded source-ordered Raydium CLMM swap-loop replay.

Composes the pinned tick selector, swap-step state and boundary transition. This
is an offline immutable replay primitive; it does not construct or submit swaps.
"""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_pool_state import RaydiumClmmRewardInfo
from veltrix.raydium_clmm_swap_boundary import (
    RaydiumSwapBoundaryResult,
    apply_raydium_swap_boundary,
)
from veltrix.raydium_clmm_swap_loop import select_next_initialized_tick
from veltrix.raydium_clmm_swap_loop_state import (
    RaydiumSwapLoopState,
    advance_raydium_dynamic_fee_after_tick,
    apply_prepared_raydium_swap_step,
    prepare_raydium_swap_step,
    set_raydium_swap_loop_target,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumTickArrayMutationResult,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArray


class RaydiumSwapLoopExecutionError(ValueError):
    """Raised when the bounded swap loop cannot preserve pinned source invariants."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapLoopExecution:
    state: RaydiumSwapLoopState
    updated_reward_infos: tuple[RaydiumClmmRewardInfo, ...]
    tick_arrays: tuple[RaydiumClmmTickArray, ...]
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None
    mutation: RaydiumTickArrayMutationResult | None
    array_mutations: tuple[tuple[int, RaydiumTickArrayMutationResult], ...]
    iterations: int
    boundary_transitions: int
    complete: bool
    termination_reason: str


def execute_raydium_swap_loop(
    state: RaydiumSwapLoopState,
    *,
    tick_arrays: tuple[RaydiumClmmTickArray, ...],
    traversal: RaydiumClmmBitmapTraversalResult,
    sqrt_price_limit_x64: int,
    first_tick_array_contains_pool_tick: bool,
    updated_reward_infos: tuple[RaydiumClmmRewardInfo, ...],
    recent_epoch: int,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
    max_iterations: int = 512,
) -> RaydiumSwapLoopExecution:
    """Run the outer swap loop with a strict iteration bound and mutation lineage.

    The supplied arrays must be the exact bitmap/CPI-verified sequence. The
    working set is updated immutably after each boundary; source account hashes
    are retained only as provenance for untouched arrays and explicitly cleared
    for mutated account states until a serializer produces new commitments.
    """
    if not isinstance(state, RaydiumSwapLoopState):
        raise RaydiumSwapLoopExecutionError("initialized Raydium swap-loop state is required")
    if not isinstance(tick_arrays, tuple) or not tick_arrays:
        raise RaydiumSwapLoopExecutionError("verified ordered tick arrays are required")
    if not isinstance(traversal, RaydiumClmmBitmapTraversalResult):
        raise RaydiumSwapLoopExecutionError("verified bitmap traversal is required")
    if not isinstance(first_tick_array_contains_pool_tick, bool):
        raise RaydiumSwapLoopExecutionError("first-array containment flag must be boolean")
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= 100_000:
        raise RaydiumSwapLoopExecutionError("max_iterations must be in [1, 100000]")
    if (
        isinstance(sqrt_price_limit_x64, bool)
        or not isinstance(sqrt_price_limit_x64, int)
        or sqrt_price_limit_x64 <= 0
    ):
        raise RaydiumSwapLoopExecutionError("sqrt price limit must be a positive integer")
    if not isinstance(updated_reward_infos, tuple) or len(updated_reward_infos) != 3:
        raise RaydiumSwapLoopExecutionError("exactly three updated reward records are required")
    if any(not isinstance(item, RaydiumClmmTickArray) for item in tick_arrays):
        raise RaydiumSwapLoopExecutionError("tick-array working set contains an invalid record")

    working_arrays = list(tick_arrays)
    current_array_index = 0
    first_contains = first_tick_array_contains_pool_tick
    mutation_context: RaydiumTickArrayMutationResult | None = None
    array_mutations: dict[int, RaydiumTickArrayMutationResult] = {}
    iterations = 0
    boundaries = 0
    reason = "iteration_limit"
    complete = False

    while iterations < max_iterations:
        if state.fee_state.amount_specified_remaining == 0:
            reason, complete = "amount_fully_consumed", True
            break
        if state.sqrt_price_x64 == sqrt_price_limit_x64:
            reason, complete = "price_limit_reached", True
            break

        selection = select_next_initialized_tick(
            tick_arrays=tuple(working_arrays),
            traversal=traversal,
            current_tick=state.tick,
            current_tick_array_index=current_array_index,
            tick_spacing=state.pool.tick_spacing,
            zero_for_one=state.zero_for_one,
            first_tick_array_contains_pool_tick=first_contains,
        )
        current_array_index = selection.tick_array_index
        first_contains = selection.first_tick_array_contains_pool_tick
        current_array = working_arrays[current_array_index]

        targeted = set_raydium_swap_loop_target(
            state,
            next_tick=selection.tick.tick_index,
            sqrt_price_limit_x64=sqrt_price_limit_x64,
        )
        prepared = prepare_raydium_swap_step(targeted)

        if prepared.result.sqrt_price_next_x64 == targeted.sqrt_price_next_x64:
            boundary: RaydiumSwapBoundaryResult = apply_raydium_swap_boundary(
                prepared,
                tick_array=current_array,
                next_tick=selection.tick,
                recent_epoch=recent_epoch,
                updated_reward_infos=updated_reward_infos,
                protocol_fee_rate=protocol_fee_rate,
                fund_fee_rate=fund_fee_rate,
                bitmap_extension=(
                    mutation_context.bitmap_extension
                    if mutation_context is not None else bitmap_extension
                ),
                mutation_context=mutation_context,
                prior_array_mutation=array_mutations.get(current_array_index),
            )
            state = boundary.state
            mutation_context = boundary.mutation
            array_mutations[current_array_index] = boundary.mutation
            working_arrays[current_array_index] = boundary.mutation.tick_array
            bitmap_extension = boundary.mutation.bitmap_extension
            boundaries += 1
        else:
            applied = apply_prepared_raydium_swap_step(
                prepared,
                protocol_fee_rate=protocol_fee_rate,
                fund_fee_rate=fund_fee_rate,
            )
            state = advance_raydium_dynamic_fee_after_tick(
                applied,
                tick_after_transition=applied.tick,
                is_skipped_tick_spacing=prepared.is_skipped_tick_spacing,
            )

        iterations += 1

    if not complete and iterations >= max_iterations:
        reason = "iteration_limit"
    return RaydiumSwapLoopExecution(
        state=state,
        updated_reward_infos=updated_reward_infos,
        tick_arrays=tuple(working_arrays),
        bitmap_extension=bitmap_extension,
        mutation=mutation_context,
        array_mutations=tuple(sorted(array_mutations.items())),
        iterations=iterations,
        boundary_transitions=boundaries,
        complete=complete,
        termination_reason=reason,
    )
