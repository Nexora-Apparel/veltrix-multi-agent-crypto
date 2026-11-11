"""Pinned Raydium CLMM per-step fee accounting and final amount settlement.

Source: instructions/swap.rs at 6d7a0f82d2f293c553c69aaf943377367c6c4cc6.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_tick_math import Q64

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
FEE_RATE_DENOMINATOR = 1_000_000


class RaydiumFeeAccountingError(ValueError):
    """Raised when a Raydium fee/amount transition exceeds source integer bounds."""


@dataclass(frozen=True, slots=True)
class RaydiumFeeAccountingState:
    amount_specified_remaining: int
    amount_calculated: int
    liquidity: int
    fee_growth_global_x64: int = 0
    lp_fee: int = 0
    protocol_fee: int = 0
    fund_fee: int = 0


@dataclass(frozen=True, slots=True)
class RaydiumSwapSettlement:
    amount_0: int
    amount_1: int
    trade_fee_0: int
    trade_fee_1: int


def _integer(value: int, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RaydiumFeeAccountingError(f"{field} must fit unsigned integer range")


def _add_u64(left: int, right: int, field: str) -> int:
    result = left + right
    if result > U64_MAX:
        raise RaydiumFeeAccountingError(f"{field} overflows u64")
    return result


def apply_raydium_swap_amounts(
    state: RaydiumFeeAccountingState,
    *,
    amount_in: int,
    amount_out: int,
    fee_amount: int,
    is_base_input: bool,
    is_fee_on_input: bool,
    protocol_fee_rate: int,
    fund_fee_rate: int,
) -> RaydiumFeeAccountingState:
    """Port SwapState::apply_swap_amounts and spilt_fees without mutation."""
    if not isinstance(state, RaydiumFeeAccountingState):
        raise RaydiumFeeAccountingError("Raydium fee-accounting state is required")
    for value, field in (
        (amount_in, "amount_in"), (amount_out, "amount_out"),
        (fee_amount, "fee_amount"), (state.amount_specified_remaining, "amount_specified_remaining"),
        (state.amount_calculated, "amount_calculated"), (state.liquidity, "liquidity"),
        (state.lp_fee, "lp_fee"), (state.protocol_fee, "protocol_fee"),
        (state.fund_fee, "fund_fee"),
    ):
        _integer(value, U128_MAX if field in ("liquidity",) else U64_MAX, field)
    _integer(state.fee_growth_global_x64, U128_MAX, "fee_growth_global_x64")
    for value, field in ((is_base_input, "is_base_input"), (is_fee_on_input, "is_fee_on_input")):
        if not isinstance(value, bool):
            raise RaydiumFeeAccountingError(f"{field} must be boolean")
    _integer(protocol_fee_rate, FEE_RATE_DENOMINATOR, "protocol_fee_rate")
    _integer(fund_fee_rate, FEE_RATE_DENOMINATOR, "fund_fee_rate")
    if protocol_fee_rate + fund_fee_rate > FEE_RATE_DENOMINATOR:
        raise RaydiumFeeAccountingError("protocol and fund fee rates exceed fee denominator")

    amount_in_consumed = (
        _add_u64(amount_in, fee_amount, "amount_in_consumed")
        if is_fee_on_input else amount_in
    )
    if is_base_input:
        if amount_in_consumed > state.amount_specified_remaining:
            raise RaydiumFeeAccountingError("input consumption exceeds remaining specified amount")
        remaining = state.amount_specified_remaining - amount_in_consumed
        calculated = _add_u64(state.amount_calculated, amount_out, "amount_calculated")
    else:
        if amount_out > state.amount_specified_remaining:
            raise RaydiumFeeAccountingError("output consumption exceeds remaining specified amount")
        remaining = state.amount_specified_remaining - amount_out
        calculated = _add_u64(state.amount_calculated, amount_in_consumed, "amount_calculated")

    protocol_delta = fee_amount * protocol_fee_rate // FEE_RATE_DENOMINATOR
    fund_delta = fee_amount * fund_fee_rate // FEE_RATE_DENOMINATOR
    protocol_fee = _add_u64(state.protocol_fee, protocol_delta, "protocol_fee")
    fund_fee = _add_u64(state.fund_fee, fund_delta, "fund_fee")
    remaining_fee = fee_amount - protocol_delta - fund_delta

    if state.liquidity > 0:
        growth_delta = remaining_fee * Q64 // state.liquidity
        if growth_delta > U128_MAX:
            raise RaydiumFeeAccountingError("fee growth delta exceeds u128")
        fee_growth = (state.fee_growth_global_x64 + growth_delta) % (1 << 128)
        lp_fee = _add_u64(state.lp_fee, remaining_fee, "lp_fee")
    else:
        fee_growth = state.fee_growth_global_x64
        protocol_fee = _add_u64(protocol_fee, remaining_fee, "protocol_fee")
        lp_fee = state.lp_fee

    return replace(
        state,
        amount_specified_remaining=remaining,
        amount_calculated=calculated,
        fee_growth_global_x64=fee_growth,
        lp_fee=lp_fee,
        protocol_fee=protocol_fee,
        fund_fee=fund_fee,
    )


def settle_raydium_swap_amounts(
    state: RaydiumFeeAccountingState,
    *,
    amount_specified: int,
    zero_for_one: bool,
    is_base_input: bool,
    fee_on_token0: bool,
) -> RaydiumSwapSettlement:
    """Port SwapState::settle_amounts and return token-side trade fee totals."""
    if not isinstance(state, RaydiumFeeAccountingState):
        raise RaydiumFeeAccountingError("Raydium fee-accounting state is required")
    _integer(amount_specified, U64_MAX, "amount_specified")
    _integer(state.amount_specified_remaining, U64_MAX, "amount_specified_remaining")
    for value, field in (
        (zero_for_one, "zero_for_one"),
        (is_base_input, "is_base_input"),
        (fee_on_token0, "fee_on_token0"),
    ):
        if not isinstance(value, bool):
            raise RaydiumFeeAccountingError(f"{field} must be boolean")
    _integer(state.amount_calculated, U64_MAX, "amount_calculated")
    for value, field in (
        (state.lp_fee, "lp_fee"),
        (state.protocol_fee, "protocol_fee"),
        (state.fund_fee, "fund_fee"),
    ):
        _integer(value, U64_MAX, field)
    if state.amount_specified_remaining > amount_specified:
        raise RaydiumFeeAccountingError("remaining specified amount exceeds original amount")
    consumed = amount_specified - state.amount_specified_remaining
    if zero_for_one == is_base_input:
        amount_0, amount_1 = consumed, state.amount_calculated
    else:
        amount_0, amount_1 = state.amount_calculated, consumed
    total_trade_fee = _add_u64(
        _add_u64(state.lp_fee, state.protocol_fee, "total_trade_fee"),
        state.fund_fee,
        "total_trade_fee",
    )
    trade_fee_0, trade_fee_1 = (total_trade_fee, 0) if fee_on_token0 else (0, total_trade_fee)
    return RaydiumSwapSettlement(amount_0, amount_1, trade_fee_0, trade_fee_1)
