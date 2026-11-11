"""Pinned Raydium CLMM tick-crossing fee/reward and liquidity arithmetic."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick


U128_MAX = (1 << 128) - 1
I128_MIN = -(1 << 127)
I128_MAX = (1 << 127) - 1
REWARD_COUNT = 3


class RaydiumTickCrossingError(ValueError):
    """Raised when a tick crossing or liquidity transition is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumRewardGrowth:
    initialized: bool
    reward_growth_global_x64: int


@dataclass(frozen=True, slots=True)
class RaydiumTickCrossingResult:
    tick: RaydiumClmmTick
    liquidity_net: int
    liquidity_after: int


def _u128(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U128_MAX:
        raise RaydiumTickCrossingError(f"{name} must fit u128")
    return value


def _i128(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not I128_MIN <= value <= I128_MAX:
        raise RaydiumTickCrossingError(f"{name} must fit i128")
    return value


def add_liquidity_delta(liquidity: int, delta: int) -> int:
    """Match pinned liquidity_math::add_delta checked u128 arithmetic."""
    liquidity = _u128(liquidity, "liquidity")
    delta = _i128(delta, "liquidity delta")
    if delta < 0:
        amount = -delta
        if amount > liquidity:
            raise RaydiumTickCrossingError("liquidity subtraction underflows u128")
        return liquidity - amount
    result = liquidity + delta
    if result > U128_MAX:
        raise RaydiumTickCrossingError("liquidity addition overflows u128")
    return result


def cross_tick_state(
    tick: RaydiumClmmTick,
    *,
    fee_growth_global_0_x64: int,
    fee_growth_global_1_x64: int,
    reward_infos: tuple[RaydiumRewardGrowth, ...],
) -> tuple[RaydiumClmmTick, int]:
    """Match TickState::cross; only initialized reward slots are inverted."""
    if not isinstance(tick, RaydiumClmmTick):
        raise RaydiumTickCrossingError("decoded Raydium tick state is required")
    fee0 = _u128(fee_growth_global_0_x64, "global token-0 fee growth")
    fee1 = _u128(fee_growth_global_1_x64, "global token-1 fee growth")
    liquidity_net = _i128(tick.liquidity_net, "tick liquidity net")
    if tick.initialized is not True:
        raise RaydiumTickCrossingError("tick crossing requires a decoded initialized tick")
    if tick.liquidity_gross <= 0 or tick.orders_amount > 0 or tick.part_filled_orders_remaining > 0:
        raise RaydiumTickCrossingError(
            "tick crossing requires liquidity and no outstanding limit orders"
        )
    if not isinstance(reward_infos, tuple) or len(reward_infos) != REWARD_COUNT:
        raise RaydiumTickCrossingError("exactly three Raydium reward slots are required")
    if len(tick.reward_growths_outside_x64) != REWARD_COUNT:
        raise RaydiumTickCrossingError("tick reward-growth state must contain three slots")

    old_rewards = tuple(
        _u128(value, f"tick reward growth outside {index}")
        for index, value in enumerate(tick.reward_growths_outside_x64)
    )
    new_rewards = []
    for index, reward in enumerate(reward_infos):
        if not isinstance(reward, RaydiumRewardGrowth) or not isinstance(reward.initialized, bool):
            raise RaydiumTickCrossingError(f"reward slot {index} is invalid")
        global_growth = _u128(reward.reward_growth_global_x64, f"global reward growth {index}")
        if reward.initialized:
            new_rewards.append((global_growth - old_rewards[index]) % (1 << 128))
        else:
            new_rewards.append(old_rewards[index])

    crossed = replace(
        tick,
        fee_growth_outside_0_x64=(fee0 - _u128(tick.fee_growth_outside_0_x64, "outside token-0 fee growth")) % (1 << 128),
        fee_growth_outside_1_x64=(fee1 - _u128(tick.fee_growth_outside_1_x64, "outside token-1 fee growth")) % (1 << 128),
        reward_growths_outside_x64=tuple(new_rewards),
    )
    return crossed, liquidity_net


def apply_crossed_tick_liquidity(
    current_liquidity: int,
    liquidity_net: int,
    *,
    zero_for_one: bool,
) -> int:
    """Match swap.rs: negate liquidity_net only for zero_for_one, then add_delta."""
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickCrossingError("swap direction must be boolean")
    delta = _i128(liquidity_net, "crossed tick liquidity net")
    if zero_for_one:
        if delta == I128_MIN:
            raise RaydiumTickCrossingError("negating i128 minimum liquidity delta overflows")
        delta = -delta
    return add_liquidity_delta(current_liquidity, delta)


def cross_tick_and_apply_liquidity(
    tick: RaydiumClmmTick,
    *,
    current_liquidity: int,
    fee_growth_global_0_x64: int,
    fee_growth_global_1_x64: int,
    reward_infos: tuple[RaydiumRewardGrowth, ...],
    zero_for_one: bool,
) -> RaydiumTickCrossingResult:
    crossed, liquidity_net = cross_tick_state(
        tick,
        fee_growth_global_0_x64=fee_growth_global_0_x64,
        fee_growth_global_1_x64=fee_growth_global_1_x64,
        reward_infos=reward_infos,
    )
    liquidity_after = apply_crossed_tick_liquidity(
        current_liquidity, liquidity_net, zero_for_one=zero_for_one
    )
    return RaydiumTickCrossingResult(crossed, liquidity_net, liquidity_after)
