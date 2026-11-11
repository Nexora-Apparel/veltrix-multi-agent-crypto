"""Exact Raydium CLMM TickArrayState directional tick-search primitives.

Ported from the pinned source revision:
states/tick_array.rs::first_initialized_tick
states/tick_array.rs::next_initialized_tick
states/tick_array.rs::next_tick_arrary_start_index
states/tick_array.rs::get_array_start_index
"""
from __future__ import annotations

from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    raydium_current_tick_array_start,
)


class RaydiumTickArrayCursorError(ValueError):
    """Raised when a TickArrayState cannot be traversed safely."""


def _validate_array(tick_array: RaydiumClmmTickArray) -> None:
    if not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumTickArrayCursorError("decoded Raydium TickArrayState is required")
    if not isinstance(tick_array.ticks, tuple) or len(tick_array.ticks) != RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickArrayCursorError("TickArrayState must contain exactly 60 tick records")
    if isinstance(tick_array.tick_spacing, bool) or not isinstance(tick_array.tick_spacing, int) or not 1 <= tick_array.tick_spacing <= 0xFFFF:
        raise RaydiumTickArrayCursorError("TickArrayState tick spacing must fit a positive u16")
    if isinstance(tick_array.start_tick_index, bool) or not isinstance(tick_array.start_tick_index, int) or not -(1 << 31) <= tick_array.start_tick_index < (1 << 31):
        raise RaydiumTickArrayCursorError("TickArrayState start index must fit i32")
    expected_start = raydium_current_tick_array_start(
        tick_array.start_tick_index, tick_array.tick_spacing
    )
    if expected_start != tick_array.start_tick_index:
        raise RaydiumTickArrayCursorError("TickArrayState start index is not aligned to its spacing")


def first_initialized_tick(
    tick_array: RaydiumClmmTickArray,
    *,
    zero_for_one: bool,
) -> RaydiumClmmTick:
    """Return the highest initialized tick for zero-for-one, otherwise the lowest."""
    _validate_array(tick_array)
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickArrayCursorError("swap direction must be boolean")
    indexes = range(RAYDIUM_TICK_ARRAY_SIZE - 1, -1, -1) if zero_for_one else range(RAYDIUM_TICK_ARRAY_SIZE)
    for index in indexes:
        tick = tick_array.ticks[index]
        if not isinstance(tick, RaydiumClmmTick) or not isinstance(tick.initialized, bool):
            raise RaydiumTickArrayCursorError("TickArrayState contains a malformed tick record")
        if tick.initialized:
            expected_tick = tick_array.start_tick_index + index * tick_array.tick_spacing
            if tick.tick_index != expected_tick:
                raise RaydiumTickArrayCursorError("initialized tick index differs from its canonical array offset")
            return tick
    raise RaydiumTickArrayCursorError("TickArrayState contains no initialized tick")


def next_initialized_tick(
    tick_array: RaydiumClmmTickArray,
    *,
    current_tick_index: int,
    tick_spacing: int,
    zero_for_one: bool,
) -> RaydiumClmmTick | None:
    """Match Raydium's inclusive-left/exclusive-right directional tick search."""
    _validate_array(tick_array)
    if isinstance(current_tick_index, bool) or not isinstance(current_tick_index, int) or not -(1 << 31) <= current_tick_index < (1 << 31):
        raise RaydiumTickArrayCursorError("current tick index must fit i32")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or not 1 <= tick_spacing <= 0xFFFF:
        raise RaydiumTickArrayCursorError("tick spacing must fit a positive u16")
    if tick_spacing != tick_array.tick_spacing:
        raise RaydiumTickArrayCursorError("requested tick spacing differs from TickArrayState")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickArrayCursorError("swap direction must be boolean")

    current_array_start = raydium_current_tick_array_start(
        current_tick_index, tick_spacing
    )
    if current_array_start != tick_array.start_tick_index:
        return None
    offset = (current_tick_index - tick_array.start_tick_index) // tick_spacing
    if not 0 <= offset < RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickArrayCursorError("current tick offset is outside TickArrayState")

    indexes = range(offset, -1, -1) if zero_for_one else range(offset + 1, RAYDIUM_TICK_ARRAY_SIZE)
    for index in indexes:
        tick = tick_array.ticks[index]
        if not isinstance(tick, RaydiumClmmTick) or not isinstance(tick.initialized, bool):
            raise RaydiumTickArrayCursorError("TickArrayState contains a malformed tick record")
        if tick.initialized:
            expected_tick = tick_array.start_tick_index + index * tick_spacing
            if tick.tick_index != expected_tick:
                raise RaydiumTickArrayCursorError("initialized tick index differs from its canonical array offset")
            return tick
    return None


def next_tick_array_start_index(
    tick_array: RaydiumClmmTickArray,
    *,
    zero_for_one: bool,
) -> int:
    """Return the immediately adjacent array boundary in swap direction."""
    _validate_array(tick_array)
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickArrayCursorError("swap direction must be boolean")
    delta = RAYDIUM_TICK_ARRAY_SIZE * tick_array.tick_spacing
    result = tick_array.start_tick_index - delta if zero_for_one else tick_array.start_tick_index + delta
    if not -(1 << 31) <= result < (1 << 31):
        raise RaydiumTickArrayCursorError("next TickArrayState start index overflows i32")
    return result
