import pytest

from veltrix.raydium_clmm_tick_array_cursor import (
    RaydiumTickArrayCursorError,
    first_initialized_tick,
    next_initialized_tick,
    next_tick_array_start_index,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
)


def build_tick_array(start, spacing, initialized_offsets):
    initialized = set(initialized_offsets)
    ticks = tuple(
        RaydiumClmmTick(
            tick_index=start + index * spacing,
            liquidity_net=0,
            liquidity_gross=1 if index in initialized else 0,
            orders_amount=0,
            part_filled_orders_remaining=0,
            initialized=index in initialized,
        )
        for index in range(RAYDIUM_TICK_ARRAY_SIZE)
    )
    return RaydiumClmmTickArray(
        address="TickArrayFixture111111111111111111111111",
        pool_address="PoolFixture1111111111111111111111111111",
        start_tick_index=start,
        tick_spacing=spacing,
        initialized_tick_count=len(initialized),
        recent_epoch=1,
        ticks=ticks,
        data_sha256="0" * 64,
    )


def test_first_initialized_tick_matches_raydium_directional_order():
    array = build_tick_array(-900, 15, [40, 59])
    assert first_initialized_tick(array, zero_for_one=False).tick_index == -300
    assert first_initialized_tick(array, zero_for_one=True).tick_index == -15


@pytest.mark.parametrize(
    "current,direction,expected",
    [
        (0, True, 0),
        (1, True, 0),
        (29, True, 0),
        (30, True, 30),
        (31, True, 30),
        (0, False, 30),
        (29, False, 30),
        (30, False, 105),
        (31, False, 105),
        (-900, True, -900),
        (-899, True, -900),
        (-871, True, -900),
        (-870, True, -870),
        (-869, True, -870),
        (-900, False, -870),
        (-871, False, -870),
        (-870, False, -795),
        (-869, False, -795),
    ],
)
def test_next_initialized_tick_matches_pinned_rust_vectors(current, direction, expected):
    start = 0 if current >= 0 else -900
    array = build_tick_array(start, 15, [0, 2, 7])
    result = next_initialized_tick(
        array,
        current_tick_index=current,
        tick_spacing=15,
        zero_for_one=direction,
    )
    assert result is not None
    assert result.tick_index == expected


@pytest.mark.parametrize(
    "current,direction",
    [(105, False), (-795, False), (900, False), (-10, False)],
)
def test_next_initialized_tick_returns_none_at_array_end_or_outside(current, direction):
    start = 0 if current >= 0 else -900
    array = build_tick_array(start, 15, [0, 2, 7])
    assert next_initialized_tick(
        array,
        current_tick_index=current,
        tick_spacing=15,
        zero_for_one=direction,
    ) is None


def test_next_initialized_tick_uses_inclusive_left_and_exclusive_right_boundaries():
    array = build_tick_array(0, 15, [0, 2, 7])
    assert next_initialized_tick(
        array, current_tick_index=30, tick_spacing=15, zero_for_one=True
    ).tick_index == 30
    assert next_initialized_tick(
        array, current_tick_index=30, tick_spacing=15, zero_for_one=False
    ).tick_index == 105


def test_tick_array_start_uses_floor_semantics_for_negative_ticks():
    array = build_tick_array(-900, 15, [0])
    assert next_tick_array_start_index(array, zero_for_one=True) == -1800
    assert next_tick_array_start_index(array, zero_for_one=False) == 0


def test_tick_array_cursor_rejects_misaligned_spacing_and_malformed_tick_offsets():
    array = build_tick_array(0, 15, [2])
    with pytest.raises(RaydiumTickArrayCursorError, match="spacing"):
        next_initialized_tick(
            array, current_tick_index=0, tick_spacing=10, zero_for_one=False
        )
    bad_ticks = list(array.ticks)
    bad_ticks[2] = RaydiumClmmTick(
        tick_index=31,
        liquidity_net=0,
        liquidity_gross=1,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=True,
    )
    bad = RaydiumClmmTickArray(
        address=array.address,
        pool_address=array.pool_address,
        start_tick_index=array.start_tick_index,
        tick_spacing=array.tick_spacing,
        initialized_tick_count=1,
        recent_epoch=array.recent_epoch,
        ticks=tuple(bad_ticks),
        data_sha256=array.data_sha256,
    )
    with pytest.raises(RaydiumTickArrayCursorError, match="canonical array offset"):
        next_initialized_tick(
            bad, current_tick_index=0, tick_spacing=15, zero_for_one=False
        )


def test_first_initialized_tick_rejects_empty_tick_array():
    array = build_tick_array(0, 15, [])
    with pytest.raises(RaydiumTickArrayCursorError, match="no initialized tick"):
        first_initialized_tick(array, zero_for_one=False)
