"""Adversarial tests for Raydium tick crossing and FIFO limit-order transitions."""
import pytest

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import Q64
from veltrix.raydium_clmm_tick_transitions import (
    RaydiumTickTransitionError,
    cross_raydium_tick,
    get_limit_order_input_with_price,
    get_limit_order_output_with_price,
    get_price_from_sqrt_price,
    match_raydium_limit_order,
)


def tick_state(**overrides):
    values = dict(
        tick_index=100,
        liquidity_net=500,
        liquidity_gross=1000,
        orders_amount=1000,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=10,
        fee_growth_outside_1_x64=20,
        reward_growths_outside_x64=(30, 40, 50),
        order_phase=0,
        unfilled_ratio_x64=Q64,
        padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


def test_sqrt_price_to_token0_price_uses_exact_floor_and_ceiling():
    sqrt_price = Q64 + 1
    floor = get_price_from_sqrt_price(sqrt_price, round_up=False)
    ceil = get_price_from_sqrt_price(sqrt_price, round_up=True)
    assert floor == (sqrt_price * sqrt_price) >> 64
    assert ceil == ((sqrt_price * sqrt_price + Q64 - 1) >> 64)


def test_limit_order_input_rounds_up_and_output_rounds_down():
    price = Q64 + 123
    assert get_limit_order_output_with_price(1000, price, True) == 1000 * price // Q64
    assert get_limit_order_output_with_price(1000, price, False) == 1000 * Q64 // price
    assert get_limit_order_input_with_price(1000, price, True) == (1000 * price + Q64 - 1) // Q64
    assert get_limit_order_input_with_price(1000, price, False) == (1000 * Q64 + price - 1) // price


def test_cross_tick_wraps_fee_and_initialized_reward_growth_and_applies_directional_liquidity():
    tick = tick_state(orders_amount=0)
    left = cross_raydium_tick(
        tick,
        fee_growth_global_0_x64=5,
        fee_growth_global_1_x64=7,
        reward_growths_global_x64=(100, 200, 300),
        reward_initialized=(True, False, True),
        zero_for_one=True,
        liquidity=1000,
    )
    assert left.tick.fee_growth_outside_0_x64 == (5 - 10) % (1 << 128)
    assert left.tick.fee_growth_outside_1_x64 == (7 - 20) % (1 << 128)
    assert left.tick.reward_growths_outside_x64 == (
        (100 - 30) % (1 << 128), 40, (300 - 50) % (1 << 128)
    )
    assert left.liquidity_net == 500
    assert left.next_liquidity == 500
    assert left.next_tick_current == 99

    right = cross_raydium_tick(
        tick,
        fee_growth_global_0_x64=5,
        fee_growth_global_1_x64=7,
        reward_growths_global_x64=(100, 200, 300),
        reward_initialized=(False, False, False),
        zero_for_one=False,
        liquidity=1000,
    )
    assert right.next_liquidity == 1500
    assert right.next_tick_current == 100
    assert right.tick.reward_growths_outside_x64 == tick.reward_growths_outside_x64


def test_cross_tick_rejects_liquidity_underflow_and_overflow():
    with pytest.raises(RaydiumTickTransitionError, match="underflows"):
        cross_raydium_tick(
            tick_state(liquidity_net=2000, orders_amount=0),
            fee_growth_global_0_x64=0, fee_growth_global_1_x64=0,
            reward_growths_global_x64=(0, 0, 0),
            reward_initialized=(False, False, False),
            zero_for_one=True, liquidity=1000,
        )
    with pytest.raises(RaydiumTickTransitionError, match="overflows"):
        cross_raydium_tick(
            tick_state(liquidity_net=1000, orders_amount=0),
            fee_growth_global_0_x64=0, fee_growth_global_1_x64=0,
            reward_growths_global_x64=(0, 0, 0),
            reward_initialized=(False, False, False),
            zero_for_one=False, liquidity=(1 << 128) - 1,
        )


@pytest.mark.parametrize(
    "is_base_input,is_fee_on_input,expected_in,expected_out,expected_fee",
    [
        (True, True, 99, 99, 1),
        (True, False, 100, 99, 1),
        (False, True, 100, 100, 1),
        (False, False, 101, 100, 1),
    ],
)
def test_limit_order_matches_exact_input_output_and_fee_modes(
    is_base_input, is_fee_on_input, expected_in, expected_out, expected_fee
):
    tick = tick_state()
    updated, result = match_raydium_limit_order(
        tick, swap_amount=100, zero_for_one=True,
        is_base_input=is_base_input, fee_rate=3000,
        is_fee_on_input=is_fee_on_input, sqrt_price_x64=Q64,
    )
    assert result.amount_in == expected_in
    assert result.amount_out == expected_out
    assert result.amm_fee_amount == expected_fee
    assert updated.orders_amount == 0
    assert updated.part_filled_orders_remaining == 1000 - (
        100 if not is_base_input and not is_fee_on_input else
        101 if not is_base_input and is_fee_on_input else 100
    )
    assert updated.order_phase == 1


def test_limit_order_consumes_part_filled_fifo_cohort_before_new_orders():
    tick = tick_state(
        orders_amount=500, part_filled_orders_remaining=100,
        unfilled_ratio_x64=Q64 // 2,
    )
    updated, result = match_raydium_limit_order(
        tick, swap_amount=50, zero_for_one=True,
        is_base_input=True, fee_rate=0, is_fee_on_input=True,
        sqrt_price_x64=Q64,
    )
    assert result.amount_out == 50
    assert updated.part_filled_orders_remaining == 50
    assert updated.orders_amount == 500
    assert updated.order_phase == 0
    assert updated.unfilled_ratio_x64 == (Q64 // 2) * 50 // 100


def test_limit_order_moves_to_next_fifo_phase_after_part_filled_cohort_is_exhausted():
    tick = tick_state(
        orders_amount=500, part_filled_orders_remaining=100,
        unfilled_ratio_x64=Q64 // 2,
    )
    updated, result = match_raydium_limit_order(
        tick, swap_amount=150, zero_for_one=True,
        is_base_input=True, fee_rate=0, is_fee_on_input=True,
        sqrt_price_x64=Q64,
    )
    assert result.amount_out == 150
    assert updated.part_filled_orders_remaining == 450
    assert updated.orders_amount == 0
    assert updated.order_phase == 1
    assert updated.unfilled_ratio_x64 == Q64 * 450 // 500


def test_limit_order_caps_input_when_total_unfilled_is_exhausted():
    tick = tick_state(orders_amount=10, part_filled_orders_remaining=0)
    updated, result = match_raydium_limit_order(
        tick, swap_amount=1000, zero_for_one=True,
        is_base_input=True, fee_rate=3000, is_fee_on_input=True,
        sqrt_price_x64=Q64,
    )
    assert result.amount_out == 10
    assert result.amount_in == 10
    assert result.amm_fee_amount == (10 * 3000 + 997_000 - 1) // 997_000
    assert updated.orders_amount == 0
    assert updated.part_filled_orders_remaining == 0


def test_limit_order_without_orders_is_a_noop_and_invalid_fee_is_rejected():
    tick = tick_state(orders_amount=0, part_filled_orders_remaining=0)
    updated, result = match_raydium_limit_order(
        tick, swap_amount=100, zero_for_one=True,
        is_base_input=True, fee_rate=3000, is_fee_on_input=True,
        sqrt_price_x64=Q64,
    )
    assert updated == tick
    assert result.amount_in == result.amount_out == result.amm_fee_amount == 0
    with pytest.raises(RaydiumTickTransitionError, match="fee_rate"):
        match_raydium_limit_order(
            tick_state(), swap_amount=100, zero_for_one=True,
            is_base_input=True, fee_rate=1_000_000, is_fee_on_input=True,
            sqrt_price_x64=Q64,
        )
