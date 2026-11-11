"""Pinned Raydium CLMM tick-crossing and FIFO limit-order state transitions."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import Q64


U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
I128_MIN = -(1 << 127)
I128_MAX = (1 << 127) - 1
FEE_RATE_DENOMINATOR = 1_000_000


class RaydiumTickTransitionError(ValueError):
    """Raised for invalid tick-crossing or limit-order state transitions."""


@dataclass(frozen=True, slots=True)
class RaydiumLimitOrderMatchResult:
    amount_in: int = 0
    amount_out: int = 0
    amm_fee_amount: int = 0


@dataclass(frozen=True, slots=True)
class RaydiumTickCrossingResult:
    tick: RaydiumClmmTick
    liquidity_net: int
    next_tick_current: int
    next_liquidity: int


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise RaydiumTickTransitionError("division denominator must be positive")
    return (numerator + denominator - 1) // denominator


def get_price_from_sqrt_price(sqrt_price_x64: int, *, round_up: bool) -> int:
    if isinstance(sqrt_price_x64, bool) or not isinstance(sqrt_price_x64, int) or not 0 < sqrt_price_x64 <= U128_MAX:
        raise RaydiumTickTransitionError("sqrt price must be a positive u128")
    if not isinstance(round_up, bool):
        raise RaydiumTickTransitionError("round_up must be a boolean")
    product = sqrt_price_x64 * sqrt_price_x64
    if round_up:
        product += Q64 - 1
    price = product >> 64
    if price > U128_MAX:
        raise RaydiumTickTransitionError("Q64.64 price exceeds u128")
    return price


def get_limit_order_output_with_price(
    amount_in: int, token_0_price_x64: int, zero_for_one: bool,
) -> int:
    if isinstance(amount_in, bool) or not isinstance(amount_in, int) or not 0 <= amount_in <= U64_MAX:
        raise RaydiumTickTransitionError("limit-order amount_in must fit u64")
    if isinstance(token_0_price_x64, bool) or not isinstance(token_0_price_x64, int) or not 0 < token_0_price_x64 <= U128_MAX:
        raise RaydiumTickTransitionError("token-0 price must be a positive u128")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickTransitionError("zero_for_one must be a boolean")
    return (
        amount_in * token_0_price_x64 // Q64
        if zero_for_one else
        amount_in * Q64 // token_0_price_x64
    )


def get_limit_order_input_with_price(
    amount_out: int, token_0_price_x64: int, zero_for_one: bool,
) -> int:
    if isinstance(amount_out, bool) or not isinstance(amount_out, int) or not 0 <= amount_out <= U64_MAX:
        raise RaydiumTickTransitionError("limit-order amount_out must fit u64")
    if isinstance(token_0_price_x64, bool) or not isinstance(token_0_price_x64, int) or not 0 < token_0_price_x64 <= U128_MAX:
        raise RaydiumTickTransitionError("token-0 price must be a positive u128")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickTransitionError("zero_for_one must be a boolean")
    amount_in = (
        _ceil_div(amount_out * token_0_price_x64, Q64)
        if zero_for_one else
        _ceil_div(amount_out * Q64, token_0_price_x64)
    )
    if amount_in > U64_MAX:
        raise RaydiumTickTransitionError("limit-order input amount exceeds u64")
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
) -> tuple[RaydiumClmmTick, RaydiumLimitOrderMatchResult]:
    """Port TickState::match_limit_order_with_sqrt_price including FIFO cohort state."""
    if not isinstance(tick, RaydiumClmmTick):
        raise RaydiumTickTransitionError("decoded Raydium TickState is required")
    for value, name in ((zero_for_one, "zero_for_one"), (is_base_input, "is_base_input"), (is_fee_on_input, "is_fee_on_input")):
        if not isinstance(value, bool):
            raise RaydiumTickTransitionError(f"{name} must be a boolean")
    if isinstance(swap_amount, bool) or not isinstance(swap_amount, int) or not 0 <= swap_amount <= U64_MAX:
        raise RaydiumTickTransitionError("swap_amount must fit u64")
    if isinstance(fee_rate, bool) or not isinstance(fee_rate, int) or not 0 <= fee_rate < FEE_RATE_DENOMINATOR:
        raise RaydiumTickTransitionError("fee_rate must be below the Raydium fee denominator")
    if isinstance(tick.orders_amount, bool) or not isinstance(tick.orders_amount, int) or not 0 <= tick.orders_amount <= U64_MAX:
        raise RaydiumTickTransitionError("orders_amount must fit u64")
    if isinstance(tick.part_filled_orders_remaining, bool) or not isinstance(tick.part_filled_orders_remaining, int) or not 0 <= tick.part_filled_orders_remaining <= U64_MAX:
        raise RaydiumTickTransitionError("part_filled_orders_remaining must fit u64")
    total_unfilled = tick.orders_amount + tick.part_filled_orders_remaining
    if total_unfilled > U64_MAX:
        raise RaydiumTickTransitionError("total unfilled limit-order amount overflows u64")
    if swap_amount == 0 or total_unfilled == 0:
        return tick, RaydiumLimitOrderMatchResult()

    token_0_price_x64 = get_price_from_sqrt_price(
        sqrt_price_x64, round_up=not zero_for_one
    )
    amount_in = 0
    amount_out = 0
    fee_amount = 0

    if is_base_input:
        if is_fee_on_input:
            fee_amount = _ceil_div(swap_amount * fee_rate, FEE_RATE_DENOMINATOR)
            amount_in = swap_amount - fee_amount
        else:
            amount_in = swap_amount
        matched_output = get_limit_order_output_with_price(
            amount_in, token_0_price_x64, zero_for_one
        )
        if matched_output > total_unfilled:
            amount_out = total_unfilled
            amount_in = get_limit_order_input_with_price(
                amount_out, token_0_price_x64, not zero_for_one
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
                _ceil_div(net_output * FEE_RATE_DENOMINATOR, FEE_RATE_DENOMINATOR - fee_rate),
                total_unfilled,
            )
        amount_in = get_limit_order_input_with_price(
            amount_out, token_0_price_x64, not zero_for_one
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
    amount_out_to_consume = amount_out - consume_part

    if amount_out_to_consume > 0:
        if part_remaining != 0 or orders_amount < amount_out_to_consume:
            raise RaydiumTickTransitionError("limit-order FIFO amount does not match remaining cohorts")
        order_phase = min(U64_MAX, order_phase + 1)
        unfilled_ratio = Q64 * (orders_amount - amount_out_to_consume) // orders_amount
        part_remaining = orders_amount - amount_out_to_consume
        orders_amount = 0

    if not is_fee_on_input:
        fee_amount = _ceil_div(amount_out * fee_rate, FEE_RATE_DENOMINATOR)
        if fee_amount > amount_out:
            raise RaydiumTickTransitionError("limit-order output fee exceeds gross output")
        amount_out -= fee_amount

    if any(value > U64_MAX for value in (amount_in, amount_out, fee_amount)):
        raise RaydiumTickTransitionError("limit-order result exceeds u64")
    updated_tick = replace(
        tick,
        order_phase=order_phase,
        orders_amount=orders_amount,
        part_filled_orders_remaining=part_remaining,
        unfilled_ratio_x64=unfilled_ratio,
        initialized=(
            tick.liquidity_gross > 0 or orders_amount > 0 or part_remaining > 0
        ),
    )
    return updated_tick, RaydiumLimitOrderMatchResult(amount_in, amount_out, fee_amount)


def cross_raydium_tick(
    tick: RaydiumClmmTick,
    *,
    fee_growth_global_0_x64: int,
    fee_growth_global_1_x64: int,
    reward_growths_global_x64: tuple[int, int, int],
    reward_initialized: tuple[bool, bool, bool],
    zero_for_one: bool,
    liquidity: int,
) -> RaydiumTickCrossingResult:
    """Port TickState::cross and the swap-loop liquidity/tick direction update."""
    if not isinstance(tick, RaydiumClmmTick):
        raise RaydiumTickTransitionError("decoded Raydium TickState is required")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickTransitionError("zero_for_one must be a boolean")
    if len(reward_growths_global_x64) != 3 or len(reward_initialized) != 3:
        raise RaydiumTickTransitionError("exactly three reward growth states are required")
    if any(not isinstance(flag, bool) for flag in reward_initialized):
        raise RaydiumTickTransitionError("reward_initialized entries must be booleans")
    for value, name in (
        (fee_growth_global_0_x64, "fee_growth_global_0_x64"),
        (fee_growth_global_1_x64, "fee_growth_global_1_x64"),
        *[(v, f"reward_growths_global_x64[{i}]") for i, v in enumerate(reward_growths_global_x64)],
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U128_MAX:
            raise RaydiumTickTransitionError(f"{name} must fit u128")
    if isinstance(liquidity, bool) or not isinstance(liquidity, int) or not 0 <= liquidity <= U128_MAX:
        raise RaydiumTickTransitionError("liquidity must fit u128")
    liquidity_net = tick.liquidity_net
    if isinstance(liquidity_net, bool) or not isinstance(liquidity_net, int) or not I128_MIN <= liquidity_net <= I128_MAX:
        raise RaydiumTickTransitionError("tick liquidity_net must fit i128")

    rewards = list(tick.reward_growths_outside_x64)
    for index in range(3):
        if reward_initialized[index]:
            rewards[index] = (
                reward_growths_global_x64[index] - rewards[index]
            ) % (1 << 128)
    updated = replace(
        tick,
        fee_growth_outside_0_x64=(
            fee_growth_global_0_x64 - tick.fee_growth_outside_0_x64
        ) % (1 << 128),
        fee_growth_outside_1_x64=(
            fee_growth_global_1_x64 - tick.fee_growth_outside_1_x64
        ) % (1 << 128),
        reward_growths_outside_x64=tuple(rewards),
    )
    liquidity_delta = -liquidity_net if zero_for_one else liquidity_net
    next_liquidity = liquidity + liquidity_delta
    if not 0 <= next_liquidity <= U128_MAX:
        raise RaydiumTickTransitionError("tick crossing liquidity delta underflows or overflows u128")
    next_tick_current = tick.tick - 1 if zero_for_one else tick.tick
    if not -(1 << 31) <= next_tick_current < (1 << 31):
        raise RaydiumTickTransitionError("post-crossing current tick overflows i32")
    return RaydiumTickCrossingResult(
        tick=updated,
        liquidity_net=liquidity_net,
        next_tick_current=next_tick_current,
        next_liquidity=next_liquidity,
    )
