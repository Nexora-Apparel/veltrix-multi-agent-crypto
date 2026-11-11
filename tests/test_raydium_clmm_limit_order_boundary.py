import pytest

from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_limit_order_boundary import (
    RaydiumLimitOrderBoundaryError,
    apply_raydium_limit_order_boundary,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import Q64


def tick(**overrides):
    values = dict(
        tick_index=100,
        liquidity_net=0,
        liquidity_gross=1000,
        orders_amount=1000,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=0,
        fee_growth_outside_1_x64=0,
        reward_growths_outside_x64=(0, 0, 0),
        order_phase=0,
        unfilled_ratio_x64=Q64,
        padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


def state(amount=10_000):
    return RaydiumFeeAccountingState(
        amount_specified_remaining=amount,
        amount_calculated=0,
        liquidity=1_000_000,
    )


def test_limit_order_boundary_uses_precomputed_fee_and_applies_fee_split():
    before = state()
    result = apply_raydium_limit_order_boundary(
        tick(),
        before,
        fee_rate=3000,
        sqrt_price_x64=Q64,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=100_000,
        fund_fee_rate=50_000,
    )
    assert result.unfilled_amount_before == 1000
    assert result.unfilled_amount_after == 901
    assert (result.match.amount_in, result.match.amount_out, result.match.amm_fee_amount) == (99, 99, 1)
    assert result.tick.orders_amount == 0
    assert result.tick.part_filled_orders_remaining == 901
    assert result.tick.order_phase == 1
    assert result.fee_state.amount_specified_remaining == 9900
    assert result.fee_state.amount_calculated == 99
    assert result.fee_state.lp_fee + result.fee_state.protocol_fee + result.fee_state.fund_fee == 1


def test_limit_order_boundary_preserves_part_filled_fifo_cohort_first():
    result = apply_raydium_limit_order_boundary(
        tick(orders_amount=500, part_filled_orders_remaining=100, unfilled_ratio_x64=Q64 // 2),
        state(50),
        fee_rate=0,
        sqrt_price_x64=Q64,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
    )
    assert result.unfilled_amount_before == 600
    assert result.unfilled_amount_after == 550
    assert result.tick.part_filled_orders_remaining == 50
    assert result.tick.orders_amount == 500
    assert result.tick.order_phase == 0
    assert result.match.amount_out == 50


def test_empty_order_boundary_is_an_exact_noop():
    original = tick(orders_amount=0)
    before = state()
    result = apply_raydium_limit_order_boundary(
        original,
        before,
        fee_rate=3000,
        sqrt_price_x64=Q64,
        zero_for_one=True,
        is_base_input=True,
        is_fee_on_input=True,
        protocol_fee_rate=0,
        fund_fee_rate=0,
    )
    assert result.tick == original
    assert result.fee_state is before
    assert result.unfilled_amount_before == result.unfilled_amount_after == 0


def test_limit_order_boundary_rejects_uninitialized_tick_and_unfilled_overflow():
    with pytest.raises(RaydiumLimitOrderBoundaryError, match="initialized"):
        apply_raydium_limit_order_boundary(
            tick(initialized=False),
            state(),
            fee_rate=0,
            sqrt_price_x64=Q64,
            zero_for_one=True,
            is_base_input=True,
            is_fee_on_input=True,
            protocol_fee_rate=0,
            fund_fee_rate=0,
        )
    with pytest.raises(RaydiumLimitOrderBoundaryError, match="overflows"):
        apply_raydium_limit_order_boundary(
            tick(orders_amount=(1 << 64) - 1, part_filled_orders_remaining=1),
            state(),
            fee_rate=0,
            sqrt_price_x64=Q64,
            zero_for_one=True,
            is_base_input=True,
            is_fee_on_input=True,
            protocol_fee_rate=0,
            fund_fee_rate=0,
        )
