"""Differential regression vectors against the pre-canonical Phase 09.35 API.

The frozen reference helpers below preserve the historical #75 transition
semantics. They are test-only oracles; production imports always use the
canonical Phase 09.35+ implementation.
"""
from dataclasses import replace

import pytest

from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_tick_math import (
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    get_sqrt_price_at_tick,
)
from veltrix.raydium_clmm_dynamic_fee import (
    MAX_FEE_RATE_NUMERATOR,
    RaydiumDynamicFeeSwapState,
    advance_dynamic_fee_index,
    compute_dynamic_fee_rate,
    compute_total_fee_rate,
    get_spacing_bounded_price,
    tick_spacing_index_from_tick,
    update_dynamic_fee_reference,
    update_volatility_accumulator,
)


def state_info(**overrides):
    values = dict(
        filter_period=10,
        decay_period=30,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=1_000_000,
        tick_spacing_index_reference=0,
        volatility_reference=0,
        volatility_accumulator=0,
        last_update_timestamp=100,
        padding=bytes(46),
    )
    values.update(overrides)
    return RaydiumClmmDynamicFeeInfo(**values)


# Frozen behaviour from PR #75 (9ccdc1ab91c255e2a110c73dc1b5d407a31bc182).
def legacy_tick_index(tick, spacing):
    quotient = abs(tick) // spacing
    if tick < 0:
        quotient = -quotient
        if tick % spacing:
            quotient -= 1
    return quotient


def legacy_reference(info, index, timestamp):
    elapsed = max(0, timestamp - info.last_update_timestamp)
    if elapsed < info.filter_period:
        return info
    if elapsed < info.decay_period:
        return replace(
            info,
            tick_spacing_index_reference=index,
            volatility_reference=(
                info.volatility_accumulator * info.reduction_factor
            ) // 10_000,
            last_update_timestamp=timestamp,
        )
    return replace(
        info,
        tick_spacing_index_reference=index,
        volatility_reference=0,
        last_update_timestamp=timestamp,
    )


def legacy_accumulator(info, index):
    delta = abs(info.tick_spacing_index_reference - index)
    value = info.volatility_reference + delta * 10_000
    return replace(info, volatility_accumulator=min(
        value, info.max_volatility_accumulator
    ))


def legacy_dynamic_fee(info, spacing):
    crossed = info.volatility_accumulator * spacing
    if crossed > 0xFFFFFFFF:
        raise OverflowError
    denominator = 100_000 * 10_000 * 10_000
    numerator = info.dynamic_fee_control * crossed * crossed
    return min((numerator + denominator - 1) // denominator, 100_000)


def legacy_total_fee(base, info, spacing):
    if not info.enabled:
        return base
    return min(base + legacy_dynamic_fee(info, spacing), 100_000)


def legacy_index_transition(
    info, current_index, tick, tick_next, sqrt_price, sqrt_price_next,
    spacing, zero_for_one, skipped,
):
    next_info = info
    next_index = current_index
    if skipped:
        tick_index = tick_next if sqrt_price == sqrt_price_next else tick
        next_index = legacy_tick_index(tick_index, spacing)
        if not zero_for_one and tick_index % spacing == 0:
            next_index -= 1
        if info.volatility_accumulator != info.max_volatility_accumulator:
            next_info = legacy_accumulator(info, next_index)
    next_index += -1 if zero_for_one else 1
    return next_info, next_index


def legacy_spacing_bound(target, index, spacing, liquidity, info, zero_for_one):
    if not info.enabled or liquidity == 0 or (
        info.volatility_accumulator == info.max_volatility_accumulator
    ):
        return True, target, None
    tick = index * spacing if zero_for_one else (index + 1) * spacing
    tick = max(RAYDIUM_MIN_TICK, min(RAYDIUM_MAX_TICK, tick))
    price = get_sqrt_price_at_tick(tick)
    if zero_for_one:
        if target > price:
            return False, target, None
    elif target < price:
        return False, target, None
    return False, price, tick


@pytest.mark.parametrize(
    "tick,spacing",
    [
        (0, 10), (1, 10), (9, 10), (10, 10),
        (-1, 10), (-5, 10), (-10, 10), (-11, 10),
        (-21, 10), (30, 60), (-30, 60), (-90, 60), (-121, 60),
        (443636, 1), (-443636, 1),
    ],
)
def test_canonical_tick_spacing_index_matches_historical_075(tick, spacing):
    assert tick_spacing_index_from_tick(tick, spacing) == legacy_tick_index(tick, spacing)


@pytest.mark.parametrize(
    "elapsed,index",
    [(0, -7), (9, 4), (10, 8), (29, -8), (30, 9), (100, 2)],
)
def test_canonical_reference_transition_matches_historical_075(elapsed, index):
    original = state_info(
        tick_spacing_index_reference=5,
        volatility_reference=20_000,
        volatility_accumulator=50_001,
        last_update_timestamp=100,
    )
    expected = legacy_reference(original, index, 100 + elapsed)
    actual = update_dynamic_fee_reference(
        original, tick_spacing_index=index, current_timestamp=100 + elapsed
    )
    assert actual == expected


@pytest.mark.parametrize("index", [-100, -3, -1, 0, 1, 3, 100])
def test_canonical_volatility_accumulator_matches_historical_075(index):
    original = state_info(
        tick_spacing_index_reference=-2,
        volatility_reference=20_001,
        max_volatility_accumulator=50_000,
    )
    assert update_volatility_accumulator(
        original, tick_spacing_index=index
    ) == legacy_accumulator(original, index)


@pytest.mark.parametrize(
    "accumulator,spacing,control,base",
    [
        (0, 1, 1, 0),
        (10_000, 60, 1000, 1000),
        (600_000, 10, 1000, 1000),
        (1_000_000, 1, 99_999, 90_000),
        (100_000, 20, 25_000, 75_000),
    ],
)
def test_canonical_fee_calculation_matches_historical_075(
    accumulator, spacing, control, base
):
    original = state_info(
        volatility_accumulator=accumulator,
        max_volatility_accumulator=max(accumulator, 1_000_000),
        dynamic_fee_control=control,
    )
    expected_dynamic = legacy_dynamic_fee(original, spacing)
    assert compute_dynamic_fee_rate(original, tick_spacing=spacing) == expected_dynamic
    assert compute_total_fee_rate(
        base_fee_rate=base, info=original, tick_spacing=spacing
    ) == legacy_total_fee(base, original, spacing)


@pytest.mark.parametrize(
    "current,tick,tick_next,price,next_price,spacing,direction,skipped",
    [
        (0, 25, 30, 100, 200, 10, False, True),
        (3, 30, 40, 200, 200, 10, True, False),
        (-2, -25, -30, 200, 100, 10, True, True),
        (7, -60, -60, 100, 100, 60, False, True),
        (-9, 61, 120, 100, 100, 60, True, True),
        (0, 0, 60, 100, 200, 60, False, False),
    ],
)
def test_canonical_index_transition_matches_historical_075(
    current, tick, tick_next, price, next_price, spacing, direction, skipped
):
    original = state_info(
        tick_spacing_index_reference=-2,
        volatility_reference=10_000,
        volatility_accumulator=1_000,
    )
    expected_info, expected_index = legacy_index_transition(
        original, current, tick, tick_next, price, next_price,
        spacing, direction, skipped,
    )
    actual = advance_dynamic_fee_index(
        RaydiumDynamicFeeSwapState(original, spacing, 1000, current),
        tick=tick,
        tick_next=tick_next,
        sqrt_price_x64=price,
        sqrt_price_next_x64=next_price,
        zero_for_one=direction,
        is_skipped_tick_spacing=skipped,
    )
    assert actual.info == expected_info
    assert actual.tick_spacing_index == expected_index


@pytest.mark.parametrize(
    "index,spacing,liquidity,direction,target_tick",
    [
        (10, 10, 1000, True, -100),
        (-2, 60, 1000, False, 0),
        (0, 10, 0, True, -50),
        (0, 10, 1000, False, 50),
    ],
)
def test_canonical_spacing_bound_matches_historical_075(
    index, spacing, liquidity, direction, target_tick
):
    original = state_info(volatility_accumulator=10_000)
    target = get_sqrt_price_at_tick(target_tick)
    expected = legacy_spacing_bound(
        target, index, spacing, liquidity, original, direction
    )
    actual = get_spacing_bounded_price(
        RaydiumDynamicFeeSwapState(original, spacing, 1000, index),
        target_price_x64=target,
        zero_for_one=direction,
        liquidity=liquidity,
    )
    assert (actual.reached_target, actual.sqrt_price_x64, actual.bounded_tick) == expected


def test_static_fee_and_max_volatility_short_circuits_match_historical_075():
    static = RaydiumClmmDynamicFeeInfo()
    assert compute_total_fee_rate(
        base_fee_rate=3000, info=static, tick_spacing=10
    ) == legacy_total_fee(3000, static, 10)

    saturated = state_info(
        max_volatility_accumulator=1000,
        volatility_accumulator=1000,
    )
    actual = get_spacing_bounded_price(
        RaydiumDynamicFeeSwapState(saturated, 10, 1000, 1),
        target_price_x64=get_sqrt_price_at_tick(50),
        zero_for_one=False,
        liquidity=100,
    )
    expected = legacy_spacing_bound(
        get_sqrt_price_at_tick(50), 1, 10, 100, saturated, False
    )
    assert (actual.reached_target, actual.sqrt_price_x64, actual.bounded_tick) == expected
