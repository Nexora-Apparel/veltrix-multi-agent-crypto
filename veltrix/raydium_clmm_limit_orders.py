"""Pinned Raydium CLMM FIFO limit-order matching transitions.

Source: programs/amm/src/states/tick_array.rs at
6d7a0f82d2f293c553c69aaf943377367c6c4cc6.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import Q64

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
FEE_RATE_DENOMINATOR = 1_000_000


class RaydiumLimitOrderError(ValueError):
    """Raised when limit-order state or a match transition is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumLimitOrderMatch:
    amount_in: int = 0
    amount_out: int = 0
    amm_fee_amount: int = 0


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise RaydiumLimitOrderError("division denominator must be positive")
    return (numerator + denominator - 1) // denominator


def _u64(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        raise RaydiumLimitOrderError(f"{field} must fit u64")


def _u128(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U128_MAX:
        raise RaydiumLimitOrderError(f"{field} must fit u128")


def get_token0_price_x64(sqrt_price_x64: int, *, round_up: bool) -> int:
    """Port tick_math::get_price_from_sqrt_price with its directional rounding."""
    _u128(sqrt_price_x64, "sqrt_price_x64")
    if sqrt_price_x64 == 0:
        raise RaydiumLimitOrderError("sqrt price must be positive")
    if not isinstance(round_up, bool):
        raise RaydiumLimitOrderError("round_up must be boolean")
    product = sqrt_price_x64 * sqrt_price_x64
    return (product + (Q64 - 1 if round_up else 0)) // Q64


def get_limit_order_output_with_price(
    amount_in: int, token0_price_x64: int, zero_for_one: bool
) -> int:
    _u64(amount_in, "amount_in")
    _u128(token0_price_x64, "token0_price_x64")
    if token0_price_x64 == 0 or not isinstance(zero_for_one, bool):
        raise RaydiumLimitOrderError("price must be positive and direction boolean")
    if zero_for_one:
        return amount_in * token0_price_x64 // Q64
    return amount_in * Q64 // token0_price_x64


def get_limit_order_input_with_price(
    amount_out: int, token0_price_x64: int, zero_for_one: bool
) -> int:
    _u64(amount_out, "amount_out")
    _u128(token0_price_x64, "token0_price_x64")
    if token0_price_x64 == 0 or not isinstance(zero_for_one, bool):
        raise RaydiumLimitOrderError("price must be positive and direction boolean")
    if zero_for_one:
        amount_in = _ceil_div(amount_out * token0_price_x64, Q64)
    else:
        amount_in = _ceil_div(amount_out * Q64, token0_price_x64)
    if amount_in > U64_MAX:
        raise RaydiumLimitOrderError("limit-order input amount exceeds u64")
    return amount_in


def match_raydium_limit_order(
    tick: RaydiumClmmTick,
    *,
    swap_amount: int,
    zero_for_one: bool,
    is_base_input: bool,
    fee_rate: int,
    is_fee_on_input: bool,
    sqrt_price_x64: int,
) -> tuple[RaydiumClmmTick, RaydiumLimitOrderMatch]:
    """Port TickState::match_limit_order_with_sqrt_price, including FIFO cohorts."""
    if not isinstance(tick, RaydiumClmmTick):
        raise RaydiumLimitOrderError("decoded Raydium TickState is required")
    for value, field in (
        (zero_for_one, "zero_for_one"),
        (is_base_input, "is_base_input"),
        (is_fee_on_input, "is_fee_on_input"),
    ):
        if not isinstance(value, bool):
            raise RaydiumLimitOrderError(f"{field} must be boolean")
    _u64(swap_amount, "swap_amount")
    _u64(tick.orders_amount, "orders_amount")
    _u64(tick.part_filled_orders_remaining, "part_filled_orders_remaining")
    _u64(tick.order_phase, "order_phase")
    _u128(tick.unfilled_ratio_x64, "unfilled_ratio_x64")
    _u128(sqrt_price_x64, "sqrt_price_x64")
    if sqrt_price_x64 == 0:
        raise RaydiumLimitOrderError("sqrt price must be positive")
    if isinstance(fee_rate, bool) or not isinstance(fee_rate, int) or not 0 <= fee_rate < FEE_RATE_DENOMINATOR:
        raise RaydiumLimitOrderError("fee_rate must be below the Raydium fee denominator")
    if tick.unfilled_ratio_x64 > Q64:
        raise RaydiumLimitOrderError("unfilled ratio exceeds Q64.64 unity")
    total_unfilled = tick.orders_amount + tick.part_filled_orders_remaining
    if total_unfilled > U64_MAX:
        raise RaydiumLimitOrderError("total unfilled limit-order amount overflows u64")
    if swap_amount == 0 or total_unfilled == 0:
        return tick, RaydiumLimitOrderMatch()

    token0_price_x64 = get_token0_price_x64(
        sqrt_price_x64, round_up=not zero_for_one
    )
    amount_in = amount_out = fee_amount = 0

    if is_base_input:
        if is_fee_on_input:
            fee_amount = _ceil_div(swap_amount * fee_rate, FEE_RATE_DENOMINATOR)
            amount_in = swap_amount - fee_amount
        else:
            amount_in = swap_amount
        matched_output = get_limit_order_output_with_price(
            amount_in, token0_price_x64, zero_for_one
        )
        if matched_output > total_unfilled:
            amount_out = total_unfilled
            amount_in = get_limit_order_input_with_price(
                amount_out, token0_price_x64, not zero_for_one
            )
            if is_fee_on_input:
                fee_amount = _ceil_div(
                    amount_in * fee_rate, FEE_RATE_DENOMINATOR - fee_rate
                )
        else:
            amount_out = matched_output
    else:
        net_output = min(swap_amount, total_unfilled)
        if is_fee_on_input:
            amount_out = net_output
        else:
            amount_out = min(
                _ceil_div(net_output * FEE_RATE_DENOMINATOR,
                          FEE_RATE_DENOMINATOR - fee_rate),
                total_unfilled,
            )
        amount_in = get_limit_order_input_with_price(
            amount_out, token0_price_x64, not zero_for_one
        )
        if is_fee_on_input:
            fee_amount = _ceil_div(
                amount_in * fee_rate, FEE_RATE_DENOMINATOR - fee_rate
            )

    part_remaining = tick.part_filled_orders_remaining
    orders_amount = tick.orders_amount
    order_phase = tick.order_phase
    unfilled_ratio = tick.unfilled_ratio_x64
    consume_part = min(part_remaining, amount_out)
    if consume_part > 0:
        unfilled_ratio = (
            unfilled_ratio * (part_remaining - consume_part) // part_remaining
        )
    part_remaining -= consume_part
    amount_to_consume_from_orders = amount_out - consume_part
    if amount_to_consume_from_orders > 0:
        if part_remaining != 0 or orders_amount < amount_to_consume_from_orders:
            raise RaydiumLimitOrderError("FIFO cohort amounts are inconsistent")
        order_phase = min(U64_MAX, order_phase + 1)
        unfilled_ratio = (
            Q64 * (orders_amount - amount_to_consume_from_orders) // orders_amount
        )
        part_remaining = orders_amount - amount_to_consume_from_orders
        orders_amount = 0

    if not is_fee_on_input:
        fee_amount = _ceil_div(amount_out * fee_rate, FEE_RATE_DENOMINATOR)
        if fee_amount > amount_out:
            raise RaydiumLimitOrderError("output fee exceeds gross limit-order output")
        amount_out -= fee_amount

    if any(value > U64_MAX for value in (amount_in, amount_out, fee_amount)):
        raise RaydiumLimitOrderError("limit-order result exceeds u64")
    updated = replace(
        tick,
        order_phase=order_phase,
        orders_amount=orders_amount,
        part_filled_orders_remaining=part_remaining,
        unfilled_ratio_x64=unfilled_ratio,
        initialized=(tick.liquidity_gross > 0 or orders_amount > 0 or part_remaining > 0),
    )
    return updated, RaydiumLimitOrderMatch(amount_in, amount_out, fee_amount)
