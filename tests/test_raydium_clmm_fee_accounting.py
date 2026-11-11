"""Source-vector and adversarial tests for Raydium swap fee accounting."""
import pytest

from veltrix.raydium_clmm_tick_math import Q64
from veltrix.raydium_clmm_fee_accounting import (
    U64_MAX,
    U128_MAX,
    RaydiumFeeAccountingError,
    RaydiumFeeAccountingState,
    apply_raydium_swap_amounts,
    settle_raydium_swap_amounts,
)


def state(**overrides):
    values = dict(
        amount_specified_remaining=1000,
        amount_calculated=0,
        liquidity=10_000,
        fee_growth_global_x64=0,
        lp_fee=0,
        protocol_fee=0,
        fund_fee=0,
    )
    values.update(overrides)
    return RaydiumFeeAccountingState(**values)


def apply(s, *, base=True, fee_on_input=True, amount_in=100, amount_out=90,
          fee=10_000, protocol=100_000, fund=50_000):
    return apply_raydium_swap_amounts(
        s, amount_in=amount_in, amount_out=amount_out, fee_amount=fee,
        is_base_input=base, is_fee_on_input=fee_on_input,
        protocol_fee_rate=protocol, fund_fee_rate=fund,
    )


def test_splits_fee_into_protocol_fund_and_lp_with_floor_rounding():
    updated = apply(state(amount_specified_remaining=20_000), fee=10_000, protocol=100_000, fund=50_000)
    assert updated.protocol_fee == 1000
    assert updated.fund_fee == 500
    assert updated.lp_fee == 8500
    assert updated.fee_growth_global_x64 == 8500 * Q64 // 10_000
    assert updated.amount_specified_remaining == 9_900
    assert updated.amount_calculated == 90


def test_zero_liquidity_routes_remainder_to_protocol_fee_without_lp_growth():
    updated = apply(state(amount_specified_remaining=20_000, liquidity=0), fee=10_000)
    assert updated.protocol_fee == 9500
    assert updated.fund_fee == 500
    assert updated.lp_fee == 0
    assert updated.fee_growth_global_x64 == 0


@pytest.mark.parametrize(
    "base,fee_on_input,zero_for_one,expected_amounts",
    [
        (True, True, True, (110, 90)),
        (True, True, False, (90, 110)),
        (True, False, True, (100, 90)),
        (True, False, False, (90, 100)),
        (False, True, True, (110, 90)),
        (False, True, False, (90, 110)),
        (False, False, True, (100, 90)),
        (False, False, False, (90, 100)),
    ],
)
def test_exact_input_output_and_directional_settlement(base, fee_on_input, zero_for_one, expected_amounts):
    updated = apply(
        state(), base=base, fee_on_input=fee_on_input,
        amount_in=100, amount_out=90, fee=10,
        protocol=0, fund=0,
    )
    settled = settle_raydium_swap_amounts(
        updated, amount_specified=1000, zero_for_one=zero_for_one,
        is_base_input=base, fee_on_token0=zero_for_one,
    )
    assert (settled.amount_0, settled.amount_1) == expected_amounts
    assert (settled.trade_fee_0, settled.trade_fee_1) == (
        (10, 0) if zero_for_one else (0, 10)
    )


def test_fee_on_input_adds_fee_to_input_consumption_and_checked_subtracts_remaining():
    updated = apply(
        state(), base=True, fee_on_input=True, amount_in=100,
        amount_out=90, fee=10, protocol=0, fund=0,
    )
    assert updated.amount_specified_remaining == 890
    assert updated.amount_calculated == 90


def test_fee_on_output_does_not_add_fee_to_input_consumption():
    updated = apply(
        state(), base=True, fee_on_input=False, amount_in=100,
        amount_out=90, fee=10, protocol=0, fund=0,
    )
    assert updated.amount_specified_remaining == 900
    assert updated.amount_calculated == 90


def test_exact_output_adds_input_consumed_and_subtracts_output_remaining():
    updated = apply(
        state(), base=False, fee_on_input=True, amount_in=100,
        amount_out=90, fee=10, protocol=0, fund=0,
    )
    assert updated.amount_specified_remaining == 910
    assert updated.amount_calculated == 110


def test_fee_growth_global_wraps_u128_and_lp_fee_accumulates_checked_u64():
    updated = apply(
        state(liquidity=1, fee_growth_global_x64=U128_MAX),
        amount_in=0, amount_out=0, fee=1,
        protocol=0, fund=0,
    )
    assert updated.fee_growth_global_x64 == (U128_MAX + Q64) % (1 << 128)
    assert updated.lp_fee == 1
    with pytest.raises(RaydiumFeeAccountingError, match="lp_fee"):
        apply(
            state(lp_fee=U64_MAX), amount_in=0, amount_out=0,
            fee=1, protocol=0, fund=0,
        )


def test_rejects_overconsumption_and_fee_rate_sum_above_denominator():
    with pytest.raises(RaydiumFeeAccountingError, match="input consumption"):
        apply(state(), amount_in=1000, amount_out=0, fee=1,
              protocol=0, fund=0)
    with pytest.raises(RaydiumFeeAccountingError, match="exceed fee denominator"):
        apply(state(), fee=1, protocol=600_000, fund=500_000)


def test_settlement_rejects_invalid_remaining_and_fee_total_overflow():
    with pytest.raises(RaydiumFeeAccountingError, match="remaining specified"):
        settle_raydium_swap_amounts(
            state(amount_specified_remaining=1001), amount_specified=1000,
            zero_for_one=True, is_base_input=True, fee_on_token0=True,
        )
    with pytest.raises(RaydiumFeeAccountingError, match="total_trade_fee"):
        settle_raydium_swap_amounts(
            state(lp_fee=U64_MAX, protocol_fee=1), amount_specified=1000,
            zero_for_one=True, is_base_input=True, fee_on_token0=True,
        )


def test_rejects_boolean_integer_fields_and_non_boolean_flags():
    with pytest.raises(RaydiumFeeAccountingError, match="amount_in"):
        apply(state(), amount_in=True)
    with pytest.raises(RaydiumFeeAccountingError, match="is_base_input"):
        apply_raydium_swap_amounts(
            state(), amount_in=1, amount_out=1, fee_amount=0,
            is_base_input=1, is_fee_on_input=True,
            protocol_fee_rate=0, fund_fee_rate=0,
        )
