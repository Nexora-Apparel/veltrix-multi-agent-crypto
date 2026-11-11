"""Exact Raydium CLMM TickArrayState directional tick-search primitives.

Ported from the pinned source revision:
states/tick_array.rs::first_initialized_tick
states/tick_array.rs::next_initialized_tick
states/tick_array.rs::next_tick_arrary_start_index
states/tick_array.rs::get_array_start_index
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
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


@dataclass(frozen=True, slots=True)
class RaydiumClmmTickArrayQueue:
    """Immutable equivalent of the ordered VecDeque consumed by swap_internal."""

    pool_address: str
    tick_spacing: int
    arrays: tuple[RaydiumClmmTickArray, ...]
    next_index: int = 0
    current: RaydiumClmmTickArray | None = None
    consumed_addresses: tuple[str, ...] = ()
    skipped_addresses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.pool_address, str) or not self.pool_address:
            raise RaydiumTickArrayCursorError("queue pool address must be non-empty")
        if isinstance(self.tick_spacing, bool) or not isinstance(self.tick_spacing, int) or not 1 <= self.tick_spacing <= 0xFFFF:
            raise RaydiumTickArrayCursorError("queue tick spacing must fit a positive u16")
        if not isinstance(self.arrays, tuple) or not self.arrays:
            raise RaydiumTickArrayCursorError("ordered tick-array account queue must not be empty")
        if isinstance(self.next_index, bool) or not isinstance(self.next_index, int) or not 0 <= self.next_index <= len(self.arrays):
            raise RaydiumTickArrayCursorError("tick-array queue cursor is outside the supplied account list")
        addresses = []
        starts = []
        for array in self.arrays:
            _validate_array(array)
            if array.pool_address != self.pool_address or array.tick_spacing != self.tick_spacing:
                raise RaydiumTickArrayCursorError("queued TickArrayState belongs to a different pool or spacing")
            addresses.append(array.address)
            starts.append(array.start_tick_index)
        if len(addresses) != len(set(addresses)) or len(starts) != len(set(starts)):
            raise RaydiumTickArrayCursorError("queued TickArrayState accounts contain duplicate addresses or starts")
        if len(self.consumed_addresses) != len(set(self.consumed_addresses)):
            raise RaydiumTickArrayCursorError("consumed TickArrayState addresses contain duplicates")
        if len(self.skipped_addresses) != len(set(self.skipped_addresses)):
            raise RaydiumTickArrayCursorError("skipped TickArrayState addresses contain duplicates")
        expected_consumed = tuple(addresses[:self.next_index])
        if self.consumed_addresses != expected_consumed:
            raise RaydiumTickArrayCursorError("consumed account addresses do not match the queue cursor")
        if self.current is None:
            if self.next_index != 0 or self.skipped_addresses:
                raise RaydiumTickArrayCursorError("unselected queue cannot have consumed or skipped accounts")
        elif (
            self.next_index == 0
            or self.current != self.arrays[self.next_index - 1]
            or self.skipped_addresses != tuple(addresses[:self.next_index - 1])
        ):
            raise RaydiumTickArrayCursorError("current and skipped accounts do not match the consumed queue prefix")

    @classmethod
    def from_snapshot(
        cls,
        *,
        pool_address: str,
        tick_spacing: int,
        snapshot: RaydiumClmmTickArraySnapshot,
    ) -> "RaydiumClmmTickArrayQueue":
        if not isinstance(snapshot, RaydiumClmmTickArraySnapshot):
            raise RaydiumTickArrayCursorError("verified CPI tick-array snapshot is required")
        array_addresses = tuple(item.address for item in snapshot.tick_arrays)
        cpi_array_order = tuple(
            address for address in snapshot.cpi_account_order
            if address != snapshot.bitmap_extension_address
        )
        if not snapshot.cpi_account_order or cpi_array_order != array_addresses:
            raise RaydiumTickArrayCursorError("tick-array snapshot does not preserve exact CPI account order")
        if snapshot.bitmap_extension_address is not None:
            if snapshot.bitmap_extension is None or snapshot.bitmap_extension.address != snapshot.bitmap_extension_address:
                raise RaydiumTickArrayCursorError("CPI bitmap extension address is not backed by decoded evidence")
            if snapshot.cpi_account_order.count(snapshot.bitmap_extension_address) != 1:
                raise RaydiumTickArrayCursorError("CPI bitmap extension must occur exactly once in account order")
        return cls(
            pool_address=pool_address,
            tick_spacing=tick_spacing,
            arrays=snapshot.tick_arrays,
        )

    def select_first(self, first_valid_start_index: int) -> "RaydiumClmmTickArrayQueue":
        """Pop in CPI order until the first bitmap-selected array is found."""
        if self.current is not None or self.next_index != 0 or self.consumed_addresses:
            raise RaydiumTickArrayCursorError("first tick array can only be selected from a fresh queue")
        if isinstance(first_valid_start_index, bool) or not isinstance(first_valid_start_index, int):
            raise RaydiumTickArrayCursorError("first valid tick-array start must be an integer")
        for index, array in enumerate(self.arrays):
            if array.pool_address != self.pool_address:
                raise RaydiumTickArrayCursorError("consumed tick array belongs to a different pool")
            consumed = tuple(item.address for item in self.arrays[:index + 1])
            if array.start_tick_index == first_valid_start_index:
                return replace(
                    self,
                    next_index=index + 1,
                    current=array,
                    consumed_addresses=consumed,
                    skipped_addresses=tuple(item.address for item in self.arrays[:index]),
                )
        raise RaydiumTickArrayCursorError("not enough CPI tick-array accounts for the first bitmap-selected array")

    def advance_to(self, next_start_index: int) -> "RaydiumClmmTickArrayQueue":
        """Pop the remaining CPI queue until the bitmap-selected next array is reached."""
        if self.current is None:
            raise RaydiumTickArrayCursorError("first tick array must be selected before advancing")
        if isinstance(next_start_index, bool) or not isinstance(next_start_index, int):
            raise RaydiumTickArrayCursorError("next tick-array start must be an integer")
        if self.current.start_tick_index == next_start_index:
            return self
        for index in range(self.next_index, len(self.arrays)):
            array = self.arrays[index]
            if array.pool_address != self.pool_address or array.tick_spacing != self.tick_spacing:
                raise RaydiumTickArrayCursorError("consumed next tick array belongs to a different pool or spacing")
            consumed = self.consumed_addresses + tuple(
                item.address for item in self.arrays[self.next_index:index + 1]
            )
            skipped = self.skipped_addresses + tuple(
                item.address for item in self.arrays[self.next_index:index]
            )
            if array.start_tick_index == next_start_index:
                return replace(
                    self,
                    next_index=index + 1,
                    current=array,
                    consumed_addresses=consumed,
                    skipped_addresses=skipped,
                )
        raise RaydiumTickArrayCursorError("not enough CPI tick-array accounts for the bitmap-selected next array")
