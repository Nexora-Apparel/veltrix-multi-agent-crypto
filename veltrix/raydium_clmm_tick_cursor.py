"""Pinned Raydium TickArrayState next-initialized-tick cursor semantics."""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_pool_state import RAYDIUM_MAX_TICK, RAYDIUM_MIN_TICK
from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_replay_bundle import RaydiumClmmReplayAccountBundle
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    raydium_current_tick_array_start,
)


class RaydiumTickCursorError(ValueError):
    """Raised when a tick-array cursor request violates the pinned source domain."""


@dataclass(frozen=True, slots=True)
class RaydiumInitializedTickCursor:
    tick_array_start_index: int
    tick_offset: int
    tick: RaydiumClmmTick
    zero_for_one: bool


def find_next_initialized_tick_in_array(
    tick_array: RaydiumClmmTickArray,
    *,
    current_tick_index: int,
    tick_spacing: int,
    zero_for_one: bool,
) -> RaydiumInitializedTickCursor | None:
    """Mirror TickArrayState::next_initialized_tick at the pinned Raydium commit.

    Leftward swaps search [0, current_offset] in reverse, including the
    current tick. Rightward swaps search (current_offset, 60), excluding it.
    A tick is initialized when it has liquidity or outstanding/part-filled
    limit orders, exactly as TickState::is_initialized defines.
    """
    if not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumTickCursorError("decoded Raydium TickArrayState is required")
    if isinstance(current_tick_index, bool) or not isinstance(current_tick_index, int) or not RAYDIUM_MIN_TICK <= current_tick_index <= RAYDIUM_MAX_TICK:
        raise RaydiumTickCursorError("current tick is outside the Raydium protocol range")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumTickCursorError("tick spacing must be a positive u16")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickCursorError("swap direction must be boolean")
    if tick_array.tick_spacing != tick_spacing:
        raise RaydiumTickCursorError("TickArrayState spacing differs from the requested cursor spacing")
    if not isinstance(tick_array.ticks, tuple) or len(tick_array.ticks) != RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickCursorError("TickArrayState must contain exactly 60 ticks")

    expected_start = raydium_current_tick_array_start(
        current_tick_index, tick_spacing
    )
    if expected_start != tick_array.start_tick_index:
        # This matches the pinned source API: a non-owning array returns None.
        return None

    offset = (current_tick_index - tick_array.start_tick_index) // tick_spacing
    if not 0 <= offset < RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickCursorError("current tick resolves outside its owning tick array")

    indices = (
        range(offset, -1, -1)
        if zero_for_one
        else range(offset + 1, RAYDIUM_TICK_ARRAY_SIZE)
    )
    for index in indices:
        tick = tick_array.ticks[index]
        if not isinstance(tick, RaydiumClmmTick):
            raise RaydiumTickCursorError("TickArrayState contains a malformed tick")
        expected_tick = tick_array.start_tick_index + index * tick_spacing
        if tick.tick_index not in (0, expected_tick):
            raise RaydiumTickCursorError("tick index does not match its array slot")
        initialized = (
            tick.liquidity_gross > 0
            or tick.orders_amount > 0
            or tick.part_filled_orders_remaining > 0
        )
        if tick.initialized is not initialized:
            raise RaydiumTickCursorError("tick initialized flag disagrees with liquidity/order fields")
        if initialized:
            if tick.tick_index != expected_tick:
                raise RaydiumTickCursorError("initialized tick index is not canonical for its array slot")
            return RaydiumInitializedTickCursor(
                tick_array_start_index=tick_array.start_tick_index,
                tick_offset=index,
                tick=tick,
                zero_for_one=zero_for_one,
            )
    return None



def first_initialized_tick_in_array(
    tick_array: RaydiumClmmTickArray,
    *,
    zero_for_one: bool,
) -> RaydiumInitializedTickCursor:
    """Mirror TickArrayState::first_initialized_tick for a non-owning first array."""
    if not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumTickCursorError("decoded Raydium TickArrayState is required")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickCursorError("swap direction must be boolean")
    if not isinstance(tick_array.ticks, tuple) or len(tick_array.ticks) != RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickCursorError("TickArrayState must contain exactly 60 ticks")
    indices = (
        range(RAYDIUM_TICK_ARRAY_SIZE - 1, -1, -1)
        if zero_for_one
        else range(RAYDIUM_TICK_ARRAY_SIZE)
    )
    for index in indices:
        tick = tick_array.ticks[index]
        if not isinstance(tick, RaydiumClmmTick):
            raise RaydiumTickCursorError("TickArrayState contains a malformed tick")
        expected_tick = (
            tick_array.start_tick_index + index * tick_array.tick_spacing
        )
        initialized = (
            tick.liquidity_gross > 0
            or tick.orders_amount > 0
            or tick.part_filled_orders_remaining > 0
        )
        if tick.initialized is not initialized:
            raise RaydiumTickCursorError("tick initialized flag disagrees with liquidity/order fields")
        if initialized:
            if tick.tick_index != expected_tick:
                raise RaydiumTickCursorError("initialized tick index is not canonical for its array slot")
            return RaydiumInitializedTickCursor(
                tick_array_start_index=tick_array.start_tick_index,
                tick_offset=index,
                tick=tick,
                zero_for_one=zero_for_one,
            )
    raise RaydiumTickCursorError("tick array has no initialized tick")


def select_next_initialized_tick_in_first_array(
    tick_array: RaydiumClmmTickArray,
    *,
    current_tick_index: int,
    tick_spacing: int,
    zero_for_one: bool,
    first_tick_array_contains_pool_tick: bool,
) -> RaydiumInitializedTickCursor | None:
    """Mirror the first-array branch at the start of Raydium swap_internal."""
    if not isinstance(first_tick_array_contains_pool_tick, bool):
        raise RaydiumTickCursorError("first-array ownership flag must be boolean")
    if first_tick_array_contains_pool_tick:
        return find_next_initialized_tick_in_array(
            tick_array,
            current_tick_index=current_tick_index,
            tick_spacing=tick_spacing,
            zero_for_one=zero_for_one,
        )
    return first_initialized_tick_in_array(
        tick_array, zero_for_one=zero_for_one
    )


def next_tick_array_start_index(
    tick_array_start_index: int,
    *,
    tick_spacing: int,
    zero_for_one: bool,
) -> int:
    """Mirror TickArrayState::next_tick_arrary_start_index."""
    if isinstance(tick_array_start_index, bool) or not isinstance(tick_array_start_index, int) or not -(1 << 31) <= tick_array_start_index < (1 << 31):
        raise RaydiumTickCursorError("tick-array start index must fit i32")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumTickCursorError("tick spacing must be a positive u16")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickCursorError("swap direction must be boolean")
    delta = RAYDIUM_TICK_ARRAY_SIZE * tick_spacing
    result = tick_array_start_index - delta if zero_for_one else tick_array_start_index + delta
    if not -(1 << 31) <= result < (1 << 31):
        raise RaydiumTickCursorError("next tick-array start index overflows i32")
    return result


@dataclass(frozen=True, slots=True)
class RaydiumBitmapTickSelection:
    cursor: RaydiumInitializedTickCursor
    sequence_index: int
    visited_array_starts: tuple[int, ...]


def select_next_initialized_tick_from_bitmap_sequence(
    bundle: RaydiumClmmReplayAccountBundle,
    traversal: RaydiumClmmBitmapTraversalResult,
    *,
    current_tick_index: int,
    first_tick_array_contains_pool_tick: bool,
) -> RaydiumBitmapTickSelection:
    """Mirror swap_internal's first-array, then next-bitmap-array selection order."""
    if not isinstance(bundle, RaydiumClmmReplayAccountBundle):
        raise RaydiumTickCursorError("validated Raydium replay account bundle is required")
    if not isinstance(traversal, RaydiumClmmBitmapTraversalResult) or traversal.passed is not True:
        raise RaydiumTickCursorError("passed bitmap traversal proof is required")
    if traversal.pool_address != bundle.pool.pool_address:
        raise RaydiumTickCursorError("bitmap traversal proof belongs to a different pool")
    if traversal.zero_for_one not in (True, False):
        raise RaydiumTickCursorError("bitmap traversal direction is invalid")
    if not traversal.bounded_interval_complete:
        raise RaydiumTickCursorError("bitmap traversal interval is not complete")
    expected = traversal.expected_initialized_array_starts
    if (
        not isinstance(expected, tuple)
        or not expected
        or expected != traversal.supplied_initialized_array_starts
        or expected != traversal.supplied_array_starts
    ):
        raise RaydiumTickCursorError("bitmap traversal proof does not contain the exact ordered initialized-array sequence")
    if not isinstance(first_tick_array_contains_pool_tick, bool):
        raise RaydiumTickCursorError("first-array ownership flag must be boolean")
    if not RAYDIUM_MIN_TICK <= current_tick_index <= RAYDIUM_MAX_TICK:
        raise RaydiumTickCursorError("current tick is outside the Raydium protocol range")

    by_start = {item.start_tick_index: item for item in bundle.tick_arrays}
    if len(by_start) != len(bundle.tick_arrays) or set(by_start) != set(expected):
        raise RaydiumTickCursorError("replay bundle tick arrays differ from the bitmap-proven CPI sequence")
    if first_tick_array_contains_pool_tick:
        expected_current = raydium_current_tick_array_start(
            current_tick_index, bundle.pool.tick_spacing
        )
        if expected_current != expected[0]:
            raise RaydiumTickCursorError("first-array ownership flag conflicts with the current pool tick")

    visited = []
    first_array = by_start[expected[0]]
    first_result = select_next_initialized_tick_in_first_array(
        first_array,
        current_tick_index=current_tick_index,
        tick_spacing=bundle.pool.tick_spacing,
        zero_for_one=traversal.zero_for_one,
        first_tick_array_contains_pool_tick=first_tick_array_contains_pool_tick,
    )
    visited.append(expected[0])
    if first_result is not None:
        return RaydiumBitmapTickSelection(
            cursor=first_result,
            sequence_index=0,
            visited_array_starts=tuple(visited),
        )

    for sequence_index, start in enumerate(expected[1:], start=1):
        array = by_start[start]
        result = first_initialized_tick_in_array(
            array, zero_for_one=traversal.zero_for_one
        )
        visited.append(start)
        return RaydiumBitmapTickSelection(
            cursor=result,
            sequence_index=sequence_index,
            visited_array_starts=tuple(visited),
        )
    raise RaydiumTickCursorError(
        "bitmap proves initialized arrays but no next initialized tick exists in the supplied direction"
    )
