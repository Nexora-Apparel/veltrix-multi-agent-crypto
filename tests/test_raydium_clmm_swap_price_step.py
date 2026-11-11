import pytest

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeSwapState,
    compute_total_fee_rate,
    update_volatility_accumulator,
)
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_swap_price_step import (
    RaydiumSwapPriceStepError,
    compute_raydium_clmm_swap_price_step,
)
from veltrix.raydium_clmm_swap_step_math import compute_swap_step
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def dynamic_info(**overrides):
    values = dict(
        filter_period=60,
        decay_period=3600,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=100_000,
        tick_spacing_index_reference=-2,
        volatility_reference=10_000,
        volatility_accumulator=0,
        last_update_timestamp=1000,
        padding=bytes(46),
    )
    values.update(overrides)
    return RaydiumClmmDynamicFeeInfo(**values)


def fee_state():
    return RaydiumFeeAccountingState(
        amount_specified_remaining=100_000,
        amount_calculated=0,
        liquidity=1_000_000,
    )


def test_static_fee_price_step_composes_swap_math_and_fee_accounting():
    state = fee_state()
    current = 1 << 64
    target = get_sqrt_price_at_tick(10)
    result = compute_raydium_clmm_swap_price_step(
        state,
        sqrt_price_current_x64=current,
        target_price_x64=target,
        base_fee_rate=3000,
        protocol_fee_rate=100_000,
        fund_fee_rate=50_000,
        zero_for_one=False,
        is_base_input=True,
        is_fee_on_input=True,
    )
    expected = compute_swap_step(
        current,
        target,
        state.liquidity,
        state.amount_specified_remaining,
        3000,
        is_base_input=True,
        zero_for_one=False,
        is_fee_on_input=True,
    )
    assert result.price_changed is True
    assert result.fee_rate == 3000
    assert result.swap_result == expected
    assert result.fee_state.amount_specified_remaining == (
        state.amount_specified_remaining - expected.amount_in - expected.fee_amount
    )
    assert result.fee_state.amount_calculated == expected.amount_out
    assert result.fee_state.lp_fee + result.fee_state.protocol_fee + result.fee_state.fund_fee == expected.fee_amount


def test_dynamic_fee_step_updates_volatility_before_fee_and_spacing_bound():
    state = fee_state()
    info = dynamic_info()
    dynamic = RaydiumDynamicFeeSwapState(
        info=info,
        tick_spacing=10,
        base_fee_rate=1000,
        tick_spacing_index=0,
    )
    updated_info = update_volatility_accumulator(info, tick_spacing_index=0)
    expected_fee = compute_total_fee_rate(
        base_fee_rate=1000,
        info=updated_info,
        tick_spacing=10,
    )
    result = compute_raydium_clmm_swap_price_step(
        state,
        sqrt_price_current_x64=1 << 64,
        target_price_x64=get_sqrt_price_at_tick(20),
        base_fee_rate=1000,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        zero_for_one=False,
        is_base_input=True,
        is_fee_on_input=True,
        dynamic_fee_state=dynamic,
    )
    assert result.dynamic_fee_state.info == updated_info
    assert result.fee_rate == expected_fee
    assert result.spacing_bound.sqrt_price_x64 == get_sqrt_price_at_tick(10)
    assert result.spacing_bound.bounded_tick == 10
    assert result.price_changed is True


def test_no_price_change_is_a_zero_amount_noop_and_preserves_fee_state():
    state = fee_state()
    result = compute_raydium_clmm_swap_price_step(
        state,
        sqrt_price_current_x64=1 << 64,
        target_price_x64=1 << 64,
        base_fee_rate=2500,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        zero_for_one=False,
        is_base_input=True,
        is_fee_on_input=True,
    )
    assert result.price_changed is False
    assert result.swap_result.amount_in == 0
    assert result.swap_result.amount_out == 0
    assert result.swap_result.fee_amount == 0
    assert result.fee_state is state


def test_dynamic_fee_max_accumulator_disables_spacing_bound_for_this_step():
    state = fee_state()
    info = dynamic_info(
        volatility_accumulator=100_000,
        max_volatility_accumulator=100_000,
    )
    dynamic = RaydiumDynamicFeeSwapState(info, 10, 1000, 0)
    target = get_sqrt_price_at_tick(20)
    result = compute_raydium_clmm_swap_price_step(
        state,
        sqrt_price_current_x64=1 << 64,
        target_price_x64=target,
        base_fee_rate=1000,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        zero_for_one=False,
        is_base_input=True,
        is_fee_on_input=True,
        dynamic_fee_state=dynamic,
    )
    assert result.spacing_bound.reached_target is True
    assert result.spacing_bound.sqrt_price_x64 == target


def test_price_step_rejects_invalid_direction_and_fee_configuration():
    with pytest.raises(RaydiumSwapPriceStepError, match="failed"):
        compute_raydium_clmm_swap_price_step(
            fee_state(),
            sqrt_price_current_x64=1 << 64,
            target_price_x64=get_sqrt_price_at_tick(-10),
            base_fee_rate=3000,
            protocol_fee_rate=0,
            fund_fee_rate=0,
            zero_for_one=False,
            is_base_input=True,
            is_fee_on_input=True,
        )
    with pytest.raises(RaydiumSwapPriceStepError, match="base fee"):
        compute_raydium_clmm_swap_price_step(
            fee_state(),
            sqrt_price_current_x64=1 << 64,
            target_price_x64=get_sqrt_price_at_tick(10),
            base_fee_rate=True,
            protocol_fee_rate=0,
            fund_fee_rate=0,
            zero_for_one=False,
            is_base_input=True,
            is_fee_on_input=True,
        )
