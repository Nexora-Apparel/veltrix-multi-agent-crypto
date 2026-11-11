"""Source-vector tests for Raydium swap-loop tick selection."""
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick, RaydiumClmmTickArray
from veltrix.raydium_clmm_swap_loop import (
    RaydiumSwapLoopSelectionError,
    select_next_initialized_tick,
)

POOL = str(Pubkey.new_unique())


def tick(index, initialized=True):
    return RaydiumClmmTick(
        tick_index=index,
        liquidity_net=0,
        liquidity_gross=100 if initialized else 0,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=initialized,
    )


def array(start, spacing, initialized_indexes):
    ticks = tuple(
        tick(start + index * spacing, index in initialized_indexes)
        for index in range(60)
    )
    return RaydiumClmmTickArray(
        address=str(Pubkey.new_unique()),
        pool_address=POOL,
        start_tick_index=start,
        tick_spacing=spacing,
        initialized_tick_count=len(initialized_indexes),
        recent_epoch=1,
        ticks=ticks,
        data_sha256="a" * 64,
    )


def traversal(starts, zero_for_one):
    return RaydiumClmmBitmapTraversalResult(
        passed=True,
        pool_address=POOL,
        zero_for_one=zero_for_one,
        current_array_start_index=starts[0],
        first_initialized_array_start_index=starts[0],
        last_cpi_array_start_index=starts[-1],
        expected_initialized_array_starts=tuple(starts),
        supplied_initialized_array_starts=tuple(starts),
        supplied_array_starts=tuple(starts),
        bitmap_extension_sha256=None,
        bounded_interval_complete=True,
        execution_ready=False,
    )


def test_zero_for_one_search_includes_current_tick_and_scans_downward():
    arrays = (array(0, 1, {2, 5, 9}),)
    result = select_next_initialized_tick(
        tick_arrays=arrays, traversal=traversal([0], True),
        current_tick=5, current_tick_array_index=0, tick_spacing=1,
        zero_for_one=True, first_tick_array_contains_pool_tick=True,
    )
    assert result.tick.tick_index == 5
    assert result.advanced_tick_array is False


def test_one_for_zero_search_is_strictly_above_current_tick():
    arrays = (array(0, 1, {5, 6, 10}),)
    result = select_next_initialized_tick(
        tick_arrays=arrays, traversal=traversal([0], False),
        current_tick=5, current_tick_array_index=0, tick_spacing=1,
        zero_for_one=False, first_tick_array_contains_pool_tick=True,
    )
    assert result.tick.tick_index == 6


def test_first_array_fallback_uses_directional_first_tick_once():
    arrays = (array(60, 1, {1, 12, 58}), array(120, 1, {3}))
    result = select_next_initialized_tick(
        tick_arrays=arrays, traversal=traversal([60, 120], False),
        current_tick=0, current_tick_array_index=0, tick_spacing=1,
        zero_for_one=False, first_tick_array_contains_pool_tick=False,
    )
    assert result.tick.tick_index == 61
    assert result.first_tick_array_contains_pool_tick is True
    assert result.advanced_tick_array is False


def test_zero_for_one_first_array_fallback_selects_highest_initialized_tick():
    arrays = (array(-120, 1, {1, 12, 58}), array(-180, 1, {3}))
    result = select_next_initialized_tick(
        tick_arrays=arrays, traversal=traversal([-120, -180], True),
        current_tick=-60, current_tick_array_index=0, tick_spacing=1,
        zero_for_one=True, first_tick_array_contains_pool_tick=False,
    )
    assert result.tick.tick_index == -62
    assert result.first_tick_array_contains_pool_tick is True


def test_empty_current_array_advances_to_next_bitmap_selected_array():
    arrays = (array(0, 1, set()), array(60, 1, {4, 50}))
    result = select_next_initialized_tick(
        tick_arrays=arrays, traversal=traversal([0, 60], False),
        current_tick=10, current_tick_array_index=0, tick_spacing=1,
        zero_for_one=False, first_tick_array_contains_pool_tick=True,
    )
    assert result.tick.tick_index == 64
    assert result.tick_array_index == 1
    assert result.advanced_tick_array is True


def test_rejects_cpi_order_that_differs_from_bitmap_proof():
    arrays = (array(0, 1, {5}), array(60, 1, {4}))
    proof = traversal([0, 60], False)
    bad_proof = replace(proof, supplied_array_starts=(60, 0))
    with pytest.raises(RaydiumSwapLoopSelectionError, match="CPI account order"):
        select_next_initialized_tick(
            tick_arrays=arrays, traversal=bad_proof,
            current_tick=5, current_tick_array_index=0, tick_spacing=1,
            zero_for_one=False, first_tick_array_contains_pool_tick=True,
        )


def test_bitmap_selected_arrays_may_skip_empty_array_intervals():
    arrays = (array(0, 1, set()), array(180, 1, {4}))
    result = select_next_initialized_tick(
        tick_arrays=arrays, traversal=traversal([0, 180], False),
        current_tick=10, current_tick_array_index=0, tick_spacing=1,
        zero_for_one=False, first_tick_array_contains_pool_tick=True,
    )
    assert result.tick.tick_index == 184
    assert result.tick_array_index == 1


def test_rejects_first_array_fallback_after_initial_array():
    arrays = (array(0, 1, {5}), array(60, 1, {4}))
    with pytest.raises(RaydiumSwapLoopSelectionError, match="initial CPI array"):
        select_next_initialized_tick(
            tick_arrays=arrays, traversal=traversal([0, 60], False),
            current_tick=65, current_tick_array_index=1, tick_spacing=1,
            zero_for_one=False, first_tick_array_contains_pool_tick=False,
        )


def test_rejects_missing_next_tick_array_instead_of_guessing():
    arrays = (array(0, 1, set()),)
    with pytest.raises(RaydiumSwapLoopSelectionError, match="no next initialized"):
        select_next_initialized_tick(
            tick_arrays=arrays, traversal=traversal([0], False),
            current_tick=10, current_tick_array_index=0, tick_spacing=1,
            zero_for_one=False, first_tick_array_contains_pool_tick=True,
        )
