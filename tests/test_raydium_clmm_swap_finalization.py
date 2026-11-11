"""Round-trip tests for final Raydium CLMM account persistence."""
import hashlib
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RAYDIUM_OBSERVATION_RECORD_LENGTH,
    RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_swap_finalization import (
    RaydiumSwapFinalizationError,
    finalize_raydium_swap_state,
)
from veltrix.raydium_clmm_swap_loop_engine import execute_raydium_swap_loop
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET,
    RAYDIUM_TICK_ARRAY_TICKS_OFFSET,
    RAYDIUM_TICK_STATE_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def _put(data, offset, size, value):
    data[offset:offset + size] = value.to_bytes(size, "little")


def make_pool():
    pool_key = str(Pubkey.new_unique())
    mint0, mint1 = sorted((str(Pubkey.new_unique()), str(Pubkey.new_unique())), key=lambda x: bytes(Pubkey.from_string(x)))
    values = dict(
        pool_address=pool_key, bump=1, amm_config=str(Pubkey.new_unique()),
        pool_creator=str(Pubkey.new_unique()), token_mint_0=mint0,
        token_mint_1=mint1, token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()),
        observation_key=derive_raydium_clmm_observation_address(pool_key)[0], mint_decimals_0=6,
        mint_decimals_1=6, tick_spacing=10, liquidity=10_000,
        sqrt_price_x64=get_sqrt_price_at_tick(0), tick_current=0,
        status=0, fee_on=0, seed_index=0, account_data_sha256="",
        tick_array_bitmap=(0,) * 8 + (1,) + (0,) * 7,
    )
    raw = bytearray(RAYDIUM_POOL_STATE_LENGTH)
    raw[:8] = RAYDIUM_POOL_STATE_DISCRIMINATOR
    raw[201:233] = bytes(Pubkey.from_string(values["observation_key"]))
    _put(raw, 237, 16, values["liquidity"])
    _put(raw, 253, 16, values["sqrt_price_x64"])
    _put(raw, 269, 4, 0)
    for i, word in enumerate(values["tick_array_bitmap"]):
        _put(raw, RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + i * 8, 8, word)
    encoded = bytes(raw)
    return RaydiumClmmPoolState(
        **{**values, "account_data_sha256": hashlib.sha256(encoded).hexdigest(), "raw_data": encoded}
    )


def make_tick_array(pool):
    tick = RaydiumClmmTick(
        tick_index=10, liquidity_net=100, liquidity_gross=1000,
        orders_amount=0, part_filled_orders_remaining=0, initialized=True,
        fee_growth_outside_0_x64=2, fee_growth_outside_1_x64=3,
        reward_growths_outside_x64=(4, 5, 6),
    )
    empty = RaydiumClmmTick(
        tick_index=0, liquidity_net=0, liquidity_gross=0,
        orders_amount=0, part_filled_orders_remaining=0, initialized=False,
    )
    ticks = (empty, tick) + tuple(
        replace(empty, tick_index=index * 10)
        for index in range(2, 60)
    )
    raw = bytearray(RAYDIUM_TICK_ARRAY_DATA_LENGTH)
    raw[:8] = RAYDIUM_TICK_ARRAY_DISCRIMINATOR
    raw[8:40] = bytes(Pubkey.from_string(pool.pool_address))
    _put(raw, 40, 4, 0)
    offset = RAYDIUM_TICK_ARRAY_TICKS_OFFSET + RAYDIUM_TICK_STATE_SIZE
    _put(raw, offset, 4, 10)
    _put(raw, offset + 4, 16, 100)
    _put(raw, offset + 20, 16, 1000)
    _put(raw, offset + 36, 16, 2)
    _put(raw, offset + 52, 16, 3)
    for index, growth in enumerate((4, 5, 6)):
        _put(raw, offset + 68 + index * 16, 16, growth)
    raw[RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET] = 1
    _put(raw, 10_125, 8, 1)
    encoded = bytes(raw)
    return RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=0,
        ),
        pool_address=pool.pool_address, start_tick_index=0, tick_spacing=10,
        initialized_tick_count=1, recent_epoch=1, ticks=ticks,
        data_sha256=hashlib.sha256(encoded).hexdigest(), raw_data=encoded,
    )


def make_observation(pool):
    address, _ = derive_raydium_clmm_observation_address(pool.pool_address)
    raw = bytearray(RAYDIUM_OBSERVATION_STATE_LENGTH)
    raw[:8] = RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR
    raw[8] = 0
    _put(raw, 9, 8, 1)
    _put(raw, 17, 2, 0)
    raw[19:51] = bytes(Pubkey.from_string(pool.pool_address))
    encoded = bytes(raw)
    records = tuple(
        RaydiumClmmObservation(0, 0, bytes(32))
        for _ in range(RAYDIUM_OBSERVATION_COUNT)
    )
    return RaydiumClmmObservationState(
        address=address, initialized=False, recent_epoch=1,
        observation_index=0, pool_id=pool.pool_address,
        observations=records, padding=bytes(32),
        data_sha256=hashlib.sha256(encoded).hexdigest(), raw_data=encoded,
    )


def test_finalizer_serializes_pool_tick_array_rewards_and_observation():
    pool = make_pool()
    array = make_tick_array(pool)
    traversal = RaydiumClmmBitmapTraversalResult(
        passed=True, pool_address=pool.pool_address, zero_for_one=False,
        current_array_start_index=0, first_initialized_array_start_index=0,
        last_cpi_array_start_index=0, expected_initialized_array_starts=(0,),
        supplied_initialized_array_starts=(0,), supplied_array_starts=(0,),
        bitmap_extension_sha256=None, bounded_interval_complete=True,
    )
    loop = execute_raydium_swap_loop(
        pool, (array,), traversal, amount_specified=100_000,
        trade_fee_rate=3000, zero_for_one=False, is_base_input=True,
        block_timestamp=100, sqrt_price_limit_x64=get_sqrt_price_at_tick(10),
        protocol_fee_rate=1000, fund_fee_rate=500, recent_epoch=2,
    )

    result = finalize_raydium_swap_state(loop, make_observation(pool))

    assert result.pool.tick_current == 10
    assert result.pool.sqrt_price_x64 == get_sqrt_price_at_tick(10)
    assert result.pool.liquidity == 10_100
    assert result.pool_data == result.pool.raw_data
    assert hashlib.sha256(result.pool_data).hexdigest() == result.pool.account_data_sha256
    assert len(result.tick_array_data) == 1
    assert result.tick_array_data[0] == result.tick_arrays[0].raw_data
    assert hashlib.sha256(result.tick_array_data[0]).hexdigest() == result.tick_arrays[0].data_sha256
    assert result.observation.initialized is True
    assert result.observation.observations[0].block_timestamp == 100
    assert result.observation_data == result.observation.raw_data
    assert result.settlement.amount_0 > 0
    assert result.settlement.amount_1 > 0


def test_finalizer_rejects_missing_original_pool_bytes():
    pool = make_pool()
    array = make_tick_array(pool)
    traversal = RaydiumClmmBitmapTraversalResult(
        passed=True, pool_address=pool.pool_address, zero_for_one=False,
        current_array_start_index=0, first_initialized_array_start_index=0,
        last_cpi_array_start_index=0, expected_initialized_array_starts=(0,),
        supplied_initialized_array_starts=(0,), supplied_array_starts=(0,),
        bitmap_extension_sha256=None, bounded_interval_complete=True,
    )
    loop = execute_raydium_swap_loop(
        pool, (array,), traversal, amount_specified=100_000,
        trade_fee_rate=3000, zero_for_one=False, is_base_input=True,
        block_timestamp=100, sqrt_price_limit_x64=get_sqrt_price_at_tick(10),
        protocol_fee_rate=0, fund_fee_rate=0, recent_epoch=2,
    )
    loop = replace(
        loop,
        mutation_session=replace(
            loop.mutation_session,
            pool=replace(loop.mutation_session.pool, raw_data=b""),
        ),
    )
    with pytest.raises(RaydiumSwapFinalizationError, match="raw account bytes"):
        finalize_raydium_swap_state(loop, make_observation(pool))
