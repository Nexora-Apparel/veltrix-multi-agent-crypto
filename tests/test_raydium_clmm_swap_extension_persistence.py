"""End-to-end extension-bitmap mutation and serialization regression."""
import hashlib
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_swap_finalization import finalize_raydium_swap_state
from veltrix.raydium_clmm_swap_loop_engine import execute_raydium_swap_loop
from veltrix.raydium_clmm_tick_array_mutation import (
    TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
    RaydiumClmmTickArrayBitmapExtension,
    derive_raydium_tick_array_bitmap_extension_address,
)
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


def put(data, offset, size, value):
    data[offset:offset + size] = value.to_bytes(size, "little")


def fixture():
    pool_key = str(Pubkey.new_unique())
    mint0, mint1 = sorted(
        (str(Pubkey.new_unique()), str(Pubkey.new_unique())),
        key=lambda key: bytes(Pubkey.from_string(key)),
    )
    observation_key = derive_raydium_clmm_observation_address(pool_key)[0]
    pool_values = dict(
        pool_address=pool_key, bump=1, amm_config=str(Pubkey.new_unique()),
        pool_creator=str(Pubkey.new_unique()), token_mint_0=mint0,
        token_mint_1=mint1, token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()), observation_key=observation_key,
        mint_decimals_0=6, mint_decimals_1=6, tick_spacing=1,
        liquidity=10_000, sqrt_price_x64=get_sqrt_price_at_tick(30720),
        tick_current=30720, status=0, fee_on=0, seed_index=0,
        account_data_sha256="", source_commit=RAYDIUM_CLMM_SOURCE_COMMIT,
        tick_array_bitmap=(0,) * 16,
    )
    pool_raw = bytearray(RAYDIUM_POOL_STATE_LENGTH)
    pool_raw[:8] = RAYDIUM_POOL_STATE_DISCRIMINATOR
    pool_raw[201:233] = bytes(Pubkey.from_string(observation_key))
    put(pool_raw, 237, 16, pool_values["liquidity"])
    put(pool_raw, 253, 16, pool_values["sqrt_price_x64"])
    put(pool_raw, 269, 4, pool_values["tick_current"])
    pool_raw_bytes = bytes(pool_raw)
    pool = RaydiumClmmPoolState(
        **{
            **pool_values,
            "account_data_sha256": hashlib.sha256(pool_raw_bytes).hexdigest(),
            "raw_data": pool_raw_bytes,
        }
    )

    order_tick = RaydiumClmmTick(
        tick_index=30721, liquidity_net=0, liquidity_gross=0,
        orders_amount=1, part_filled_orders_remaining=0, initialized=True,
    )
    empty = RaydiumClmmTick(
        tick_index=0, liquidity_net=0, liquidity_gross=0,
        orders_amount=0, part_filled_orders_remaining=0, initialized=False,
    )
    ticks = (empty, order_tick) + (empty,) * 58
    array_raw = bytearray(RAYDIUM_TICK_ARRAY_DATA_LENGTH)
    array_raw[:8] = RAYDIUM_TICK_ARRAY_DISCRIMINATOR
    array_raw[8:40] = bytes(Pubkey.from_string(pool_key))
    put(array_raw, 40, 4, 30720)
    tick_offset = RAYDIUM_TICK_ARRAY_TICKS_OFFSET + RAYDIUM_TICK_STATE_SIZE
    put(array_raw, tick_offset, 4, 30721)
    put(array_raw, tick_offset + 124, 8, 1)
    array_raw[RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET] = 1
    put(array_raw, 10_125, 8, 1)
    array_bytes = bytes(array_raw)
    array = RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool_key, start_tick_index=30720,
        ),
        pool_address=pool_key, start_tick_index=30720, tick_spacing=10,
        initialized_tick_count=1, recent_epoch=1, ticks=ticks,
        data_sha256=hashlib.sha256(array_bytes).hexdigest(),
        raw_data=array_bytes,
    )

    positive = [[0] * 8 for _ in range(14)]
    positive[0][0] = 1
    negative = [[0] * 8 for _ in range(14)]
    extension_address = derive_raydium_tick_array_bitmap_extension_address(pool_key)
    extension_raw = bytearray(
        TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR + bytes(Pubkey.from_string(pool_key))
    )
    for side in (positive, negative):
        for row in side:
            for word in row:
                extension_raw.extend(word.to_bytes(8, "little"))
    extension_bytes = bytes(extension_raw)
    extension = RaydiumClmmTickArrayBitmapExtension(
        address=extension_address,
        pool_address=pool_key,
        positive_bitmaps=tuple(tuple(row) for row in positive),
        negative_bitmaps=tuple(tuple(row) for row in negative),
        data_sha256=hashlib.sha256(extension_bytes).hexdigest(),
    )

    traversal = RaydiumClmmBitmapTraversalResult(
        passed=True, pool_address=pool_key, zero_for_one=False,
        current_array_start_index=30720,
        first_initialized_array_start_index=30720,
        last_cpi_array_start_index=30720,
        expected_initialized_array_starts=(30720,),
        supplied_initialized_array_starts=(30720,),
        supplied_array_starts=(30720,),
        bitmap_extension_sha256=extension.data_sha256,
        bounded_interval_complete=True,
    )

    observation_raw = bytearray(RAYDIUM_OBSERVATION_STATE_LENGTH)
    observation_raw[:8] = RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR
    observation_raw[19:51] = bytes(Pubkey.from_string(pool_key))
    observation_bytes = bytes(observation_raw)
    observation = RaydiumClmmObservationState(
        address=observation_key, initialized=False, recent_epoch=0,
        observation_index=0, pool_id=pool_key,
        observations=tuple(
            RaydiumClmmObservation(0, 0, bytes(32))
            for _ in range(RAYDIUM_OBSERVATION_COUNT)
        ),
        padding=bytes(32),
        data_sha256=hashlib.sha256(observation_bytes).hexdigest(),
        raw_data=observation_bytes,
    )
    return pool, array, extension, traversal, observation


def test_extension_bitmap_last_tick_removal_serializes_zero_bit_and_updated_hash():
    pool, array, extension, traversal, observation = fixture()
    result = execute_raydium_swap_loop(
        pool, (array,), traversal,
        amount_specified=100_000,
        trade_fee_rate=3000,
        zero_for_one=False,
        is_base_input=True,
        block_timestamp=100,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(30721),
        protocol_fee_rate=0,
        fund_fee_rate=0,
        recent_epoch=2,
        bitmap_extension=extension,
    )
    assert result.boundary_count == 1
    assert result.stop_reason == "price_limit_reached"
    final = finalize_raydium_swap_state(result, observation)

    assert final.bitmap_extension is not None
    assert final.bitmap_extension_data is not None
    assert final.source_bitmap_extension_sha256 == extension.data_sha256
    assert final.bitmap_extension.data_sha256 != extension.data_sha256
    assert final.bitmap_extension.positive_bitmaps[0][0] & 1 == 0
    assert final.tick_arrays[0].initialized_tick_count == 0
    assert final.tick_arrays[0].ticks[1].initialized is False
    assert final.tick_array_data[0][RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET] == 0
    assert hashlib.sha256(final.bitmap_extension_data).hexdigest() == final.bitmap_extension.data_sha256
