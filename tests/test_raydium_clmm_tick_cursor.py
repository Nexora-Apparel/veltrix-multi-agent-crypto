"""Source-vector tests for Raydium TickArrayState cursor direction semantics."""
from dataclasses import replace

import pytest

from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
    raydium_current_tick_array_start,
)
from veltrix.raydium_clmm_tick_cursor import (
    RaydiumTickCursorError,
    find_next_initialized_tick_in_array,
    next_tick_array_start_index,
)


def array(*, start=0, spacing=1, initialized=()):
    ticks = []
    for offset in range(RAYDIUM_TICK_ARRAY_SIZE):
        index = start + offset * spacing
        if offset in initialized:
            ticks.append(RaydiumClmmTick(
                tick_index=index,
                liquidity_net=1,
                liquidity_gross=1,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=True,
            ))
        else:
            ticks.append(RaydiumClmmTick(
                tick_index=index,
                liquidity_net=0,
                liquidity_gross=0,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=False,
            ))
    return RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address="11111111111111111111111111111111",
            start_tick_index=start,
        ),
        pool_address="11111111111111111111111111111111",
        start_tick_index=start,
        tick_spacing=spacing,
        initialized_tick_count=len(initialized),
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="0" * 64,
    )


def test_leftward_search_includes_current_tick_and_walks_backwards():
    ticks = array(initialized=(8, 10, 12))
    result = find_next_initialized_tick_in_array(
        ticks, current_tick_index=10, tick_spacing=1, zero_for_one=True
    )
    assert result.tick_offset == 10
    assert result.tick.tick_index == 10


def test_rightward_search_excludes_current_tick_and_walks_forwards():
    ticks = array(initialized=(8, 10, 12))
    result = find_next_initialized_tick_in_array(
        ticks, current_tick_index=10, tick_spacing=1, zero_for_one=False
    )
    assert result.tick_offset == 12
    assert result.tick.tick_index == 12


def test_tick_with_only_limit_orders_is_initialized_for_cursor_search():
    ticks = array()
    order_tick = replace(
        ticks.ticks[7],
        orders_amount=0,
        part_filled_orders_remaining=25,
        initialized=True,
    )
    slots = list(ticks.ticks)
    slots[7] = order_tick
    ticks = replace(ticks, ticks=tuple(slots), initialized_tick_count=1)
    result = find_next_initialized_tick_in_array(
        ticks, current_tick_index=7, tick_spacing=1, zero_for_one=True
    )
    assert result is not None
    assert result.tick.tick_index == 7
    assert result.tick.part_filled_orders_remaining == 25


def test_negative_tick_uses_floor_aligned_array_start_and_includes_left_boundary():
    start = raydium_current_tick_array_start(-1, 1)
    assert start == -60
    ticks = array(start=start, initialized=(59,))
    result = find_next_initialized_tick_in_array(
        ticks, current_tick_index=-1, tick_spacing=1, zero_for_one=True
    )
    assert result.tick_offset == 59
    assert result.tick.tick_index == -1
    assert find_next_initialized_tick_in_array(
        ticks, current_tick_index=-1, tick_spacing=1, zero_for_one=False
    ) is None


def test_non_owning_array_returns_none_like_pinned_rust_api():
    ticks = array(start=0, initialized=(1,))
    assert find_next_initialized_tick_in_array(
        ticks, current_tick_index=60, tick_spacing=1, zero_for_one=True
    ) is None


@pytest.mark.parametrize("zero_for_one,expected", [(True, -600), (False, 600)])
def test_next_tick_array_start_moves_exactly_one_array(zero_for_one, expected):
    assert next_tick_array_start_index(
        0, tick_spacing=10, zero_for_one=zero_for_one
    ) == expected


def test_cursor_rejects_invalid_spacing_direction_and_initialized_flag():
    ticks = array()
    with pytest.raises(RaydiumTickCursorError, match="spacing"):
        find_next_initialized_tick_in_array(
            ticks, current_tick_index=0, tick_spacing=0, zero_for_one=True
        )
    with pytest.raises(RaydiumTickCursorError, match="direction"):
        find_next_initialized_tick_in_array(
            ticks, current_tick_index=0, tick_spacing=1, zero_for_one=1
        )
    slots = list(ticks.ticks)
    slots[0] = replace(slots[0], liquidity_gross=1)
    malformed = replace(ticks, ticks=tuple(slots))
    with pytest.raises(RaydiumTickCursorError, match="initialized flag"):
        find_next_initialized_tick_in_array(
            malformed, current_tick_index=0, tick_spacing=1, zero_for_one=True
        )
