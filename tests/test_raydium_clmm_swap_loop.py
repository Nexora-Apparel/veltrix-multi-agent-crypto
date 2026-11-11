"""Source-order tests for Raydium swap-loop pre-step composition."""
from dataclasses import replace

import pytest

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeSwapState,
    compute_total_fee_rate,
)
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_swap_loop import (
    RaydiumSwapLoopError,
    compute_raydium_swap_loop_pre_step,
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
        base_fee_rate=0,
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
