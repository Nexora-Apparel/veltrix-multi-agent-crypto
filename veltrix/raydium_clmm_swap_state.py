"""Pinned Raydium swap amount accumulation, fee splitting and settlement."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_tick_math import Q64


U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
FEE_RATE_DENOMINATOR = 1_000_000


class RaydiumSwapStateError(ValueError):
    """Raised for invalid swap accumulator or fee settlement transitions."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapAccumulator:
    amount_specified: int
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
    consumed_specified_amount: int


def _u64(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        raise RaydiumSwapStateError(f"{name} must fit u64")
    return value


def _u128(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U128_MAX:
        raise RaydiumSwapStateError(f"{name} must fit u128")
    return value


def fee_on_token0_for_pool(fee_on: int, zero_for_one: bool) -> bool:
    """Map the pinned FeeOn enum to the token whose balance pays the AMM fee."""
    if isinstance(fee_on, bool) or not isinstance(fee_on, int) or fee_on not in (0, 1, 2):
        raise RaydiumSwapStateError("PoolState fee_on must be Input(0), Token0(1), or Token1(2)")
    if not isinstance(zero_for_one, bool):
        raise RaydiumSwapStateError("zero_for_one must be a boolean")
    if fee_on == 1:
        return True
    if fee_on == 2:
        return False
    return zero_for_one


def fee_is_collected_from_input(fee_on: int, zero_for_one: bool) -> bool:
    if isinstance(fee_on, bool) or not isinstance(fee_on, int) or fee_on not in (0, 1, 2):
        raise RaydiumSwapStateError("PoolState fee_on must be Input(0), Token0(1), or Token1(2)")
    if not isinstance(zero_for_one, bool):
        raise RaydiumSwapStateError("zero_for_one must be a boolean")
    return fee_on == 0 or (fee_on == 1 and zero_for_one) or (fee_on == 2 and not zero_for_one)


def create_swap_accumulator(*, amount_specified: int, liquidity: int, fee_growth_global_x64: int = 0) -> RaydiumClmmSwapAccumulator:
    return RaydiumClmmSwapAccumulator(
        amount_specified=_u64(amount_specified, "amount_specified"),
        amount_specified_remaining=_u64(amount_specified, "amount_specified"),
        amount_calculated=0,
        liquidity=_u128(liquidity, "liquidity"),
        fee_growth_global_x64=_u128(fee_growth_global_x64, "fee_growth_global_x64"),
    )


def apply_swap_amounts(
    state: RaydiumClmmSwapAccumulator,
    *,
    amount_in: int,
    amount_out: int,
    fee_amount: int,
    is_base_input: bool,
    is_fee_on_input: bool,
    protocol_fee_rate: int,
    fund_fee_rate: int,
) -> RaydiumClmmSwapAccumulator:
    if not isinstance(state, RaydiumClmmSwapAccumulator):
        raise RaydiumSwapStateError("swap accumulator is required")
    for value, name in ((is_base_input, "is_base_input"), (is_fee_on_input, "is_fee_on_input")):
        if not isinstance(value, bool):
            raise RaydiumSwapStateError(f"{name} must be a boolean")
    amount_in = _u64(amount_in, "amount_in")
    amount_out = _u64(amount_out, "amount_out")
    fee_amount = _u64(fee_amount, "fee_amount")
    protocol_fee_rate = _u64(protocol_fee_rate, "protocol_fee_rate")
    fund_fee_rate = _u64(fund_fee_rate, "fund_fee_rate")
    if (
        protocol_fee_rate > FEE_RATE_DENOMINATOR
        or fund_fee_rate > FEE_RATE_DENOMINATOR
        or protocol_fee_rate + fund_fee_rate > FEE_RATE_DENOMINATOR
    ):
        raise RaydiumSwapStateError("protocol and fund fee rates exceed the Raydium fee denominator")

    amount_in_consumed = amount_in + fee_amount if is_fee_on_input else amount_in
    if amount_in_consumed > U64_MAX:
        raise RaydiumSwapStateError("consumed input amount overflows u64")
    if is_base_input:
        if amount_in_consumed > state.amount_specified_remaining:
            raise RaydiumSwapStateError("exact-input step consumes more than the remaining specified amount")
        remaining = state.amount_specified_remaining - amount_in_consumed
        calculated = state.amount_calculated + amount_out
    else:
        if amount_out > state.amount_specified_remaining:
            raise RaydiumSwapStateError("exact-output step delivers more than the remaining specified amount")
        remaining = state.amount_specified_remaining - amount_out
        calculated = state.amount_calculated + amount_in_consumed
    if calculated > U64_MAX:
        raise RaydiumSwapStateError("accumulated opposite-side amount overflows u64")

    protocol_delta = fee_amount * protocol_fee_rate // FEE_RATE_DENOMINATOR
    fund_delta = fee_amount * fund_fee_rate // FEE_RATE_DENOMINATOR
    remaining_fee = fee_amount - protocol_delta - fund_delta
    protocol_total = state.protocol_fee + protocol_delta
    fund_total = state.fund_fee + fund_delta
    if protocol_total > U64_MAX or fund_total > U64_MAX:
        raise RaydiumSwapStateError("accumulated protocol or fund fee overflows u64")

    if state.liquidity > 0:
        growth_delta = remaining_fee * Q64 // state.liquidity
        growth = (state.fee_growth_global_x64 + growth_delta) % (1 << 128)
        lp_total = state.lp_fee + remaining_fee
        if lp_total > U64_MAX:
            raise RaydiumSwapStateError("accumulated LP fee overflows u64")
    else:
        growth = state.fee_growth_global_x64
        lp_total = state.lp_fee
        protocol_total += remaining_fee
        if protocol_total > U64_MAX:
            raise RaydiumSwapStateError("zero-liquidity protocol fee accumulation overflows u64")

    return replace(
        state,
        amount_specified_remaining=remaining,
        amount_calculated=calculated,
        fee_growth_global_x64=growth,
        lp_fee=lp_total,
        protocol_fee=protocol_total,
        fund_fee=fund_total,
    )


def settle_swap_amounts(
    state: RaydiumClmmSwapAccumulator,
    *,
    zero_for_one: bool,
    is_base_input: bool,
    fee_on_token0: bool,
) -> RaydiumSwapSettlement:
    if not isinstance(state, RaydiumClmmSwapAccumulator):
        raise RaydiumSwapStateError("swap accumulator is required")
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool) or not isinstance(fee_on_token0, bool):
        raise RaydiumSwapStateError("swap direction, exactness and fee-token flags must be booleans")
    consumed = state.amount_specified - state.amount_specified_remaining
    if consumed < 0:
        raise RaydiumSwapStateError("remaining specified amount exceeds the original amount")
    if zero_for_one == is_base_input:
        amount_0, amount_1 = consumed, state.amount_calculated
    else:
        amount_0, amount_1 = state.amount_calculated, consumed
    total_trade_fee = state.lp_fee + state.protocol_fee + state.fund_fee
    if total_trade_fee > U64_MAX:
        raise RaydiumSwapStateError("total trade fee overflows u64")
    return RaydiumSwapSettlement(
        amount_0=amount_0,
        amount_1=amount_1,
        trade_fee_0=total_trade_fee if fee_on_token0 else 0,
        trade_fee_1=0 if fee_on_token0 else total_trade_fee,
        consumed_specified_amount=consumed,
    )
