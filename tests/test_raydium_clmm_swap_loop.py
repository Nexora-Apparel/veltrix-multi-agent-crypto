"""Source-order tests for Raydium swap-loop pre-step composition."""
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeSwapState,
    compute_total_fee_rate,
)
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
    encode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmDynamicFeeInfo,
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
    derive_raydium_clmm_pool_address,
    encode_raydium_clmm_pool_state,
    RaydiumClmmPoolState,
    RaydiumClmmRewardInfo,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_tick_cursor import RaydiumInitializedTickCursor
from veltrix.raydium_clmm_swap_loop import (
    RaydiumSwapLoopError,
    compute_raydium_swap_loop_pre_step,
    apply_raydium_swap_loop_boundary,
    finalize_raydium_swap_loop,
)
from veltrix.raydium_clmm_tick_math import Q64, get_sqrt_price_at_tick


def fee_state(**overrides):
    values = dict(
        amount_specified_remaining=100_000_000,
        amount_calculated=0,
        liquidity=10**12,
        fee_growth_global_x64=0,
        lp_fee=0,
        protocol_fee=0,
        fund_fee=0,
    )
    values.update(overrides)
    return RaydiumFeeAccountingState(**values)


def dynamic_state(**overrides):
    values = dict(
        filter_period=60,
        decay_period=3600,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=100_000,
        tick_spacing_index_reference=0,
        volatility_reference=10_000,
        volatility_accumulator=0,
        last_update_timestamp=1000,
        padding=bytes(46),
    )
    values.update(overrides)
    info = RaydiumClmmDynamicFeeInfo(**values)
    return RaydiumDynamicFeeSwapState(
        info=info,
        tick_spacing=60,
        base_fee_rate=1000,
        tick_spacing_index=1,
    )


def test_static_fee_pre_step_computes_swap_and_applies_amount_accounting():
    result = compute_raydium_swap_loop_pre_step(
        fee_state(),
        sqrt_price_current_x64=Q64,
        target_price_x64=get_sqrt_price_at_tick(-100),
        base_fee_rate=3000,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=100_000,
        fund_fee_rate=50_000,
    )
    assert result.total_fee_rate == 3000
    assert result.dynamic_fee_state is None
    assert result.price_changed is True
    assert result.swap_step.amount_in > 0
    assert result.swap_step.amount_out > 0
    assert result.fee_accounting_state.amount_specified_remaining < 100_000_000
    assert result.fee_accounting_state.lp_fee > 0
    assert result.fee_accounting_state.protocol_fee >= 0
    assert result.fee_accounting_state.fund_fee >= 0


def test_dynamic_fee_accumulator_is_updated_before_fee_rate_and_spacing_bound():
    original = dynamic_state()
    result = compute_raydium_swap_loop_pre_step(
        fee_state(),
        sqrt_price_current_x64=Q64,
        target_price_x64=get_sqrt_price_at_tick(120),
        base_fee_rate=1000,
        zero_for_one=False,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=100_000,
        fund_fee_rate=50_000,
        dynamic_fee_state=original,
    )
    assert result.dynamic_fee_state.info.volatility_accumulator > original.info.volatility_accumulator
    assert result.total_fee_rate == compute_total_fee_rate(
        base_fee_rate=1000,
        info=result.dynamic_fee_state.info,
        tick_spacing=60,
    )
    assert result.spacing_bound.reached_target is False
    assert result.spacing_bound.bounded_tick == 120
    assert result.spacing_bound.sqrt_price_x64 == get_sqrt_price_at_tick(120)
    assert result.price_changed is True


def test_no_price_change_does_not_apply_swap_amounts():
    original = fee_state(liquidity=0)
    result = compute_raydium_swap_loop_pre_step(
        original,
        sqrt_price_current_x64=Q64,
        target_price_x64=Q64,
        base_fee_rate=1000,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        dynamic_fee_state=dynamic_state(),
    )
    assert result.price_changed is False
    assert result.swap_step.amount_in == 0
    assert result.swap_step.amount_out == 0
    assert result.fee_accounting_state == original
    assert result.spacing_bound.sqrt_price_x64 == Q64


def test_boundary_fee_rate_is_returned_before_dynamic_index_advance():
    result = compute_raydium_swap_loop_pre_step(
        fee_state(),
        sqrt_price_current_x64=Q64,
        target_price_x64=get_sqrt_price_at_tick(120),
        base_fee_rate=1000,
        zero_for_one=False,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        dynamic_fee_state=dynamic_state(),
    )
    # This is the fee rate the caller must pass to FIFO boundary matching.
    assert result.total_fee_rate >= 1000
    assert result.dynamic_fee_state.tick_spacing_index == 1


def test_rejects_invalid_domains_and_dynamic_base_fee_mismatch():
    args = dict(
        sqrt_price_current_x64=Q64,
        target_price_x64=get_sqrt_price_at_tick(-10),
        base_fee_rate=1000,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
    )
    with pytest.raises(RaydiumSwapLoopError, match="base fee"):
        compute_raydium_swap_loop_pre_step(
            fee_state(), **{**args, "base_fee_rate": 1_000_000}
        )
    with pytest.raises(RaydiumSwapLoopError, match="base rate"):
        compute_raydium_swap_loop_pre_step(
            fee_state(),
            **args,
            dynamic_fee_state=replace(dynamic_state(), base_fee_rate=2000),
        )
    with pytest.raises(RaydiumSwapLoopError, match="denominator"):
        compute_raydium_swap_loop_pre_step(
            fee_state(), **{**args, "protocol_fee_rate": 800_000, "fund_fee_rate": 300_000}
        )


def boundary_fixture(*, liquidity_gross=100, liquidity_net=25, orders=10):
    pool_address = str(Pubkey.new_unique())
    pool = RaydiumClmmPoolState(
        pool_address=pool_address,
        bump=1,
        amm_config=str(Pubkey.new_unique()),
        pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()),
        token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()),
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=1,
        liquidity=1000,
        sqrt_price_x64=Q64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="a" * 64,
        tick_array_bitmap=(0,) * 8 + (1,) + (0,) * 7,
    )
    ticks = []
    for index in range(RAYDIUM_TICK_ARRAY_SIZE):
        if index == 0:
            ticks.append(RaydiumClmmTick(
                tick_index=0,
                liquidity_net=liquidity_net,
                liquidity_gross=liquidity_gross,
                orders_amount=orders,
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
    tick_array = RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool_address, start_tick_index=0
        ),
        pool_address=pool_address,
        start_tick_index=0,
        tick_spacing=1,
        initialized_tick_count=1,
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="b" * 64,
    )
    cursor = RaydiumInitializedTickCursor(
        tick_array_start_index=0,
        tick_offset=0,
        tick=tick_array.ticks[0],
        zero_for_one=True,
    )
    pre_step = compute_raydium_swap_loop_pre_step(
        fee_state(amount_specified_remaining=1000, liquidity=1000),
        sqrt_price_current_x64=Q64,
        target_price_x64=Q64,
        base_fee_rate=0,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
    )
    return pool, tick_array, cursor, pre_step


def apply_boundary(pool, tick_array, cursor, pre_step):
    return apply_raydium_swap_loop_boundary(
        pre_step,
        pool=pool,
        tick_array=tick_array,
        bitmap_extension=None,
        cursor=cursor,
        current_tick=0,
        current_sqrt_price_x64=Q64,
        tick_boundary_price_x64=Q64,
        updated_reward_infos=(
            RaydiumClmmRewardInfo(),
            RaydiumClmmRewardInfo(),
            RaydiumClmmRewardInfo(),
        ),
        recent_epoch=2,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
    )


def test_boundary_fifo_exhaustion_then_tick_crossing_preserves_source_order():
    pool, tick_array, cursor, pre_step = boundary_fixture()
    result = apply_boundary(pool, tick_array, cursor, pre_step)
    assert result.boundary_reached is True
    assert result.limit_order_match.amount_out == 10
    assert result.tick.orders_amount == 0
    assert result.tick.liquidity_gross == 100
    assert result.crossing is not None
    assert result.liquidity_next == 975
    assert result.tick_current == -1
    assert result.mutation.initialized_before is True
    assert result.mutation.initialized_after is True
    assert result.mutation.bitmap_changed is False
    assert result.tick_array.recent_epoch == 2
    assert len(result.tick_array.data_sha256) == 64


def test_boundary_order_only_tick_exhaustion_decrements_count_and_flips_bitmap():
    pool, tick_array, cursor, pre_step = boundary_fixture(
        liquidity_gross=0, liquidity_net=0, orders=10
    )
    result = apply_boundary(pool, tick_array, cursor, pre_step)
    assert result.boundary_reached is True
    assert result.crossing is None
    assert result.tick.initialized is False
    assert result.tick_array.initialized_tick_count == 0
    assert result.mutation.bitmap_changed is True
    assert result.mutation.initialized_after is False
    assert result.pool.tick_array_bitmap[8] & 1 == 0


def test_non_boundary_step_does_not_match_orders_or_mutate_accounts():
    pool, tick_array, cursor, _ = boundary_fixture()
    pre_step = compute_raydium_swap_loop_pre_step(
        fee_state(amount_specified_remaining=1, liquidity=1000),
        sqrt_price_current_x64=Q64,
        target_price_x64=get_sqrt_price_at_tick(-10),
        base_fee_rate=0,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
    )
    result = apply_raydium_swap_loop_boundary(
        pre_step,
        pool=pool,
        tick_array=tick_array,
        bitmap_extension=None,
        cursor=cursor,
        current_tick=0,
        current_sqrt_price_x64=Q64,
        tick_boundary_price_x64=Q64,
        updated_reward_infos=(
            RaydiumClmmRewardInfo(),
            RaydiumClmmRewardInfo(),
            RaydiumClmmRewardInfo(),
        ),
        recent_epoch=2,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
    )
    assert result.boundary_reached is False
    assert result.limit_order_match.amount_in == 0
    assert result.pool == pool
    assert result.tick_array == tick_array


def finalization_fixture():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint0, mint1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    creator, vault0, vault1 = (
        Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique()
    )
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint0), bytes(mint1)], program
    )
    observation_address, _ = derive_raydium_clmm_observation_address(
        str(pool_address)
    )
    pool = RaydiumClmmPoolState(
        pool_address=str(pool_address),
        bump=bump,
        amm_config=str(config),
        pool_creator=str(creator),
        token_mint_0=str(mint0),
        token_mint_1=str(mint1),
        token_vault_0=str(vault0),
        token_vault_1=str(vault1),
        observation_key=observation_address,
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=1,
        liquidity=1_000_000,
        sqrt_price_x64=Q64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data=bytes(RAYDIUM_POOL_STATE_LENGTH),
        account_data_sha256=hashlib.sha256(bytes(RAYDIUM_POOL_STATE_LENGTH)).hexdigest(),
    )
    pool_bytes = encode_raydium_clmm_pool_state(pool)
    pool = replace(
        pool,
        account_data=pool_bytes,
        account_data_sha256=hashlib.sha256(pool_bytes).hexdigest(),
    )
    observation = RaydiumClmmObservationState(
        address=observation_address,
        initialized=False,
        recent_epoch=1,
        observation_index=0,
        pool_id=str(pool_address),
        observations=tuple(
            RaydiumClmmObservation(0, 0, bytes(32))
            for _ in range(RAYDIUM_OBSERVATION_COUNT)
        ),
        padding=bytes(32),
        data_sha256="",
    )
    observation_bytes = encode_raydium_clmm_observation_state(observation)
    observation = replace(
        observation,
        data_sha256=hashlib.sha256(observation_bytes).hexdigest(),
    )
    state = fee_state(
        amount_specified_remaining=900,
        amount_calculated=50,
        liquidity=1_000_000,
        fee_growth_global_x64=123,
        lp_fee=10,
        protocol_fee=2,
        fund_fee=1,
    )
    return pool, observation, state


def test_finalization_updates_observation_with_old_tick_then_settles_and_persists_pool():
    pool, observation, state = finalization_fixture()
    result = finalize_raydium_swap_loop(
        pool,
        observation,
        state,
        amount_specified=1000,
        final_tick=-5,
        final_sqrt_price_x64=get_sqrt_price_at_tick(-5),
        final_liquidity=900_000,
        block_timestamp=1000,
        zero_for_one=True,
        is_base_input=True,
        dynamic_fee_state=None,
    )
    assert result.observation.initialized is True
    assert result.observation.observations[0].block_timestamp == 1000
    assert result.observation.observations[0].tick_cumulative == 0
    assert result.observation.data_sha256 == hashlib.sha256(
        encode_raydium_clmm_observation_state(result.observation)
    ).hexdigest()
    assert result.settlement.amount_0 > 0
    assert result.settlement.amount_1 > 0
    assert result.pool.tick_current == -5
    assert result.pool.sqrt_price_x64 == get_sqrt_price_at_tick(-5)
    assert result.pool.liquidity == 900_000
    assert result.pool.account_data_sha256 == hashlib.sha256(
        result.pool.account_data
    ).hexdigest()
    assert encode_raydium_clmm_pool_state(result.pool) == result.pool.account_data


def test_finalization_does_not_write_observation_when_tick_is_unchanged():
    pool, observation, state = finalization_fixture()
    result = finalize_raydium_swap_loop(
        pool,
        observation,
        state,
        amount_specified=1000,
        final_tick=pool.tick_current,
        final_sqrt_price_x64=Q64,
        final_liquidity=pool.liquidity,
        block_timestamp=1000,
        zero_for_one=True,
        is_base_input=True,
        dynamic_fee_state=None,
    )
    assert result.observation == observation
    assert result.pool.tick_current == pool.tick_current
    assert result.pool.account_data_sha256 == hashlib.sha256(
        result.pool.account_data
    ).hexdigest()
