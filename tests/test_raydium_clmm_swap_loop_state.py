"""Source-vector tests for Raydium swap-loop state and step preparation."""
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState, RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_swap_loop_state import (
    RaydiumSwapLoopStateError,
    initialize_raydium_swap_loop_state,
    set_raydium_swap_loop_target,
    prepare_raydium_swap_step,
    apply_prepared_raydium_swap_step,
    advance_raydium_dynamic_fee_after_tick,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def pool(**overrides):
    values = dict(
        pool_address=str(Pubkey.new_unique()), bump=1,
        amm_config=str(Pubkey.new_unique()), pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()), token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()), token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()), mint_decimals_0=6,
        mint_decimals_1=9, tick_spacing=10, liquidity=10**9,
        sqrt_price_x64=get_sqrt_price_at_tick(0), tick_current=0, status=0,
        fee_on=0, seed_index=0, account_data_sha256="a" * 64,
    )
    values.update(overrides)
    return RaydiumClmmPoolState(**values)


def test_initializes_exact_input_fee_side_and_selected_global_growth():
    p = pool(fee_growth_global_0_x64=11, fee_growth_global_1_x64=22)
    state = initialize_raydium_swap_loop_state(
        p, amount_specified=1000, trade_fee_rate=3000,
        zero_for_one=True, is_base_input=True, block_timestamp=100,
    )
    assert state.fee_state.fee_growth_global_x64 == 11
    assert state.is_fee_on_input is True
    assert state.fee_state.amount_specified_remaining == 1000


def test_fee_on_token_modes_select_input_and_output_fee_sides():
    token0_only = pool(fee_on=1)
    token1_only = pool(fee_on=2)
    a = initialize_raydium_swap_loop_state(
        token0_only, amount_specified=100, trade_fee_rate=3000,
        zero_for_one=False, is_base_input=True, block_timestamp=100,
    )
    b = initialize_raydium_swap_loop_state(
        token1_only, amount_specified=100, trade_fee_rate=3000,
        zero_for_one=True, is_base_input=True, block_timestamp=100,
    )
    assert a.is_fee_on_input is False
    assert b.is_fee_on_input is False


def test_target_price_clamps_tick_and_respects_direction_and_limit():
    state = initialize_raydium_swap_loop_state(
        pool(), amount_specified=1000, trade_fee_rate=3000,
        zero_for_one=False, is_base_input=True, block_timestamp=100,
    )
    result = set_raydium_swap_loop_target(
        state, next_tick=100, sqrt_price_limit_x64=get_sqrt_price_at_tick(50),
    )
    assert result.tick_next == 100
    assert result.sqrt_price_next_x64 == get_sqrt_price_at_tick(100)
    with pytest.raises(RaydiumSwapLoopStateError, match="strictly above"):
        set_raydium_swap_loop_target(
            state, next_tick=0, sqrt_price_limit_x64=get_sqrt_price_at_tick(50),
        )


def test_static_fee_step_uses_trade_fee_rate_and_applies_amounts():
    state = initialize_raydium_swap_loop_state(
        pool(), amount_specified=1000, trade_fee_rate=3000,
        zero_for_one=False, is_base_input=True, block_timestamp=100,
    )
    targeted = set_raydium_swap_loop_target(
        state, next_tick=100, sqrt_price_limit_x64=get_sqrt_price_at_tick(50),
    )
    prepared = prepare_raydium_swap_step(
        targeted, target_price_x64=get_sqrt_price_at_tick(50),
    )
    assert prepared.total_fee_rate == 3000
    applied = apply_prepared_raydium_swap_step(
        prepared, protocol_fee_rate=1000, fund_fee_rate=1000,
    )
    assert applied.fee_state.amount_specified_remaining < 1000
    assert applied.sqrt_price_x64 > state.sqrt_price_x64


def test_dynamic_fee_initialization_and_step_preparation():
    info = RaydiumClmmDynamicFeeInfo(
        filter_period=60, decay_period=3600, reduction_factor=5000,
        dynamic_fee_control=1000, max_volatility_accumulator=100000,
        tick_spacing_index_reference=0, volatility_reference=0,
        volatility_accumulator=100, last_update_timestamp=0,
    )
    state = initialize_raydium_swap_loop_state(
        pool(dynamic_fee_info=info), amount_specified=1000,
        trade_fee_rate=3000, zero_for_one=False, is_base_input=True,
        block_timestamp=100,
    )
    targeted = set_raydium_swap_loop_target(
        state, next_tick=100, sqrt_price_limit_x64=get_sqrt_price_at_tick(50),
    )
    prepared = prepare_raydium_swap_step(
        targeted, target_price_x64=get_sqrt_price_at_tick(50),
    )
    assert prepared.total_fee_rate >= 3000
    assert prepared.state.dynamic_fee_state.info.last_update_timestamp == 100


def test_dynamic_fee_index_uses_post_transition_tick_and_target_price():
    info = RaydiumClmmDynamicFeeInfo(
        filter_period=60, decay_period=3600, reduction_factor=5000,
        dynamic_fee_control=1000, max_volatility_accumulator=100000,
        tick_spacing_index_reference=0, volatility_reference=0,
        volatility_accumulator=100, last_update_timestamp=0,
    )
    state = initialize_raydium_swap_loop_state(
        pool(dynamic_fee_info=info), amount_specified=1000,
        trade_fee_rate=3000, zero_for_one=False, is_base_input=True,
        block_timestamp=100,
    )
    targeted = set_raydium_swap_loop_target(
        state, next_tick=100, sqrt_price_limit_x64=get_sqrt_price_at_tick(50),
    )
    prepared = prepare_raydium_swap_step(targeted, target_price_x64=get_sqrt_price_at_tick(50))
    applied = apply_prepared_raydium_swap_step(prepared, protocol_fee_rate=0, fund_fee_rate=0)
    advanced = advance_raydium_dynamic_fee_after_tick(
        applied, tick_after_transition=1, is_skipped_tick_spacing=False,
    )
    assert advanced.tick == 1
    assert advanced.dynamic_fee_state.tick_spacing_index == state.dynamic_fee_state.tick_spacing_index + 1
