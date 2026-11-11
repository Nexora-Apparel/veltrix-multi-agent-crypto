"""Pinned Raydium CLMM next-initialized-tick selection.

This is the first source-ordered swap-loop component, ported from
TickArrayState::next_initialized_tick/first_initialized_tick and
swap_internal's first-array fallback and bitmap-directed array advance at
Raydium CLMM commit 6d7a0f82d2f293c553c69aaf943377367c6c4cc6.

It selects a tick only. It does not execute a swap or establish execution readiness.
"""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    raydium_current_tick_array_start,
)

SOURCE_COMMIT = "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"


class RaydiumSwapLoopSelectionError(ValueError):
    """Raised when source-ordered tick-array traversal is incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumNextTickSelection:
    tick: RaydiumClmmTick
    tick_array_index: int
    tick_array_start_index: int
    first_tick_array_contains_pool_tick: bool
    advanced_tick_array: bool


def _validate_inputs(
    *,
    tick_arrays: tuple[RaydiumClmmTickArray, ...],
    traversal: RaydiumClmmBitmapTraversalResult,
    tick_current: int,
    tick_spacing: int,
    zero_for_one: bool,
) -> None:
    if not isinstance(traversal, RaydiumClmmBitmapTraversalResult):
        raise RaydiumSwapLoopSelectionError("verified Raydium bitmap traversal result is required")
    if traversal.passed is not True or traversal.bounded_interval_complete is not True:
        raise RaydiumSwapLoopSelectionError("bitmap traversal must be passed and interval-complete")
    if not isinstance(tick_arrays, tuple) or not tick_arrays:
        raise RaydiumSwapLoopSelectionError("ordered decoded tick arrays are required")
    if not isinstance(zero_for_one, bool):
        raise RaydiumSwapLoopSelectionError("swap direction must be boolean")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumSwapLoopSelectionError("tick spacing must be a positive u16")
    if isinstance(tick_current, bool) or not isinstance(tick_current, int) or not -(1 << 31) <= tick_current < (1 << 31):
        raise RaydiumSwapLoopSelectionError("current tick must fit i32")

    starts = tuple(item.start_tick_index for item in tick_arrays)
    if starts != traversal.supplied_array_starts:
        raise RaydiumSwapLoopSelectionError("decoded tick-array order differs from the verified CPI account order")
    if starts != traversal.expected_initialized_array_starts:
        raise RaydiumSwapLoopSelectionError("decoded tick arrays differ from the verified bitmap-selected sequence")
    if traversal.zero_for_one is not zero_for_one:
        raise RaydiumSwapLoopSelectionError("bitmap traversal direction differs from swap direction")
    if any(
        not isinstance(item, RaydiumClmmTickArray)
        or item.pool_address != traversal.pool_address
        or item.tick_spacing != tick_spacing
        or len(item.ticks) != RAYDIUM_TICK_ARRAY_SIZE
        for item in tick_arrays
    ):
        raise RaydiumSwapLoopSelectionError("tick-array pool, spacing or 60-slot layout is inconsistent")

    stride = RAYDIUM_TICK_ARRAY_SIZE * tick_spacing
    if any(right - left != (-stride if zero_for_one else stride) for left, right in zip(starts, starts[1:])):
        raise RaydiumSwapLoopSelectionError("tick arrays are not contiguous in the pinned swap direction")
    if raydium_current_tick_array_start(tick_current, tick_spacing) != starts[0]:
        # The first CPI array is not necessarily the array containing the current tick.
        # The source's first-valid-array lookup may advance before entering the loop.
        if not (
            starts[0] < raydium_current_tick_array_start(tick_current, tick_spacing)
            if zero_for_one
            else starts[0] > raydium_current_tick_array_start(tick_current, tick_spacing)
        ):
            raise RaydiumSwapLoopSelectionError("first CPI tick array is not directionally reachable from the current tick")


def _first_initialized(array: RaydiumClmmTickArray, zero_for_one: bool) -> RaydiumClmmTick:
    indexes = range(RAYDIUM_TICK_ARRAY_SIZE - 1, -1, -1) if zero_for_one else range(RAYDIUM_TICK_ARRAY_SIZE)
    for index in indexes:
        tick = array.ticks[index]
        if tick.initialized:
            return tick
    raise RaydiumSwapLoopSelectionError("bitmap-selected tick array contains no initialized tick")


def select_next_initialized_tick(
    *,
    tick_arrays: tuple[RaydiumClmmTickArray, ...],
    traversal: RaydiumClmmBitmapTraversalResult,
    current_tick: int,
    current_tick_array_index: int,
    tick_spacing: int,
    zero_for_one: bool,
    first_tick_array_contains_pool_tick: bool,
) -> RaydiumNextTickSelection:
    """Match swap_internal's in-array search, first-array fallback and bitmap advance.

    The caller must supply the exact ordered arrays accepted by the bitmap traversal
    verifier. The function never guesses or synthesizes a missing tick array.
    """
    _validate_inputs(
        tick_arrays=tick_arrays,
        traversal=traversal,
        tick_current=current_tick,
        tick_spacing=tick_spacing,
        zero_for_one=zero_for_one,
    )
    if not isinstance(first_tick_array_contains_pool_tick, bool):
        raise RaydiumSwapLoopSelectionError("first-array containment flag must be boolean")
    if isinstance(current_tick_array_index, bool) or not isinstance(current_tick_array_index, int) or not 0 <= current_tick_array_index < len(tick_arrays):
        raise RaydiumSwapLoopSelectionError("current tick-array index is outside the supplied CPI sequence")

    array = tick_arrays[current_tick_array_index]
    if array.start_tick_index != traversal.expected_initialized_array_starts[current_tick_array_index]:
        raise RaydiumSwapLoopSelectionError("current tick-array index does not match the verified traversal sequence")

    if raydium_current_tick_array_start(current_tick, tick_spacing) == array.start_tick_index:
        offset = (current_tick - array.start_tick_index) // tick_spacing
        indexes = (
            range(offset, -1, -1)
            if zero_for_one
            else range(offset + 1, RAYDIUM_TICK_ARRAY_SIZE)
        )
        for index in indexes:
            tick = array.ticks[index]
            if tick.initialized:
                return RaydiumNextTickSelection(
                    tick=tick,
                    tick_array_index=current_tick_array_index,
                    tick_array_start_index=array.start_tick_index,
                    first_tick_array_contains_pool_tick=first_tick_array_contains_pool_tick,
                    advanced_tick_array=False,
                )

    if not first_tick_array_contains_pool_tick:
        tick = _first_initialized(array, zero_for_one)
        return RaydiumNextTickSelection(
            tick=tick,
            tick_array_index=current_tick_array_index,
            tick_array_start_index=array.start_tick_index,
            first_tick_array_contains_pool_tick=True,
            advanced_tick_array=False,
        )

    next_index = current_tick_array_index + 1
    if next_index >= len(tick_arrays):
        raise RaydiumSwapLoopSelectionError("bitmap traversal has no next initialized tick array; liquidity is insufficient")
    next_array = tick_arrays[next_index]
    tick = _first_initialized(next_array, zero_for_one)
    return RaydiumNextTickSelection(
        tick=tick,
        tick_array_index=next_index,
        tick_array_start_index=next_array.start_tick_index,
        first_tick_array_contains_pool_tick=True,
        advanced_tick_array=True,
    )
