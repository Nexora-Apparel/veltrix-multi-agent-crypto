"""Source-vector and adversarial tests for Raydium FIFO limit-order matching."""
import pytest

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import Q64
from veltrix.raydium_clmm_limit_orders import (
    RaydiumLimitOrderError,
    get_limit_order_input_with_price,
    get_limit_order_output_with_price,
    get_token0_price_x64,
    match_raydium_limit_order,
)


def tick(**overrides):
    values = dict(
        tick_index=100, liquidity_net=0, liquidity_gross=1000,
        orders_amount=1000, part_filled_orders_remaining=0,
        initialized=True, fee_growth_outside_0_x64=0,
        fee_growth_outside_1_x64=0, reward_growths_outside_x64=(0, 0, 0),
        order_phase=0, unfilled_ratio_x64=Q64, padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


def test_price_from_sqrt_uses_source_directional_rounding():
    sqrt_price = Q64 + 1
    product = sqrt_price * sqrt_price
    assert get_token0_price_x64(sqrt_price, round_up=False) == product // Q64
    assert get_token0_price_x64(sqrt_price, round_up=True) == (product + Q64 - 1) // Q64


def test_limit_order_amount_helpers_preserve_floor_and_ceil_directions():
    price = Q64 + 123
    assert get_limit_order_output_with_price(1000, price, True) == 1000 * price // Q64
    assert get_limit_order_output_with_price(1000, price, False) == 1000 * Q64 // price
    assert get_limit_order_input_with_price(1000, price, True) == (1000 * price + Q64 - 1) // Q64
    assert get_limit_order_input_with_price(1000, price, False) == (1000 * Q64 + price - 1) // price


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
    updated, result = match_raydium_limit_order(
        tick(), swap_amount=100, zero_for_one=True,
        is_base_input=is_base_input, fee_rate=3000,
        is_fee_on_input=is_fee_on_input, sqrt_price_x64=Q64,
    )
    assert (result.amount_in, result.amount_out, result.amm_fee_amount) == (
        expected_in, expected_out, expected_fee
    )
    assert updated.orders_amount == 0
    gross_output = (
        99 if is_base_input and is_fee_on_input else
        100 if is_base_input or is_fee_on_input else 101
    )
    assert updated.part_filled_orders_remaining == 1000 - gross_output
    assert updated.order_phase == 1


def test_consumes_existing_part_filled_fifo_cohort_first():
    updated, result = match_raydium_limit_order(
        tick(orders_amount=500, part_filled_orders_remaining=100,
             unfilled_ratio_x64=Q64 // 2),
        swap_amount=50, zero_for_one=True, is_base_input=True,
        fee_rate=0, is_fee_on_input=True, sqrt_price_x64=Q64,
    )
    assert result.amount_out == 50
    assert updated.part_filled_orders_remaining == 50
    assert updated.orders_amount == 500
    assert updated.order_phase == 0
    assert updated.unfilled_ratio_x64 == (Q64 // 2) * 50 // 100


def test_advances_fifo_phase_only_after_existing_part_filled_cohort_is_exhausted():
    updated, result = match_raydium_limit_order(
        tick(orders_amount=500, part_filled_orders_remaining=100,
             unfilled_ratio_x64=Q64 // 2),
        swap_amount=150, zero_for_one=True, is_base_input=True,
        fee_rate=0, is_fee_on_input=True, sqrt_price_x64=Q64,
    )
    assert result.amount_out == 150
    assert updated.part_filled_orders_remaining == 450
    assert updated.orders_amount == 0
    assert updated.order_phase == 1
    assert updated.unfilled_ratio_x64 == Q64 * 450 // 500


def test_caps_match_to_total_unfilled_amount():
    updated, result = match_raydium_limit_order(
        tick(orders_amount=10), swap_amount=1000, zero_for_one=True,
        is_base_input=True, fee_rate=3000, is_fee_on_input=True,
        sqrt_price_x64=Q64,
    )
    assert result.amount_out == 10
    assert result.amount_in == 10
    assert result.amm_fee_amount == (10 * 3000 + 997_000 - 1) // 997_000
    assert updated.orders_amount == updated.part_filled_orders_remaining == 0


def test_zero_amount_or_empty_orders_are_noop():
    original = tick()
    unchanged, result = match_raydium_limit_order(
        original, swap_amount=0, zero_for_one=True, is_base_input=True,
        fee_rate=3000, is_fee_on_input=True, sqrt_price_x64=Q64,
    )
    assert unchanged == original
    assert result == type(result)()
    empty = tick(orders_amount=0)
    unchanged, result = match_raydium_limit_order(
        empty, swap_amount=100, zero_for_one=True, is_base_input=True,
        fee_rate=3000, is_fee_on_input=True, sqrt_price_x64=Q64,
    )
    assert unchanged == empty
    assert result == type(result)()


@pytest.mark.parametrize("changes", [
    {"orders_amount": -1},
    {"part_filled_orders_remaining": 1 << 64},
    {"order_phase": 1 << 64},
    {"unfilled_ratio_x64": Q64 + 1},
    {"orders_amount": (1 << 64) - 1, "part_filled_orders_remaining": 1},
])
def test_rejects_invalid_fifo_state(changes):
    with pytest.raises(RaydiumLimitOrderError):
        match_raydium_limit_order(
            tick(**changes), swap_amount=1, zero_for_one=True,
            is_base_input=True, fee_rate=0, is_fee_on_input=True,
            sqrt_price_x64=Q64,
        )


def test_rejects_fee_and_amount_domain_errors():
    with pytest.raises(RaydiumLimitOrderError, match="fee_rate"):
        match_raydium_limit_order(
            tick(), swap_amount=1, zero_for_one=True, is_base_input=True,
            fee_rate=1_000_000, is_fee_on_input=True, sqrt_price_x64=Q64,
        )
    with pytest.raises(RaydiumLimitOrderError, match="swap_amount"):
        match_raydium_limit_order(
            tick(), swap_amount=1 << 64, zero_for_one=True, is_base_input=True,
            fee_rate=0, is_fee_on_input=True, sqrt_price_x64=Q64,
        )


def test_rejects_unrepresentable_input_and_invalid_sqrt_price():
    with pytest.raises(RaydiumLimitOrderError, match="exceeds u64"):
        get_limit_order_input_with_price((1 << 64) - 1, (1 << 128) - 1, True)
    with pytest.raises(RaydiumLimitOrderError, match="positive"):
        get_token0_price_x64(0, round_up=False)


@pytest.mark.parametrize("round_up", [False, True])
def test_sqrt_price_conversion_rejects_u128_result_overflow(round_up):
    # The pinned Rust U256 implementation rejects a post-shift price > u128::MAX.
    with pytest.raises(RaydiumLimitOrderError, match="token0_price_x64"):
        get_token0_price_x64(1 << 96, round_up=round_up)

