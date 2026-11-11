"""Differential regressions for PR #76 combined crossing vs PR #78 composition."""
from dataclasses import dataclass, replace

import pytest

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_crossing import (
    RaydiumRewardGrowth,
    cross_tick_and_apply_liquidity,
)


@dataclass(frozen=True, slots=True)
class LegacyCrossingResult:
    tick: RaydiumClmmTick
    liquidity_net: int
    next_tick_current: int
    next_liquidity: int


def legacy_cross_raydium_tick(
    tick,
    *,
    fee_growth_global_0_x64,
    fee_growth_global_1_x64,
    reward_growths_global_x64,
    reward_initialized,
    zero_for_one,
    liquidity,
):
    """Frozen PR #76 combined crossing transition used as a test oracle."""
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
    liquidity_net = tick.liquidity_net
    delta = -liquidity_net if zero_for_one else liquidity_net
    next_liquidity = liquidity + delta
    if not 0 <= next_liquidity < (1 << 128):
        raise ValueError("historical liquidity transition out of u128 range")
    next_tick_current = tick.tick_index - 1 if zero_for_one else tick.tick_index
    return LegacyCrossingResult(
        updated, liquidity_net, next_tick_current, next_liquidity
    )


def make_tick(**overrides):
    values = dict(
        tick_index=120,
        liquidity_net=25,
        liquidity_gross=100,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=5,
        fee_growth_outside_1_x64=7,
        reward_growths_outside_x64=(15, 123, 20),
        order_phase=0,
        unfilled_ratio_x64=0,
        padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


@pytest.mark.parametrize(
    "zero_for_one,liquidity_net,liquidity",
    [
        (True, 25, 100),
        (False, 25, 100),
        (True, -25, 100),
        (False, -25, 100),
        (True, 0, 100),
        (False, 0, 100),
        (True, 100, 100),  # exact subtraction to zero
        (False, -100, 100),  # exact subtraction to zero
    ],
)
def test_historical_combined_tick_cross_matches_canonical_composed_transition(
    zero_for_one, liquidity_net, liquidity
):
    original = make_tick(liquidity_net=liquidity_net)
    fee0, fee1 = 3, 9
    reward_globals = (10, 999, 20)
    reward_initialized = (True, False, True)
    historical = legacy_cross_raydium_tick(
        original,
        fee_growth_global_0_x64=fee0,
        fee_growth_global_1_x64=fee1,
        reward_growths_global_x64=reward_globals,
        reward_initialized=reward_initialized,
        zero_for_one=zero_for_one,
        liquidity=liquidity,
    )
    canonical = cross_tick_and_apply_liquidity(
        original,
        current_liquidity=liquidity,
        fee_growth_global_0_x64=fee0,
        fee_growth_global_1_x64=fee1,
        reward_infos=tuple(
            RaydiumRewardGrowth(initialized, growth)
            for initialized, growth in zip(
                reward_initialized, reward_globals
            )
        ),
        zero_for_one=zero_for_one,
    )
    assert canonical.tick == historical.tick
    assert canonical.liquidity_net == historical.liquidity_net
    assert canonical.liquidity_after == historical.next_liquidity
    assert historical.next_tick_current == (
        original.tick_index - 1 if zero_for_one else original.tick_index
    )


def test_both_crossing_implementations_match_u128_fee_growth_wraparound():
    maximum = (1 << 128) - 1
    original = make_tick(
        fee_growth_outside_0_x64=maximum - 2,
        fee_growth_outside_1_x64=maximum - 4,
        reward_growths_outside_x64=(maximum - 1, 123, maximum - 3),
    )
    globals_ = (1, 2, 3)
    flags = (True, False, True)
    historical = cross_raydium_tick(
        original,
        fee_growth_global_0_x64=globals_[0],
        fee_growth_global_1_x64=globals_[1],
        reward_growths_global_x64=globals_,
        reward_initialized=flags,
        zero_for_one=False,
        liquidity=500,
    )
    canonical = cross_tick_and_apply_liquidity(
        original,
        current_liquidity=500,
        fee_growth_global_0_x64=globals_[0],
        fee_growth_global_1_x64=globals_[1],
        reward_infos=tuple(
            RaydiumRewardGrowth(flag, value)
            for flag, value in zip(flags, globals_)
        ),
        zero_for_one=False,
    )
    assert canonical.tick == historical.tick
    assert canonical.liquidity_after == historical.next_liquidity


@pytest.mark.parametrize("orders,part_filled", [(1, 0), (0, 1)])
def test_canonical_composition_rejects_outstanding_fifo_orders_before_crossing(
    orders, part_filled
):
    # The historical helper can compute the raw cross without caller context;
    # canonical composition adds the required no-open-orders invariant.
    tick = make_tick(
        orders_amount=orders,
        part_filled_orders_remaining=part_filled,
    )
    with pytest.raises(ValueError, match="no outstanding limit orders"):
        cross_tick_and_apply_liquidity(
            tick,
            current_liquidity=500,
            fee_growth_global_0_x64=1,
            fee_growth_global_1_x64=1,
            reward_infos=(
                RaydiumRewardGrowth(True, 1),
                RaydiumRewardGrowth(False, 2),
                RaydiumRewardGrowth(True, 3),
            ),
            zero_for_one=True,
        )
