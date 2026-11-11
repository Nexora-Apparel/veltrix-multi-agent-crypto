"""Adversarial arithmetic tests for the pinned Raydium static swap-step kernel."""
from fractions import Fraction

import pytest

from veltrix.raydium_clmm_tick_math import Q64, get_sqrt_price_at_tick
from veltrix.raydium_clmm_swap_step_math import (
    FEE_RATE_DENOMINATOR,
    U64_MAX,
    RaydiumSwapMathError,
    compute_swap_step,
    get_delta_amount_0_unsigned,
    get_delta_amount_1_unsigned,
    get_delta_amounts_for_swap,
    get_next_sqrt_price_from_amount_0_rounding_up,
    get_next_sqrt_price_from_amount_1_rounding_down,
)


def ceil_fraction(value):
    return (value.numerator + value.denominator - 1) // value.denominator


@pytest.mark.parametrize("round_up", [False, True])
def test_amount_0_delta_matches_exact_rational_rounding(round_up):
    a = get_sqrt_price_at_tick(-1234)
    b = get_sqrt_price_at_tick(2345)
    liquidity = 10**12
    exact = Fraction(liquidity * Q64 * (b - a), a * b)
    expected = ceil_fraction(exact) if round_up else exact.numerator // exact.denominator
    assert get_delta_amount_0_unsigned(a, b, liquidity, round_up=round_up) == expected


@pytest.mark.parametrize("round_up", [False, True])
def test_amount_1_delta_matches_exact_rational_rounding(round_up):
    a = get_sqrt_price_at_tick(-1234)
    b = get_sqrt_price_at_tick(2345)
    liquidity = 10**12
    exact = Fraction(liquidity * (b - a), Q64)
    expected = ceil_fraction(exact) if round_up else exact.numerator // exact.denominator
    assert get_delta_amount_1_unsigned(a, b, liquidity, round_up=round_up) == expected


@pytest.mark.parametrize("zero_for_one", [False, True])
def test_combined_swap_deltas_match_directional_input_ceil_output_floor(zero_for_one):
    a = get_sqrt_price_at_tick(-50)
    b = get_sqrt_price_at_tick(75)
    liquidity = 10**15
    amount_in, amount_out = get_delta_amounts_for_swap(
        a, b, liquidity, zero_for_one=zero_for_one
    )
    if zero_for_one:
        assert amount_in == get_delta_amount_0_unsigned(a, b, liquidity, round_up=True)
        assert amount_out == get_delta_amount_1_unsigned(a, b, liquidity, round_up=False)
    else:
        assert amount_in == get_delta_amount_1_unsigned(a, b, liquidity, round_up=True)
        assert amount_out == get_delta_amount_0_unsigned(a, b, liquidity, round_up=False)


def test_next_sqrt_price_amount_0_rounds_up_exactly():
    price = Q64
    liquidity = 10**12
    amount = 123_456
    numerator = liquidity * Q64 * price
    add_denominator = liquidity * Q64 + amount * price
    remove_denominator = liquidity * Q64 - amount * price
    assert get_next_sqrt_price_from_amount_0_rounding_up(
        price, liquidity, amount, add=True
    ) == (numerator + add_denominator - 1) // add_denominator
    assert get_next_sqrt_price_from_amount_0_rounding_up(
        price, liquidity, amount, add=False
    ) == (numerator + remove_denominator - 1) // remove_denominator


def test_next_sqrt_price_amount_1_rounds_down_on_add_and_up_on_remove():
    price = Q64
    liquidity = 10**12
    amount = 123_457
    numerator = amount * Q64
    assert get_next_sqrt_price_from_amount_1_rounding_down(
        price, liquidity, amount, add=True
    ) == price + numerator // liquidity
    assert get_next_sqrt_price_from_amount_1_rounding_down(
        price, liquidity, amount, add=False
    ) == price - (numerator + liquidity - 1) // liquidity


@pytest.mark.parametrize(
    "is_base_input,zero_for_one,is_fee_on_input",
    [
        (True, True, True), (True, True, False),
        (True, False, True), (True, False, False),
        (False, True, True), (False, True, False),
        (False, False, True), (False, False, False),
    ],
)
def test_swap_step_supports_all_four_exactness_and_fee_modes(is_base_input, zero_for_one, is_fee_on_input):
    current = Q64
    target = get_sqrt_price_at_tick(-200 if zero_for_one else 200)
    result = compute_swap_step(
        current, target, 10**12, 100_000_000, 3_000,
        is_base_input=is_base_input, zero_for_one=zero_for_one,
        is_fee_on_input=is_fee_on_input,
    )
    assert result.amount_in > 0
    assert result.amount_out > 0
    assert result.fee_amount >= 0
    assert result.sqrt_price_next_x64 <= current if zero_for_one else result.sqrt_price_next_x64 >= current
    assert result.sqrt_price_next_x64 >= target if zero_for_one else result.sqrt_price_next_x64 <= target
    assert result.amount_in <= U64_MAX
    assert result.amount_out <= U64_MAX
    assert result.fee_amount <= U64_MAX
    if is_base_input and is_fee_on_input and not result.reached_target:
        assert result.amount_in + result.fee_amount == 100_000_000
    if is_base_input and not is_fee_on_input and not result.reached_target:
        assert result.amount_in == 100_000_000
    if not is_base_input and is_fee_on_input:
        assert result.amount_out <= 100_000_000


@pytest.mark.parametrize("zero_for_one", [False, True])
def test_swap_step_reaches_nearby_target_when_amount_is_sufficient(zero_for_one):
    target = get_sqrt_price_at_tick(-10 if zero_for_one else 10)
    result = compute_swap_step(
        Q64, target, 10**9, U64_MAX, 0,
        is_base_input=True, zero_for_one=zero_for_one, is_fee_on_input=True,
    )
    assert result.reached_target is True
    assert result.sqrt_price_next_x64 == target
    assert result.fee_amount == 0


def test_swap_step_falls_back_when_target_amount_exceeds_u64():
    target = get_sqrt_price_at_tick(-400_000)
    result = compute_swap_step(
        Q64, target, 1 << 100, U64_MAX, 0,
        is_base_input=True, zero_for_one=True, is_fee_on_input=True,
    )
    assert result.reached_target is False
    assert Q64 >= result.sqrt_price_next_x64 > target
    assert result.amount_in <= U64_MAX
    assert result.amount_out <= U64_MAX


def test_rejects_invalid_fee_and_direction_inputs():
    with pytest.raises(RaydiumSwapMathError, match="fee_rate"):
        compute_swap_step(
            Q64, get_sqrt_price_at_tick(-1), 1000, 100, FEE_RATE_DENOMINATOR,
            is_base_input=True, zero_for_one=True, is_fee_on_input=True,
        )
    with pytest.raises(RaydiumSwapMathError, match="target price is above"):
        compute_swap_step(
            Q64, get_sqrt_price_at_tick(1), 1000, 100, 0,
            is_base_input=True, zero_for_one=True, is_fee_on_input=True,
        )
