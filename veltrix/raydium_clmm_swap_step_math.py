"""Integer-exact single-step Raydium CLMM swap math from the pinned source.

This module ports liquidity deltas, next-sqrt-price rounding and the static
fee-mode swap-step calculation. Dynamic fees, limit orders, tick crossing,
rewards and observation updates are deliberately outside this component.
"""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_tick_math import (
    RAYDIUM_MAX_SQRT_PRICE_X64,
    RAYDIUM_MIN_SQRT_PRICE_X64,
)


Q64 = 1 << 64
U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
U256_MAX = (1 << 256) - 1
FEE_RATE_DENOMINATOR = 1_000_000


class RaydiumSwapMathError(ValueError):
    """Raised for invalid or overflowing pinned Raydium swap-step arithmetic."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RaydiumSwapStepResult:
    sqrt_price_next_x64: int
    amount_in: int
    amount_out: int
    fee_amount: int
    reached_target: bool


def _int(value: int, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RaydiumSwapMathError(f"{name} must be an integer in [0, {maximum}]")
    return value


def mul_div_floor(value: int, numerator: int, denominator: int, *, maximum: int = U256_MAX) -> int:
    if denominator <= 0:
        raise RaydiumSwapMathError("mul-div denominator must be positive", code="division_by_zero")
    result = value * numerator // denominator
    if result > maximum:
        raise RaydiumSwapMathError("mul-div result exceeds its target integer width", code="overflow")
    return result


def mul_div_ceil(value: int, numerator: int, denominator: int, *, maximum: int = U256_MAX) -> int:
    if denominator <= 0:
        raise RaydiumSwapMathError("mul-div denominator must be positive", code="division_by_zero")
    result = (value * numerator + denominator - 1) // denominator
    if result > maximum:
        raise RaydiumSwapMathError("mul-div result exceeds its target integer width", code="overflow")
    return result


def _u64_result(value: int, label: str) -> int:
    if value > U64_MAX:
        raise RaydiumSwapMathError(f"{label} exceeds u64", code="max_token_overflow")
    return value


def get_delta_amount_0_unsigned(
    sqrt_ratio_a_x64: int,
    sqrt_ratio_b_x64: int,
    liquidity: int,
    *,
    round_up: bool,
) -> int:
    sqrt_ratio_a_x64 = _int(sqrt_ratio_a_x64, "sqrt_ratio_a_x64", U128_MAX)
    sqrt_ratio_b_x64 = _int(sqrt_ratio_b_x64, "sqrt_ratio_b_x64", U128_MAX)
    liquidity = _int(liquidity, "liquidity", U128_MAX)
    low, high = sorted((sqrt_ratio_a_x64, sqrt_ratio_b_x64))
    if low == 0:
        raise RaydiumSwapMathError("sqrt ratio must be positive", code="zero_sqrt_price")
    numerator_1 = liquidity << 64
    numerator_2 = high - low
    denominator = low * high
    value = (
        mul_div_ceil(numerator_1, numerator_2, denominator)
        if round_up else mul_div_floor(numerator_1, numerator_2, denominator)
    )
    return _u64_result(value, "amount_0 delta")


def get_delta_amount_1_unsigned(
    sqrt_ratio_a_x64: int,
    sqrt_ratio_b_x64: int,
    liquidity: int,
    *,
    round_up: bool,
) -> int:
    sqrt_ratio_a_x64 = _int(sqrt_ratio_a_x64, "sqrt_ratio_a_x64", U128_MAX)
    sqrt_ratio_b_x64 = _int(sqrt_ratio_b_x64, "sqrt_ratio_b_x64", U128_MAX)
    liquidity = _int(liquidity, "liquidity", U128_MAX)
    low, high = sorted((sqrt_ratio_a_x64, sqrt_ratio_b_x64))
    difference = high - low
    value = (
        mul_div_ceil(liquidity, difference, Q64)
        if round_up else mul_div_floor(liquidity, difference, Q64)
    )
    return _u64_result(value, "amount_1 delta")


def get_delta_amounts_for_swap(
    sqrt_ratio_a_x64: int,
    sqrt_ratio_b_x64: int,
    liquidity: int,
    *,
    zero_for_one: bool,
) -> tuple[int, int]:
    sqrt_ratio_a_x64 = _int(sqrt_ratio_a_x64, "sqrt_ratio_a_x64", U128_MAX)
    sqrt_ratio_b_x64 = _int(sqrt_ratio_b_x64, "sqrt_ratio_b_x64", U128_MAX)
    liquidity = _int(liquidity, "liquidity", U128_MAX)
    if not isinstance(zero_for_one, bool):
        raise RaydiumSwapMathError("zero_for_one must be a boolean")
    low, high = sorted((sqrt_ratio_a_x64, sqrt_ratio_b_x64))
    if low == 0:
        raise RaydiumSwapMathError("sqrt ratio must be positive", code="zero_sqrt_price")
    amount_1_x64 = liquidity * (high - low)
    product = low * high
    if zero_for_one:
        amount_in = mul_div_ceil(amount_1_x64, Q64, product)
        amount_out = amount_1_x64 >> 64
    else:
        amount_in = (amount_1_x64 + U64_MAX) >> 64
        amount_out = mul_div_floor(amount_1_x64, Q64, product)
    return _u64_result(amount_in, "swap amount_in"), _u64_result(amount_out, "swap amount_out")


def get_next_sqrt_price_from_amount_0_rounding_up(
    sqrt_price_x64: int, liquidity: int, amount: int, *, add: bool,
) -> int:
    sqrt_price_x64 = _int(sqrt_price_x64, "sqrt_price_x64", U128_MAX)
    liquidity = _int(liquidity, "liquidity", U128_MAX)
    amount = _int(amount, "amount", U64_MAX)
    if not isinstance(add, bool):
        raise RaydiumSwapMathError("add must be a boolean")
    if sqrt_price_x64 == 0 or liquidity == 0:
        raise RaydiumSwapMathError("sqrt price and liquidity must be positive")
    if amount == 0:
        return sqrt_price_x64
    numerator_1 = liquidity << 64
    product = amount * sqrt_price_x64
    if add:
        denominator = numerator_1 + product
        if denominator < numerator_1:
            # This branch is unreachable for validated u64/u128 inputs, but mirrors
            # the upstream overflow fallback for completeness.
            denominator = numerator_1 // sqrt_price_x64 + amount
            result = (numerator_1 + denominator - 1) // denominator
        else:
            result = mul_div_ceil(numerator_1, sqrt_price_x64, denominator)
    else:
        denominator = numerator_1 - product
        if denominator < 0:
            raise RaydiumSwapMathError("token-0 removal underflows the sqrt-price denominator", code="underflow")
        result = mul_div_ceil(numerator_1, sqrt_price_x64, denominator)
    if result > U128_MAX:
        raise RaydiumSwapMathError("next sqrt price exceeds u128", code="overflow")
    return result


def get_next_sqrt_price_from_amount_1_rounding_down(
    sqrt_price_x64: int, liquidity: int, amount: int, *, add: bool,
) -> int:
    sqrt_price_x64 = _int(sqrt_price_x64, "sqrt_price_x64", U128_MAX)
    liquidity = _int(liquidity, "liquidity", U128_MAX)
    amount = _int(amount, "amount", U64_MAX)
    if not isinstance(add, bool):
        raise RaydiumSwapMathError("add must be a boolean")
    if liquidity == 0:
        raise RaydiumSwapMathError("liquidity must be positive", code="zero_liquidity")
    if amount == 0:
        return sqrt_price_x64
    numerator = amount << 64
    quotient = numerator // liquidity if add else (numerator + liquidity - 1) // liquidity
    result = sqrt_price_x64 + quotient if add else sqrt_price_x64 - quotient
    if result < 0 or result > U128_MAX:
        raise RaydiumSwapMathError("next sqrt price exceeds u128 bounds", code="overflow")
    return result


def get_next_sqrt_price_from_input(
    sqrt_price_x64: int, liquidity: int, amount_in: int, *, zero_for_one: bool,
) -> int:
    if not isinstance(zero_for_one, bool):
        raise RaydiumSwapMathError("zero_for_one must be a boolean")
    if not RAYDIUM_MIN_SQRT_PRICE_X64 <= sqrt_price_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumSwapMathError("current sqrt price is outside the Raydium range")
    if liquidity <= 0:
        raise RaydiumSwapMathError("liquidity must be positive", code="zero_liquidity")
    return (
        get_next_sqrt_price_from_amount_0_rounding_up(sqrt_price_x64, liquidity, amount_in, add=True)
        if zero_for_one else
        get_next_sqrt_price_from_amount_1_rounding_down(sqrt_price_x64, liquidity, amount_in, add=True)
    )


def get_next_sqrt_price_from_output(
    sqrt_price_x64: int, liquidity: int, amount_out: int, *, zero_for_one: bool,
) -> int:
    if not isinstance(zero_for_one, bool):
        raise RaydiumSwapMathError("zero_for_one must be a boolean")
    if not RAYDIUM_MIN_SQRT_PRICE_X64 <= sqrt_price_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumSwapMathError("current sqrt price is outside the Raydium range")
    if liquidity <= 0:
        raise RaydiumSwapMathError("liquidity must be positive", code="zero_liquidity")
    return (
        get_next_sqrt_price_from_amount_1_rounding_down(sqrt_price_x64, liquidity, amount_out, add=False)
        if zero_for_one else
        get_next_sqrt_price_from_amount_0_rounding_up(sqrt_price_x64, liquidity, amount_out, add=False)
    )


def compute_swap_step(
    sqrt_price_current_x64: int,
    sqrt_price_target_x64: int,
    liquidity: int,
    amount_remaining: int,
    fee_rate: int,
    *,
    is_base_input: bool,
    zero_for_one: bool,
    is_fee_on_input: bool,
) -> RaydiumSwapStepResult:
    """Port the pinned Raydium static-fee compute_swap step exactly."""
    sqrt_price_current_x64 = _int(sqrt_price_current_x64, "sqrt_price_current_x64", U128_MAX)
    sqrt_price_target_x64 = _int(sqrt_price_target_x64, "sqrt_price_target_x64", U128_MAX)
    liquidity = _int(liquidity, "liquidity", U128_MAX)
    amount_remaining = _int(amount_remaining, "amount_remaining", U64_MAX)
    fee_rate = _int(fee_rate, "fee_rate", FEE_RATE_DENOMINATOR - 1)
    for flag, name in (
        (is_base_input, "is_base_input"),
        (zero_for_one, "zero_for_one"),
        (is_fee_on_input, "is_fee_on_input"),
    ):
        if not isinstance(flag, bool):
            raise RaydiumSwapMathError(f"{name} must be a boolean")
    if amount_remaining == 0:
        raise RaydiumSwapMathError("amount_remaining must be positive")
    if not RAYDIUM_MIN_SQRT_PRICE_X64 <= sqrt_price_current_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumSwapMathError("current sqrt price is outside the Raydium range")
    if not RAYDIUM_MIN_SQRT_PRICE_X64 <= sqrt_price_target_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumSwapMathError("target sqrt price is outside the Raydium range")
    if zero_for_one and sqrt_price_target_x64 > sqrt_price_current_x64:
        raise RaydiumSwapMathError("zero-for-one target price is above current price")
    if not zero_for_one and sqrt_price_target_x64 < sqrt_price_current_x64:
        raise RaydiumSwapMathError("one-for-zero target price is below current price")

    if is_base_input:
        amount_for_price = (
            mul_div_floor(amount_remaining, FEE_RATE_DENOMINATOR - fee_rate, FEE_RATE_DENOMINATOR, maximum=U64_MAX)
            if is_fee_on_input else amount_remaining
        )
    else:
        amount_for_price = (
            amount_remaining if is_fee_on_input else
            mul_div_ceil(amount_remaining, FEE_RATE_DENOMINATOR, FEE_RATE_DENOMINATOR - fee_rate, maximum=U64_MAX)
        )

    max_reachable = None
    try:
        target_in, target_out = get_delta_amounts_for_swap(
            sqrt_price_target_x64, sqrt_price_current_x64, liquidity,
            zero_for_one=zero_for_one,
        )
        user_limit = target_in if is_base_input else target_out
        if amount_for_price >= user_limit:
            max_reachable = (target_in, target_out)
    except RaydiumSwapMathError as exc:
        if exc.code != "max_token_overflow":
            raise

    if max_reachable is not None:
        sqrt_next = sqrt_price_target_x64
        amount_in, amount_out = max_reachable
    else:
        sqrt_next = (
            get_next_sqrt_price_from_input(
                sqrt_price_current_x64, liquidity, amount_for_price, zero_for_one=zero_for_one
            )
            if is_base_input else
            get_next_sqrt_price_from_output(
                sqrt_price_current_x64, liquidity, amount_for_price, zero_for_one=zero_for_one
            )
        )
        amount_in, amount_out = get_delta_amounts_for_swap(
            sqrt_next, sqrt_price_current_x64, liquidity, zero_for_one=zero_for_one
        )

    if zero_for_one:
        if sqrt_next < sqrt_price_target_x64:
            raise RaydiumSwapMathError("computed sqrt price passed the zero-for-one target")
    elif sqrt_next > sqrt_price_target_x64:
        raise RaydiumSwapMathError("computed sqrt price passed the one-for-zero target")

    reached_target = sqrt_next == sqrt_price_target_x64
    if is_base_input:
        if is_fee_on_input:
            fee_amount = (
                amount_remaining - amount_in if not reached_target else
                mul_div_ceil(amount_in, fee_rate, FEE_RATE_DENOMINATOR - fee_rate, maximum=U64_MAX)
            )
        else:
            fee_amount = mul_div_ceil(amount_out, fee_rate, FEE_RATE_DENOMINATOR, maximum=U64_MAX)
            amount_out -= fee_amount
            if not reached_target:
                amount_in = amount_remaining
    else:
        if is_fee_on_input:
            amount_out = min(amount_out, amount_remaining)
            fee_amount = mul_div_ceil(amount_in, fee_rate, FEE_RATE_DENOMINATOR - fee_rate, maximum=U64_MAX)
        else:
            fee_amount = mul_div_ceil(amount_out, fee_rate, FEE_RATE_DENOMINATOR, maximum=U64_MAX)
            net_output = amount_out - fee_amount
            if net_output > amount_remaining:
                fee_amount = amount_out - amount_remaining
                amount_out = amount_remaining
            else:
                amount_out = net_output

    return RaydiumSwapStepResult(
        sqrt_price_next_x64=sqrt_next,
        amount_in=amount_in,
        amount_out=amount_out,
        fee_amount=fee_amount,
        reached_target=reached_target,
    )
