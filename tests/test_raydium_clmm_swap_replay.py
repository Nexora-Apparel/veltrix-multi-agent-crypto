"""Single-array source-order regression for the complete Raydium swap orchestrator."""
from dataclasses import replace
import hashlib
import pytest

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
)
from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmDynamicFeeInfo,
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolState,
    derive_raydium_clmm_pool_address,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
    RaydiumClmmTickArrayBitmapExtension as SnapshotBitmapExtension,
    derive_raydium_clmm_tick_array_address,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_swap_replay import run_raydium_clmm_swap_replay
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension as MutationBitmapExtension,
    derive_raydium_tick_array_bitmap_extension_address,
    encode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def fixture(zero_for_one=False, dynamic_fee_enabled=False, fifo_order_amount=0, multi_array=False, empty_array_after_order=False, bitmap_extension=False):
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint_0, mint_1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    pool_key, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint_0), bytes(mint_1)],
        program,
    )
    observation_key, _ = derive_raydium_clmm_observation_address(str(pool_key))
    bitmap = [0] * 16
    bitmap[8] = 0 if bitmap_extension else (3 if multi_array else 1)
    pool_base = bytes(RAYDIUM_POOL_STATE_LENGTH)
    current_tick = 400_000 if bitmap_extension else (590 if multi_array else (10 if zero_for_one else 0))
    pool = RaydiumClmmPoolState(
        pool_address=str(pool_key),
        bump=bump,
        amm_config=str(config),
        pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(mint_0),
        token_mint_1=str(mint_1),
        token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()),
        observation_key=observation_key,
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=10,
        liquidity=1_000_000_000_000 if bitmap_extension else 1_000_000,
        sqrt_price_x64=get_sqrt_price_at_tick(current_tick),
        tick_current=current_tick,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256=hashlib.sha256(pool_base).hexdigest(),
        tick_array_bitmap=tuple(bitmap),
        dynamic_fee_info=(
            RaydiumClmmDynamicFeeInfo(
                filter_period=60,
                decay_period=3600,
                reduction_factor=5000,
                dynamic_fee_control=1000,
                max_volatility_accumulator=10000,
                tick_spacing_index_reference=0,
                volatility_reference=100,
                volatility_accumulator=200,
                last_update_timestamp=1_699_999_900,
                padding=bytes(46),
            )
            if dynamic_fee_enabled
            else RaydiumClmmDynamicFeeInfo()
        ),
        account_data=pool_base,
    )
    pool_bytes = encode_raydium_clmm_pool_state(pool)
    pool = replace(
        pool,
        account_data=pool_bytes,
        account_data_sha256=hashlib.sha256(pool_bytes).hexdigest(),
    )

    array_specs = (
        [(399_600, 41, 0, 0, 100)]
        if bitmap_extension
        else ([(0, 58, 0, 100, 0), (600, 0, 500, 1000, 0)]
        if multi_array
        else [(0, 0 if zero_for_one else 1, 0 if empty_array_after_order else 500, 0 if empty_array_after_order else 1000, fifo_order_amount)])
    )
    arrays = []
    for start_tick_index, initialized_slot, liquidity_net, liquidity_gross, order_amount in array_specs:
        ticks = []
        for index in range(60):
            tick_index = start_tick_index + index * pool.tick_spacing
            if index == initialized_slot:
                ticks.append(RaydiumClmmTick(
                    tick_index=tick_index,
                    liquidity_net=liquidity_net,
                    liquidity_gross=liquidity_gross,
                    orders_amount=order_amount,
                    part_filled_orders_remaining=0,
                    initialized=True,
                    order_phase=0,
                    unfilled_ratio_x64=(1 << 64) if order_amount else 0,
                ))
            else:
                ticks.append(RaydiumClmmTick(
                    tick_index=tick_index,
                    liquidity_net=0,
                    liquidity_gross=0,
                    orders_amount=0,
                    part_filled_orders_remaining=0,
                    initialized=False,
                ))
        array = RaydiumClmmTickArray(
            address=derive_raydium_clmm_tick_array_address(
                pool_address=pool.pool_address,
                start_tick_index=start_tick_index,
            ),
            pool_address=pool.pool_address,
            start_tick_index=start_tick_index,
            tick_spacing=pool.tick_spacing,
            initialized_tick_count=1,
            recent_epoch=1,
            ticks=tuple(ticks),
            data_sha256="0" * 64,
        )
        raw = encode_raydium_clmm_tick_array(array)
        arrays.append(replace(array, data_sha256=hashlib.sha256(raw).hexdigest()))

    snapshot_extension = None
    extension_address = None
    extension_digest = None
    if bitmap_extension:
        extension_address = derive_raydium_tick_array_bitmap_extension_address(pool.pool_address)
        positive = [[0] * 8 for _ in range(14)]
        negative = [[0] * 8 for _ in range(14)]
        positive[0][2] |= 1 << 26  # positive extension bit 154 => TickArrayState start 399600
        mutation_extension = MutationBitmapExtension(
            address=extension_address,
            pool_address=pool.pool_address,
            positive_bitmaps=tuple(tuple(row) for row in positive),
            negative_bitmaps=tuple(tuple(row) for row in negative),
            data_sha256="0" * 64,
        )
        extension_raw = encode_raydium_tick_array_bitmap_extension(mutation_extension)
        extension_digest = hashlib.sha256(extension_raw).hexdigest()
        snapshot_extension = SnapshotBitmapExtension(
            address=extension_address,
            pool_address=pool.pool_address,
            positive_bitmaps=tuple(word for row in positive for word in row),
            negative_bitmaps=tuple(word for row in negative for word in row),
            data_sha256=extension_digest,
            data_bytes=extension_raw,
        )

    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="simulation-1",
        simulation_context_slot=100,
        provider_id="fixture-rpc",
        cluster="devnet",
        genesis_hash="fixture-genesis",
        context_slot=101,
        tick_arrays=tuple(arrays),
        cpi_account_order=tuple(array.address for array in arrays) + ((extension_address,) if extension_address else ()),
        bitmap_extension_address=extension_address,
        bitmap_extension_sha256=extension_digest,
        bitmap_extension=snapshot_extension,
    )
    observation = RaydiumClmmObservationState(
        address=observation_key,
        initialized=False,
        recent_epoch=1,
        observation_index=0,
        pool_id=pool.pool_address,
        observations=tuple(
            RaydiumClmmObservation(0, 0, bytes(32))
            for _ in range(RAYDIUM_OBSERVATION_COUNT)
        ),
        padding=bytes(32),
        data_sha256="0" * 64,
    )
    return pool, observation, snapshot


@pytest.mark.parametrize(
    ("zero_for_one", "is_base_input", "amount_specified", "dynamic_fee_enabled", "fifo_order_amount", "multi_array", "empty_array_after_order", "bitmap_extension"),
    [
        (False, True, 1_000_000, False, 0, False, False, False),
        (True, True, 1_000_000, False, 0, False, False, False),
        (False, False, 100, False, 0, False, False, False),
        (False, True, 1_000_000, True, 0, False, False, False),
        (False, True, 1_000_000, False, 100, False, False, False),
        (False, True, 1_000_000, False, 0, True, False, False),
        (False, True, 1_000_000, False, 100, False, True, False),
        (False, True, 1_000_000_000_000_000_000, False, 1, False, False, True),
    ],
)
def test_single_array_swap_replay_matches_direction_and_amount_mode(
    zero_for_one,
    is_base_input,
    amount_specified,
    dynamic_fee_enabled,
    fifo_order_amount,
    multi_array,
    empty_array_after_order,
    bitmap_extension,
):
    pool, observation, snapshot = fixture(zero_for_one, dynamic_fee_enabled, fifo_order_amount, multi_array, empty_array_after_order, bitmap_extension)
    result = run_raydium_clmm_swap_replay(
        pool,
        observation,
        snapshot,
        amount_specified=amount_specified,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(400_010 if bitmap_extension else (600 if multi_array else (0 if zero_for_one else 10))),
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        block_timestamp=1_700_000_000,
        recent_epoch=2,
        base_fee_rate=3_000,
        protocol_fee_rate=1_000,
        fund_fee_rate=500,
    )

    assert result.iterations
    assert result.iterations[-1].swap_complete is True
    assert result.pool.recent_epoch == 2
    assert (result.iterations[-1].dynamic_fee_state is not None) is dynamic_fee_enabled
    assert result.settlement.amount_0 > 0
    assert result.settlement.amount_1 > 0
    assert result.observation_state.initialized is True
    assert result.observation_state.observations[0].block_timestamp == 1_700_000_000
    assert result.observation_state.observations[0].tick_cumulative == 0

    if bitmap_extension:
        assert result.iterations[-1].reached_initialized_tick is True
        assert result.iterations[-1].crossed_tick is False
        assert result.pool.liquidity == 1_000_000_000_000
        assert result.pool.tick_array_bitmap[8] == 0
        assert result.tick_arrays[0].initialized_tick_count == 0
        assert result.tick_arrays[0].recent_epoch == 2
        assert result.bitmap_extension is not None
        assert not ((result.bitmap_extension.positive_bitmaps[2] >> 26) & 1)
        assert hashlib.sha256(result.bitmap_extension.data_bytes).hexdigest() == result.bitmap_extension.data_sha256
    elif empty_array_after_order or bitmap_extension:
        assert result.iterations[-1].reached_initialized_tick is True
        assert result.iterations[-1].crossed_tick is False
        assert result.pool.liquidity == 1_000_000
        assert result.pool.tick_array_bitmap[8] == 0
        assert result.tick_arrays[0].initialized_tick_count == 0
        assert result.tick_arrays[0].ticks[1].initialized is False
        assert result.tick_arrays[0].recent_epoch == 2
    elif multi_array:
        assert result.iterations[-1].reached_initialized_tick is True
        assert result.iterations[-1].crossed_tick is True
        assert result.pool.tick_current == 600
        assert result.pool.sqrt_price_x64 == get_sqrt_price_at_tick(600)
        assert result.pool.liquidity == 1_000_500
        assert result.tick_arrays[0].recent_epoch == 1
        assert result.tick_arrays[1].recent_epoch == 2
        assert result.tick_arrays[0].ticks[58].tick_index == 580
        assert result.tick_arrays[1].ticks[0].fee_growth_outside_1_x64 > 0
    elif is_base_input:
        target_tick = 0 if zero_for_one else 10
        assert result.iterations[-1].reached_initialized_tick is True
        assert result.iterations[-1].crossed_tick is True
        assert result.pool.tick_current == (target_tick - 1 if zero_for_one else target_tick)
        assert result.pool.sqrt_price_x64 == get_sqrt_price_at_tick(target_tick)
        assert result.pool.liquidity == (999_500 if zero_for_one else 1_000_500)
        assert result.fee_state.amount_specified_remaining > 0
        assert result.tick_arrays[0].recent_epoch == 2
    else:
        assert result.iterations[-1].reached_initialized_tick is False
        assert result.iterations[-1].crossed_tick is False
        assert result.fee_state.amount_specified_remaining == 0
        assert result.settlement.amount_0 == amount_specified
        assert result.pool.liquidity == 1_000_000
        assert result.tick_arrays[0].recent_epoch == 1

    if zero_for_one:
        assert result.settlement.trade_fee_0 > 0
        assert result.settlement.trade_fee_1 == 0
    else:
        assert result.settlement.trade_fee_0 == 0
        assert result.settlement.trade_fee_1 > 0

    updated_tick = result.tick_arrays[0].ticks[41] if bitmap_extension else (result.tick_arrays[1].ticks[0] if multi_array else result.tick_arrays[0].ticks[0 if zero_for_one else 1])
    if is_base_input and not multi_array and not empty_array_after_order and not bitmap_extension:
        assert updated_tick.fee_growth_outside_0_x64 > 0 if zero_for_one else updated_tick.fee_growth_outside_1_x64 > 0
    elif empty_array_after_order:
        assert updated_tick.liquidity_gross == 0
    else:
        assert updated_tick.liquidity_gross == 1000
    if fifo_order_amount:
        assert updated_tick.order_phase == 1
        assert updated_tick.orders_amount == 0
        assert updated_tick.part_filled_orders_remaining == 0
        assert updated_tick.unfilled_ratio_x64 == 0

    for array in result.tick_arrays:
        assert array.data_sha256 == hashlib.sha256(encode_raydium_clmm_tick_array(array)).hexdigest()
    assert result.pool.account_data_sha256 == hashlib.sha256(
        result.pool.account_data
    ).hexdigest()
    assert result.consumed_tick_array_addresses == tuple(array.address for array in result.tick_arrays)
    assert result.selected_tick_array_addresses == tuple(array.address for array in result.tick_arrays)
