import pytest

from veltrix.raydium_clmm_tick_array_cursor import (
    RaydiumTickArrayCursorError,
    RaydiumClmmTickArrayQueue,
    first_initialized_tick,
    next_initialized_tick,
    next_tick_array_start_index,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
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


def build_snapshot(arrays):
    addresses = tuple(item.address for item in arrays)
    return RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim-response",
        simulation_context_slot=100,
        provider_id="rpc-fixture",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=tuple(arrays),
        cpi_account_order=addresses,
    )


def test_tick_array_queue_consumes_cpi_accounts_in_source_order():
    arrays = tuple(build_tick_array(start, 15, [0]) for start in (0, 900, 1800, 2700))
    queue = RaydiumClmmTickArrayQueue.from_snapshot(
        pool_address=arrays[0].pool_address,
        tick_spacing=15,
        snapshot=build_snapshot(arrays),
    )
    selected = queue.select_first(900)
    assert selected.current == arrays[1]
    assert selected.next_index == 2
    assert selected.consumed_addresses == (arrays[0].address, arrays[1].address)
    assert selected.skipped_addresses == (arrays[0].address,)

    advanced = selected.advance_to(2700)
    assert advanced.current == arrays[3]
    assert advanced.next_index == 4
    assert advanced.consumed_addresses == tuple(item.address for item in arrays)
    assert advanced.skipped_addresses == (arrays[0].address, arrays[2].address)


def test_tick_array_queue_does_not_consume_an_account_when_target_is_current():
    arrays = tuple(build_tick_array(start, 15, [0]) for start in (0, 900))
    queue = RaydiumClmmTickArrayQueue.from_snapshot(
        pool_address=arrays[0].pool_address,
        tick_spacing=15,
        snapshot=build_snapshot(arrays),
    ).select_first(0)
    assert queue.advance_to(0) is queue
    assert queue.next_index == 1


def test_tick_array_queue_rejects_missing_first_or_next_account():
    arrays = tuple(build_tick_array(start, 15, [0]) for start in (0, 900))
    queue = RaydiumClmmTickArrayQueue.from_snapshot(
        pool_address=arrays[0].pool_address,
        tick_spacing=15,
        snapshot=build_snapshot(arrays),
    )
    with pytest.raises(RaydiumTickArrayCursorError, match="first bitmap-selected"):
        queue.select_first(1800)
    selected = queue.select_first(0)
    with pytest.raises(RaydiumTickArrayCursorError, match="next array"):
        selected.advance_to(1800)


def test_tick_array_queue_rejects_wrong_pool_and_cpi_order():
    arrays = tuple(build_tick_array(start, 15, [0]) for start in (0, 900))
    wrong_pool = RaydiumClmmTickArray(
        address=arrays[1].address,
        pool_address="OtherPool11111111111111111111111111111",
        start_tick_index=arrays[1].start_tick_index,
        tick_spacing=arrays[1].tick_spacing,
        initialized_tick_count=arrays[1].initialized_tick_count,
        recent_epoch=arrays[1].recent_epoch,
        ticks=arrays[1].ticks,
        data_sha256=arrays[1].data_sha256,
    )
    with pytest.raises(RaydiumTickArrayCursorError, match="different pool"):
        RaydiumClmmTickArrayQueue.from_snapshot(
            pool_address=arrays[0].pool_address,
            tick_spacing=15,
            snapshot=build_snapshot((arrays[0], wrong_pool)),
        )
    reversed_snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim-response",
        simulation_context_slot=100,
        provider_id="rpc-fixture",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=arrays,
        cpi_account_order=(arrays[1].address, arrays[0].address),
    )
    with pytest.raises(RaydiumTickArrayCursorError, match="CPI account order"):
        RaydiumClmmTickArrayQueue.from_snapshot(
            pool_address=arrays[0].pool_address,
            tick_spacing=15,
            snapshot=reversed_snapshot,
        )
