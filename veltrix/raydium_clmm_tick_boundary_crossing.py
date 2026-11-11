"""Raydium CLMM tick-boundary fee/reward growth crossing and liquidity delta."""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_crossing import (
    RaydiumRewardGrowth,
    RaydiumTickCrossingError,
    cross_tick_state,
    apply_crossed_tick_liquidity,
)


class RaydiumTickBoundaryCrossingError(ValueError):
    """Raised when a Raydium boundary-crossing transition is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumTickBoundaryCrossingResult:
    tick: RaydiumClmmTick
    liquidity_after: int
    crossed: bool
    liquidity_net: int


def cross_raydium_clmm_tick_at_boundary(
    pool: RaydiumClmmPoolState,
    tick: RaydiumClmmTick,
    *,
    current_liquidity: int,
    fee_growth_global_x64: int,
    zero_for_one: bool,
) -> RaydiumTickBoundaryCrossingResult:
    """Match swap.rs:719-742; fee growth is assigned to the fee-on token side."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumTickBoundaryCrossingError("decoded Raydium PoolState is required")
    if not isinstance(tick, RaydiumClmmTick):
        raise RaydiumTickBoundaryCrossingError("decoded Raydium TickState is required")
    if not isinstance(zero_for_one, bool):
        raise RaydiumTickBoundaryCrossingError("swap direction must be boolean")
    if isinstance(current_liquidity, bool) or not isinstance(current_liquidity, int) or not 0 <= current_liquidity < (1 << 128):
        raise RaydiumTickBoundaryCrossingError("current liquidity must fit u128")
    if isinstance(fee_growth_global_x64, bool) or not isinstance(fee_growth_global_x64, int) or not 0 <= fee_growth_global_x64 < (1 << 128):
        raise RaydiumTickBoundaryCrossingError("current fee growth must fit u128")

    has_limit_orders = tick.orders_amount > 0 or tick.part_filled_orders_remaining > 0
    if tick.liquidity_gross == 0 or has_limit_orders:
        return RaydiumTickBoundaryCrossingResult(
            tick=tick,
            liquidity_after=current_liquidity,
            crossed=False,
            liquidity_net=0,
        )
    if tick.initialized is not True:
        raise RaydiumTickBoundaryCrossingError("liquidity-bearing boundary tick must remain initialized")
    if pool.fee_on == 0:
        fee_on_token0 = zero_for_one
    elif pool.fee_on == 1:
        fee_on_token0 = True
    elif pool.fee_on == 2:
        fee_on_token0 = False
    else:
        fee_on_token0 = zero_for_one

    if fee_on_token0:
        fee_growth_0 = fee_growth_global_x64
        fee_growth_1 = pool.fee_growth_global_1_x64
    else:
        fee_growth_0 = pool.fee_growth_global_0_x64
        fee_growth_1 = fee_growth_global_x64

    if not isinstance(pool.reward_infos, tuple) or len(pool.reward_infos) != 3:
        raise RaydiumTickBoundaryCrossingError("PoolState must contain exactly three reward slots")
    rewards = tuple(
        RaydiumRewardGrowth(
            initialized=reward.initialized,
            reward_growth_global_x64=reward.reward_growth_global_x64,
        )
        for reward in pool.reward_infos
    )
    try:
        crossed_tick, liquidity_net = cross_tick_state(
            tick,
            fee_growth_global_0_x64=fee_growth_0,
            fee_growth_global_1_x64=fee_growth_1,
            reward_infos=rewards,
        )
        liquidity_after = apply_crossed_tick_liquidity(
            current_liquidity,
            liquidity_net,
            zero_for_one=zero_for_one,
        )
    except RaydiumTickCrossingError as exc:
        raise RaydiumTickBoundaryCrossingError(f"Raydium tick boundary crossing failed: {exc}") from exc

    return RaydiumTickBoundaryCrossingResult(
        tick=crossed_tick,
        liquidity_after=liquidity_after,
        crossed=True,
        liquidity_net=liquidity_net,
    )
