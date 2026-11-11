"""Source-vector and adversarial tests for Raydium FIFO limit-order matching."""
import pytest

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_limit_orders import (
    FEE_RATE_DENOMINATOR,
    Q64,
    U64_MAX,
    U128_MAX,
    RaydiumLimitOrderError,
    get_price_from_sqrt_price,
    limit_order_input_with_price,
    limit_order_output_with_price,
    limit_order_unfilled_amount,
    match_limit_order,
)


def tick(**overrides):
    values = dict(
        tick_index=0,
        liquidity_net=0,
        liquidity_gross=0,
        orders_amount=500,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=0,
        fee_growth_outside_1_x64=0,
        reward_growths_outside_x64=(0, 0, 0),
        order_phase=4,
        unfilled_ratio_x64=Q64,
        padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


def match(state, *, amount, zfo=True, base_input=True, fee_rate=0, fee_on_input=True):
    return match_limit_order(
        state,
        swap_amount=amount,
        zero_for_one=zfo,
        is_base_input=base_input,
        fee_rate=fee_rate,
        is_fee_on_input=fee_on_input,
        sqrt_price_x64=1 << 64,
    )


def test_price_from_sqrt_price_uses_source_floor_and_ceiling():
    # (Q64 + 1)^2 / Q64 has a non-zero remainder.
    sqrt_price = Q64 + 1
    assert get_price_from_sqrt_price(sqrt_price, round_up=False) == Q64 + 2
    assert get_price_from_sqrt_price(sqrt_price, round_up=True) == Q64 + 3


@pytest.mark.parametrize("zfo", [True, False])
def test_limit_order_input_output_helpers_at_unit_price(zfo):
    assert limit_order_output_with_price(123, Q64, zero_for_one=zfo) == 123
    assert limit_order_input_with_price(123, Q64, zero_for_one=zfo) == 123


def test_limit_order_unfilled_sum_is_checked_u64():
    assert limit_order_unfilled_amount(10, 20) == 30
    with pytest.raises(RaydiumLimitOrderError, match="overflows"):
        limit_order_unfilled_amount(U64_MAX, 1)


def test_exact_input_with_input_fee_caps_to_available_orders_and_reprices_fee():
    result = match(
        tick(),
        amount=1000,
        zfo=True,
        base_input=True,
        fee_rate=10_000,
        fee_on_input=True,
    )
    assert result.match.amount_in == 500
    assert result.match.amount_out == 500
    assert result.match.amm_fee_amount == 6
    assert result.tick.orders_amount == 0
    assert result.tick.part_filled_orders_remaining == 0
    assert result.tick.order_phase == 5
    assert result.tick.unfilled_ratio_x64 == 0
    assert result.tick.initialized is False


def test_exact_input_partial_fill_of_existing_part_filled_cohort_first():
    result = match(
        tick(
            orders_amount=500,
            part_filled_orders_remaining=50,
            order_phase=8,
            unfilled_ratio_x64=Q64,
        ),
        amount=40,
        fee_rate=0,
    )
    assert result.match.amount_in == 40
    assert result.match.amount_out == 40
    assert result.tick.part_filled_orders_remaining == 10
    assert result.tick.orders_amount == 500
    assert result.tick.order_phase == 8
    assert result.tick.unfilled_ratio_x64 == (Q64 * 10) // 50


def test_exact_input_consumes_part_filled_then_enters_next_fifo_cohort():
    result = match(
        tick(
            orders_amount=500,
            part_filled_orders_remaining=50,
            order_phase=8,
            unfilled_ratio_x64=Q64,
        ),
        amount=75,
        fee_rate=0,
    )
    assert result.match.amount_out == 75
    assert result.tick.part_filled_orders_remaining == 475
    assert result.tick.orders_amount == 0
    assert result.tick.order_phase == 9
    assert result.tick.unfilled_ratio_x64 == (Q64 * 475) // 500


def test_exact_input_fee_on_output_is_deducted_after_gross_order_consumption():
    result = match(
        tick(),
        amount=100,
        fee_rate=10_000,
        fee_on_input=False,
    )
    assert result.match.amount_in == 100
    assert result.match.amm_fee_amount == 1
    assert result.match.amount_out == 99
    assert result.tick.part_filled_orders_remaining == 400
    assert result.tick.unfilled_ratio_x64 == (Q64 * 400) // 500


def test_exact_output_with_fee_on_input_grosses_up_fee_and_preserves_net_output():
    result = match(
        tick(),
        amount=100,
        base_input=False,
        fee_rate=10_000,
        fee_on_input=True,
    )
    assert result.match.amount_in == 102
    assert result.match.amm_fee_amount == 2
    assert result.match.amount_out == 100
    assert result.tick.part_filled_orders_remaining == 400


def test_exact_output_with_fee_on_output_grosses_up_then_deducts_fee():
    result = match(
        tick(),
        amount=100,
        base_input=False,
        fee_rate=10_000,
        fee_on_input=False,
    )
    assert result.match.amount_in == 102
    assert result.match.amm_fee_amount == 2
    assert result.match.amount_out == 100
    assert result.tick.part_filled_orders_remaining == 398


def test_exact_output_cannot_consume_more_than_total_available_orders():
    result = match(
        tick(orders_amount=50),
        amount=1000,
        base_input=False,
        fee_rate=0,
    )
    assert result.match.amount_out == 50
    assert result.tick.part_filled_orders_remaining == 0
    assert result.tick.orders_amount == 0
    assert result.tick.initialized is False


def test_zero_amount_or_empty_book_is_a_noop():
    original = tick()
    assert match(original, amount=0).tick == original
    empty = tick(orders_amount=0, part_filled_orders_remaining=0, initialized=False)
    result = match(empty, amount=10)
    assert result.tick == empty
    assert result.match.amount_in == result.match.amount_out == result.match.amm_fee_amount == 0


def test_phase_saturates_at_u64_max():
    result = match(tick(order_phase=U64_MAX), amount=501)
    assert result.tick.order_phase == U64_MAX


def test_rejects_fee_rate_at_or_above_denominator():
    with pytest.raises(RaydiumLimitOrderError):
        match_limit_order(
            tick(), swap_amount=10, zero_for_one=True, is_base_input=True,
            fee_rate=FEE_RATE_DENOMINATOR, is_fee_on_input=True,
            sqrt_price_x64=Q64,
        )


def test_rejects_invalid_unfilled_ratio_and_zero_sqrt_price():
    with pytest.raises(RaydiumLimitOrderError, match="unity"):
        match(tick(unfilled_ratio_x64=Q64 + 1), amount=10)
    with pytest.raises(RaydiumLimitOrderError, match="positive"):
        match_limit_order(
            tick(), swap_amount=10, zero_for_one=True, is_base_input=True,
            fee_rate=0, is_fee_on_input=True, sqrt_price_x64=0,
        )


def test_rejects_amounts_that_overflow_u64():
    with pytest.raises(RaydiumLimitOrderError, match="overflows"):
        match(tick(orders_amount=U64_MAX, part_filled_orders_remaining=1), amount=1)


def test_price_helpers_use_directional_reciprocal_price():
    price = 2 * Q64
    assert limit_order_output_with_price(10, price, zero_for_one=True) == 20
    assert limit_order_output_with_price(10, price, zero_for_one=False) == 5
    assert limit_order_input_with_price(20, price, zero_for_one=True) == 40
    assert limit_order_input_with_price(5, price, zero_for_one=False) == 10


def test_rejects_nonempty_order_state_marked_uninitialized():
    with pytest.raises(RaydiumLimitOrderError, match="must be initialized"):
        match(
            tick(initialized=False),
            amount=1,
        )


def test_rejects_limit_order_output_over_u128():
    with pytest.raises(RaydiumLimitOrderError, match="exceeds u128"):
        limit_order_output_with_price(
            U64_MAX, U128_MAX, zero_for_one=True
        )
