from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_tick_array_cursor import (
    RaydiumTickArrayCursorError,
    RaydiumClmmTickArrayQueue,
    initialize_raydium_tick_array_queue,
    select_next_tick_for_swap,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
)


def key():
    return str(Pubkey.new_unique())


def build_pool(*, current_tick, spacing, initialized_starts):
    words = [0] * 16
    count = RAYDIUM_TICK_ARRAY_SIZE * spacing
    for start in initialized_starts:
        bit = start // count + 512
        words[bit // 64] |= 1 << (bit % 64)
    return RaydiumClmmPoolState(
        pool_address=key(),
        bump=1,
        amm_config=key(),
        pool_creator=key(),
        token_mint_0=key(),
        token_mint_1=key(),
        token_vault_0=key(),
        token_vault_1=key(),
        observation_key=key(),
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=spacing,
        liquidity=1000,
        sqrt_price_x64=1 << 64,
        tick_current=current_tick,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="0" * 64,
        tick_array_bitmap=tuple(words),
    )


def build_array(pool_address, start, spacing, offsets):
    initialized = set(offsets)
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
        address=f"TickArray{start}",
        pool_address=pool_address,
        start_tick_index=start,
        tick_spacing=spacing,
        initialized_tick_count=len(initialized),
        recent_epoch=1,
        ticks=ticks,
        data_sha256="0" * 64,
    )


def snapshot(*arrays):
    addresses = tuple(item.address for item in arrays)
    return RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=tuple(arrays),
        cpi_account_order=addresses,
    )


def test_initial_array_selection_uses_current_bitmap_and_directional_fallback():
    p = build_pool(current_tick=5, spacing=15, initialized_starts=(0,))
    current = build_array(p.pool_address, 0, 15, (0, 2, 7))
    queue, contains_current = initialize_raydium_tick_array_queue(
        p, tick_array_snapshot=snapshot(current), zero_for_one=False
    )
    assert contains_current is True
    assert queue.current == current

    p_without_current = build_pool(current_tick=5, spacing=15, initialized_starts=(900,))
    next_array = build_array(p_without_current.pool_address, 900, 15, (0,))
    queue, contains_current = initialize_raydium_tick_array_queue(
        p_without_current,
        tick_array_snapshot=snapshot(next_array),
        zero_for_one=False,
    )
    assert contains_current is False
    assert queue.current == next_array


def test_swap_loop_composition_advances_to_next_bitmap_array_after_current_array_exhaustion():
    p = build_pool(current_tick=5, spacing=15, initialized_starts=(0, 900))
    current = build_array(p.pool_address, 0, 15, (0, 2, 7))
    following = build_array(p.pool_address, 900, 15, (0,))
    tick_snapshot = snapshot(current, following)
    queue, contains_current = initialize_raydium_tick_array_queue(
        p, tick_array_snapshot=tick_snapshot, zero_for_one=False
    )
    assert contains_current is True

    step = select_next_tick_for_swap(
        p,
        tick_array_snapshot=tick_snapshot,
        queue=queue,
        current_tick_index=105,
        zero_for_one=False,
        first_tick_array_contains_pool_tick=contains_current,
    )
    assert step.tick.tick_index == 900
    assert step.array_advanced is True
    assert step.next_array_start_index == 900
    assert step.queue.current == following
    assert step.queue.consumed_addresses == (current.address, following.address)


def test_swap_loop_uses_first_initialized_tick_when_first_array_excludes_pool_tick():
    p = build_pool(current_tick=900, spacing=15, initialized_starts=(0,))
    first = build_array(p.pool_address, 0, 15, (0, 2, 7))
    tick_snapshot = snapshot(first)
    queue, contains_current = initialize_raydium_tick_array_queue(
        p, tick_array_snapshot=tick_snapshot, zero_for_one=True
    )
    assert contains_current is False
    step = select_next_tick_for_swap(
        p,
        tick_array_snapshot=tick_snapshot,
        queue=queue,
        current_tick_index=900,
        zero_for_one=True,
        first_tick_array_contains_pool_tick=contains_current,
    )
    assert step.tick.tick_index == 105
    assert step.array_advanced is False
    assert step.first_tick_array_contains_pool_tick is True


def test_swap_loop_rejects_queue_from_another_pool():
    p = build_pool(current_tick=5, spacing=15, initialized_starts=(0,))
    other = build_pool(current_tick=5, spacing=15, initialized_starts=(0,))
    array = build_array(other.pool_address, 0, 15, (0,))
    tick_snapshot = snapshot(array)
    queue = RaydiumClmmTickArrayQueue.from_snapshot(
        pool_address=other.pool_address, tick_spacing=15, snapshot=tick_snapshot
    ).select_first(0)
    with pytest.raises(RaydiumTickArrayCursorError, match="does not belong"):
        select_next_tick_for_swap(
            p,
            tick_array_snapshot=tick_snapshot,
            queue=queue,
            current_tick_index=5,
            zero_for_one=False,
            first_tick_array_contains_pool_tick=True,
        )
