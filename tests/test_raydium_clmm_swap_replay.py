"""Single-array source-order regression for the complete Raydium swap orchestrator."""
from dataclasses import replace
import hashlib

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
)
from veltrix.raydium_clmm_pool_state import (
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
    derive_raydium_clmm_tick_array_address,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_swap_replay import run_raydium_clmm_swap_replay
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def fixture():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint_0, mint_1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    pool_key, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint_0), bytes(mint_1)],
        program,
    )
    observation_key, _ = derive_raydium_clmm_observation_address(str(pool_key))
    bitmap = [0] * 16
    bitmap[8] = 1  # default bitmap bit 512 represents TickArrayState start index 0
    pool_base = bytes(RAYDIUM_POOL_STATE_LENGTH)
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
        liquidity=1_000_000,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256=hashlib.sha256(pool_base).hexdigest(),
        tick_array_bitmap=tuple(bitmap),
        account_data=pool_base,
    )
    pool_bytes = encode_raydium_clmm_pool_state(pool)
    pool = replace(
        pool,
        account_data=pool_bytes,
        account_data_sha256=hashlib.sha256(pool_bytes).hexdigest(),
    )

    ticks = []
    for index in range(60):
        tick_index = index * pool.tick_spacing
        if index == 1:
            ticks.append(RaydiumClmmTick(
                tick_index=tick_index,
                liquidity_net=500,
                liquidity_gross=1000,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=True,
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
            start_tick_index=0,
        ),
        pool_address=pool.pool_address,
        start_tick_index=0,
        tick_spacing=pool.tick_spacing,
        initialized_tick_count=1,
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="0" * 64,
    )
    array_bytes = encode_raydium_clmm_tick_array(array)
    array = replace(array, data_sha256=hashlib.sha256(array_bytes).hexdigest())

    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="simulation-1",
        simulation_context_slot=100,
        provider_id="fixture-rpc",
        cluster="devnet",
        genesis_hash="fixture-genesis",
        context_slot=101,
        tick_arrays=(array,),
        cpi_account_order=(array.address,),
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


def test_single_array_swap_replay_crosses_tick_and_settles_atomically():
    pool, observation, snapshot = fixture()
    result = run_raydium_clmm_swap_replay(
        pool,
        observation,
        snapshot,
        amount_specified=1_000_000,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(10),
        zero_for_one=False,
        is_base_input=True,
        block_timestamp=1_700_000_000,
        recent_epoch=2,
        base_fee_rate=3_000,
        protocol_fee_rate=1_000,
        fund_fee_rate=500,
    )

    assert len(result.iterations) == 1
    assert result.iterations[0].reached_initialized_tick is True
    assert result.iterations[0].crossed_tick is True
    assert result.iterations[0].swap_complete is True
    assert result.pool.tick_current == 10
    assert result.pool.sqrt_price_x64 == get_sqrt_price_at_tick(10)
    assert result.pool.liquidity == 1_000_500
    assert result.fee_state.amount_specified_remaining > 0
    assert result.settlement.amount_0 > 0
    assert result.settlement.amount_1 > 0
    assert result.settlement.trade_fee_0 == 0
    assert result.settlement.trade_fee_1 > 0
    assert result.observation_state.initialized is True
    assert result.observation_state.observations[0].block_timestamp == 1_700_000_000
    assert result.observation_state.observations[0].tick_cumulative == 0
    updated_tick = result.tick_arrays[0].ticks[1]
    assert updated_tick.fee_growth_outside_1_x64 > 0
    assert result.tick_arrays[0].recent_epoch == 2
    assert result.tick_arrays[0].data_sha256 == hashlib.sha256(
        encode_raydium_clmm_tick_array(result.tick_arrays[0])
    ).hexdigest()
    assert result.pool.account_data_sha256 == hashlib.sha256(
        result.pool.account_data
    ).hexdigest()
    assert result.consumed_tick_array_addresses == (result.tick_arrays[0].address,)
    assert result.selected_tick_array_addresses == (result.tick_arrays[0].address,)
