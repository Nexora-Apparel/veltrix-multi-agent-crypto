"""Pinned Raydium CLMM FIFO limit-order matching arithmetic."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick


Q64 = 1 << 64
U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
FEE_RATE_DENOMINATOR = 1_000_000
SOURCE_COMMIT = "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"


class RaydiumLimitOrderError(ValueError):
    """Raised when limit-order matching violates pinned Raydium arithmetic."""


@dataclass(frozen=True, slots=True)
class RaydiumLimitOrderMatch:
    amount_in: int = 0
    amount_out: int = 0
    amm_fee_amount: int = 0


@dataclass(frozen=True, slots=True)
class RaydiumLimitOrderState:
    orders_amount: int
    part_filled_orders_remaining: int
    order_phase: int
    unfilled_ratio_x64: int


@dataclass(frozen=True, slots=True)
class RaydiumLimitOrderResult:
    tick: RaydiumClmmTick
    match: RaydiumLimitOrderMatch


def _uint(value: int, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RaydiumLimitOrderError(f"{name} must fit unsigned {maximum.bit_length()}-bit integer")
    return value


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise RaydiumLimitOrderError("division denominator must be positive")
    return (numerator + denominator - 1) // denominator


def get_price_from_sqrt_price(sqrt_price_x64: int, *, round_up: bool) -> int:
    sqrt_price_x64 = _uint(sqrt_price_x64, "sqrt price", U128_MAX)
    if sqrt_price_x64 == 0 or not isinstance(round_up, bool):
        raise RaydiumLimitOrderError("sqrt price must be positive and rounding flag boolean")
    product = sqrt_price_x64 * sqrt_price_x64
    if round_up:
        product += Q64 - 1
    price = product >> 64
    if price > U128_MAX:
        raise RaydiumLimitOrderError("Q64.64 price exceeds u128")
    return price


def limit_order_output_with_price(
    amount_in: int,
    token_0_price_x64: int,
    *,
    zero_for_one: bool,
) -> int:
    amount_in = _uint(amount_in, "limit-order input", U64_MAX)
    price = _uint(token_0_price_x64, "token-0 price", U128_MAX)
    if price == 0 or not isinstance(zero_for_one, bool):
        raise RaydiumLimitOrderError("price must be positive and direction boolean")
    if zero_for_one:
        output = (amount_in * price) // Q64
    else:
        output = (amount_in * Q64) // price
    if output > U128_MAX:
        raise RaydiumLimitOrderError("limit-order output exceeds u128")
    return output


def limit_order_input_with_price(
    amount_out: int,
    token_0_price_x64: int,
    *,
    zero_for_one: bool,
) -> int:
    amount_out = _uint(amount_out, "limit-order output", U64_MAX)
    price = _uint(token_0_price_x64, "token-0 price", U128_MAX)
    if price == 0 or not isinstance(zero_for_one, bool):
        raise RaydiumLimitOrderError("price must be positive and direction boolean")
    if zero_for_one:
        amount_in = _ceil_div(amount_out * price, Q64)
    else:
        amount_in = _ceil_div(amount_out * Q64, price)
    if amount_in > U64_MAX:
        raise RaydiumLimitOrderError("limit-order required input exceeds u64")
    return amount_in


def limit_order_unfilled_amount(
    orders_amount: int,
    part_filled_orders_remaining: int,
) -> int:
    orders = _uint(orders_amount, "orders amount", U64_MAX)
    part = _uint(part_filled_orders_remaining, "part-filled remainder", U64_MAX)
    total = orders + part
    if total > U64_MAX:
        raise RaydiumLimitOrderError("total unfilled limit-order amount overflows u64")
    return total


def _fee_ceil(amount: int, numerator: int, denominator: int) -> int:
    fee = _ceil_div(amount * numerator, denominator)
    if fee > U64_MAX:
        raise RaydiumLimitOrderError("limit-order fee exceeds u64")
    return fee


def match_limit_order(
    tick: RaydiumClmmTick,
    *,
    swap_amount: int,
    zero_for_one: bool,
    is_base_input: bool,
    fee_rate: int,
    is_fee_on_input: bool,
    sqrt_price_x64: int,
) -> RaydiumLimitOrderResult:
    """Match one tick's FIFO limit orders exactly as TickState::match_limit_order_with_sqrt_price."""
    if not isinstance(tick, RaydiumClmmTick):
        raise RaydiumLimitOrderError("decoded Raydium tick state is required")
    amount = _uint(swap_amount, "swap amount", U64_MAX)
    rate = _uint(fee_rate, "fee rate", FEE_RATE_DENOMINATOR - 1)
    if not all(isinstance(flag, bool) for flag in (zero_for_one, is_base_input, is_fee_on_input)):
        raise RaydiumLimitOrderError("swap direction, amount mode and fee mode must be booleans")
    orders = _uint(tick.orders_amount, "orders amount", U64_MAX)
    part_remaining = _uint(
        tick.part_filled_orders_remaining, "part-filled remainder", U64_MAX
    )
    phase = _uint(tick.order_phase, "order phase", U64_MAX)
    ratio = _uint(tick.unfilled_ratio_x64, "unfilled ratio", U128_MAX)
    if ratio > Q64:
        raise RaydiumLimitOrderError("unfilled ratio exceeds Q64.64 unity")
    total_unfilled = limit_order_unfilled_amount(orders, part_remaining)
    if total_unfilled > 0 and tick.initialized is not True:
        raise RaydiumLimitOrderError("non-empty limit-order state must be initialized")
    if amount == 0 or total_unfilled == 0:
        return RaydiumLimitOrderResult(
            tick,
            RaydiumLimitOrderMatch(),
        )

    price = get_price_from_sqrt_price(
        sqrt_price_x64, round_up=not zero_for_one
    )
    if price == 0:
        raise RaydiumLimitOrderError("derived token-0 price is zero")

    result_in = 0
    result_out = 0
    fee_amount = 0
    if is_base_input:
        if is_fee_on_input:
            fee_amount = _fee_ceil(amount, rate, FEE_RATE_DENOMINATOR)
            result_in = amount - fee_amount
        else:
            result_in = amount
        matched_output = limit_order_output_with_price(
            result_in, price, zero_for_one=zero_for_one
        )
        if matched_output > total_unfilled:
            result_out = total_unfilled
            result_in = limit_order_input_with_price(
                total_unfilled,
                price,
                zero_for_one=not zero_for_one,
            )
            if is_fee_on_input:
                fee_amount = _fee_ceil(
                    result_in, rate, FEE_RATE_DENOMINATOR - rate
                )
        else:
            result_out = matched_output
    else:
        net_output = min(amount, total_unfilled)
        if is_fee_on_input:
            result_out = net_output
        else:
            result_out = min(
                _ceil_div(net_output * FEE_RATE_DENOMINATOR, FEE_RATE_DENOMINATOR - rate),
                total_unfilled,
            )
        result_in = limit_order_input_with_price(
            result_out,
            price,
            zero_for_one=not zero_for_one,
        )
        if is_fee_on_input:
            fee_amount = _fee_ceil(
                result_in, rate, FEE_RATE_DENOMINATOR - rate
            )

    consume_from_part = min(part_remaining, result_out)
    if consume_from_part > 0:
        ratio = (
            ratio * (part_remaining - consume_from_part)
        ) // part_remaining
    part_remaining = max(0, part_remaining - consume_from_part)
    amount_out_continue = max(0, result_out - consume_from_part)

    if amount_out_continue > 0:
        if part_remaining != 0:
            raise RaydiumLimitOrderError("FIFO order cohorts are inconsistent")
        if orders < amount_out_continue:
            raise RaydiumLimitOrderError("matched output exceeds newly opened order amount")
        phase = min(U64_MAX, phase + 1)
        ratio = (Q64 * (orders - amount_out_continue)) // orders
        part_remaining = orders - amount_out_continue
        orders = 0

    if not is_fee_on_input:
        fee_amount = _fee_ceil(
            result_out, rate, FEE_RATE_DENOMINATOR
        )
        if fee_amount > result_out:
            raise RaydiumLimitOrderError("output fee exceeds gross limit-order output")
        result_out -= fee_amount

    updated_tick = replace(
        tick,
        orders_amount=orders,
        part_filled_orders_remaining=part_remaining,
        order_phase=phase,
        unfilled_ratio_x64=ratio,
        initialized=(
            tick.liquidity_gross > 0
            or orders > 0
            or part_remaining > 0
        ),
    )
    return RaydiumLimitOrderResult(
        updated_tick,
        RaydiumLimitOrderMatch(result_in, result_out, fee_amount),
    )
