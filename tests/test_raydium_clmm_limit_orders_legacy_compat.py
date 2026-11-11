"""Differential FIFO matching tests: historical PR #76 vs canonical PR #79."""
from dataclasses import dataclass, replace

import pytest

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import Q64
from veltrix.raydium_clmm_limit_orders import (
    match_raydium_limit_order as canonical_match,
)


U64_MAX = (1 << 64) - 1
FEE_DENOMINATOR = 1_000_000


@dataclass(frozen=True, slots=True)
class LegacyMatchResult:
    amount_in: int = 0
    amount_out: int = 0
    amm_fee_amount: int = 0


def ceil_div(numerator, denominator):
    return (numerator + denominator - 1) // denominator


def legacy_price_from_sqrt(sqrt_price_x64, *, round_up):
    product = sqrt_price_x64 * sqrt_price_x64
    if round_up:
        product += Q64 - 1
    return product >> 64


def legacy_output(amount_in, token0_price_x64, zero_for_one):
    return (
        amount_in * token0_price_x64 // Q64
        if zero_for_one
        else amount_in * Q64 // token0_price_x64
    )


def legacy_input(amount_out, token0_price_x64, zero_for_one):
    amount_in = (
        ceil_div(amount_out * token0_price_x64, Q64)
        if zero_for_one
        else ceil_div(amount_out * Q64, token0_price_x64)
    )
    if amount_in > U64_MAX:
        raise ValueError("legacy input exceeds u64")
    return amount_in


def legacy_match(
    tick,
    *,
    swap_amount,
    zero_for_one,
    is_base_input,
    fee_rate,
    is_fee_on_input,
    sqrt_price_x64,
):
    """Frozen historical PR #76 matching transition for valid input states."""
    if swap_amount == 0:
        return tick, LegacyMatchResult()
    total_unfilled = tick.orders_amount + tick.part_filled_orders_remaining
    if total_unfilled == 0:
        return tick, LegacyMatchResult()

    price = legacy_price_from_sqrt(
        sqrt_price_x64, round_up=not zero_for_one
    )
    amount_in = amount_out = fee_amount = 0

    if is_base_input:
        if is_fee_on_input:
            fee_amount = ceil_div(
                swap_amount * fee_rate, FEE_DENOMINATOR
            )
            amount_in = swap_amount - fee_amount
        else:
            amount_in = swap_amount
        matched_output = legacy_output(
            amount_in, price, zero_for_one
        )
        if matched_output > total_unfilled:
            amount_out = total_unfilled
            amount_in = legacy_input(
                amount_out, price, not zero_for_one
            )
            if is_fee_on_input:
                fee_amount = ceil_div(
                    amount_in * fee_rate,
                    FEE_DENOMINATOR - fee_rate,
                )
        else:
            amount_out = matched_output
    else:
        net_output = min(swap_amount, total_unfilled)
        if is_fee_on_input:
            amount_out = net_output
        else:
            amount_out = min(
                ceil_div(
                    net_output * FEE_DENOMINATOR,
                    FEE_DENOMINATOR - fee_rate,
                ),
                total_unfilled,
            )
        amount_in = legacy_input(
            amount_out, price, not zero_for_one
        )
        if is_fee_on_input:
            fee_amount = ceil_div(
                amount_in * fee_rate,
                FEE_DENOMINATOR - fee_rate,
            )

    part_remaining = tick.part_filled_orders_remaining
    orders_amount = tick.orders_amount
    order_phase = tick.order_phase
    unfilled_ratio = tick.unfilled_ratio_x64
    consume_part = min(part_remaining, amount_out)
    if consume_part > 0:
        unfilled_ratio = (
            unfilled_ratio * (part_remaining - consume_part)
            // part_remaining
        )
    part_remaining -= consume_part
    amount_to_consume = amount_out - consume_part
    if amount_to_consume > 0:
        if part_remaining != 0 or orders_amount < amount_to_consume:
            raise ValueError("legacy FIFO cohort amounts are inconsistent")
        order_phase = min(U64_MAX, order_phase + 1)
        unfilled_ratio = (
            Q64 * (orders_amount - amount_to_consume) // orders_amount
        )
        part_remaining = orders_amount - amount_to_consume
        orders_amount = 0

    if not is_fee_on_input:
        fee_amount = ceil_div(
            amount_out * fee_rate, FEE_DENOMINATOR
        )
        if fee_amount > amount_out:
            raise ValueError("legacy output fee exceeds gross output")
        amount_out -= fee_amount

    updated = replace(
        tick,
        order_phase=order_phase,
        orders_amount=orders_amount,
        part_filled_orders_remaining=part_remaining,
        unfilled_ratio_x64=unfilled_ratio,
        initialized=(
            tick.liquidity_gross > 0
            or orders_amount > 0
            or part_remaining > 0
        ),
    )
    return updated, LegacyMatchResult(
        amount_in, amount_out, fee_amount
    )


def make_tick(**overrides):
    values = dict(
        tick_index=120,
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


def assert_match_equal(actual_tick, actual, expected_tick, expected):
    assert actual_tick == expected_tick
    assert (actual.amount_in, actual.amount_out, actual.amm_fee_amount) == (
        expected.amount_in, expected.amount_out, expected.amm_fee_amount
    )


@pytest.mark.parametrize("zero_for_one", [True, False])
@pytest.mark.parametrize("is_base_input", [True, False])
@pytest.mark.parametrize("is_fee_on_input", [True, False])
def test_legacy_and_canonical_matcher_agree_across_all_direction_and_fee_modes(
    zero_for_one, is_base_input, is_fee_on_input
):
    tick = make_tick()
    args = dict(
        swap_amount=100,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        fee_rate=3000,
        is_fee_on_input=is_fee_on_input,
        sqrt_price_x64=Q64 + 123,
    )
    expected_tick, expected = legacy_match(tick, **args)
    actual_tick, actual = canonical_match(tick, **args)
    assert_match_equal(actual_tick, actual, expected_tick, expected)


@pytest.mark.parametrize("zero_for_one", [True, False])
@pytest.mark.parametrize("is_base_input", [True, False])
@pytest.mark.parametrize("is_fee_on_input", [True, False])
def test_legacy_and_canonical_matcher_preserve_part_filled_fifo_priority(
    zero_for_one, is_base_input, is_fee_on_input
):
    tick = make_tick(
        orders_amount=500,
        part_filled_orders_remaining=100,
        unfilled_ratio_x64=Q64 // 2,
    )
    args = dict(
        swap_amount=150,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        fee_rate=3000,
        is_fee_on_input=is_fee_on_input,
        sqrt_price_x64=Q64,
    )
    expected_tick, expected = legacy_match(tick, **args)
    actual_tick, actual = canonical_match(tick, **args)
    assert_match_equal(actual_tick, actual, expected_tick, expected)


@pytest.mark.parametrize("zero_for_one", [True, False])
@pytest.mark.parametrize("is_base_input", [True, False])
@pytest.mark.parametrize("is_fee_on_input", [True, False])
def test_legacy_and_canonical_matcher_agree_when_orders_are_exhausted(
    zero_for_one, is_base_input, is_fee_on_input
):
    tick = make_tick(orders_amount=10)
    args = dict(
        swap_amount=1000,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        fee_rate=3000,
        is_fee_on_input=is_fee_on_input,
        sqrt_price_x64=Q64,
    )
    expected_tick, expected = legacy_match(tick, **args)
    actual_tick, actual = canonical_match(tick, **args)
    assert_match_equal(actual_tick, actual, expected_tick, expected)
