"""Differential regression tests for the historical Phase 09.35 dynamic-fee API.

The oracle functions below preserve the historical public-call semantics from
PR #75. They intentionally live only in tests: production code remains the
canonical Phase 09.35 state-object API. These tests compare valid, reachable
inputs and do not claim full swap-loop or Rust/on-chain parity.
"""
from dataclasses import replace

import pytest

from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick
from veltrix.raydium_clmm_dynamic_fee import (
    MAX_FEE_RATE_NUMERATOR,
    RaydiumDynamicFeeSwapState,
    advance_dynamic_fee_index,
    compute_total_fee_rate,
    get_spacing_bounded_price,
    tick_spacing_index_from_tick,
    update_volatility_accumulator,
)


def make_info(**overrides):
    values = dict(
        filter_period=60,
        decay_period=3600,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=1_000_000,
        tick_spacing_index_reference=0,
        volatility_reference=10_000,
        volatility_accumulator=20_000,
        last_update_timestamp=1000,
        padding=bytes(46),
    )
    values.update(overrides)
    return RaydiumClmmDynamicFeeInfo(**values)


def legacy_update_index(
    info, *, current_index, tick, tick_next, sqrt_price_x64,
    sqrt_price_next_x64, tick_spacing, zero_for_one,
    is_skipped_tick_spacing,
):
    """Frozen PR #75 transition, retained as a test oracle."""
    next_info = info
    next_index = current_index
    if is_skipped_tick_spacing:
        tick_index = tick_next if sqrt_price_x64 == sqrt_price_next_x64 else tick
        next_index = tick_spacing_index_from_tick(tick_index, tick_spacing)
        if not zero_for_one and tick_index % tick_spacing == 0:
            next_index -= 1
        if info.volatility_accumulator != info.max_volatility_accumulator:
            next_info = update_volatility_accumulator(
                info, tick_spacing_index=next_index
            )
    next_index += -1 if zero_for_one else 1
    return next_info, next_index


def legacy_total_fee(base_fee_rate, *, dynamic_fee_info, tick_spacing):
    """Historical get_total_fee_rate behavior, using canonical fee arithmetic."""
    if not dynamic_fee_info.enabled:
        return base_fee_rate
    from veltrix.raydium_clmm_dynamic_fee import compute_dynamic_fee_rate
    return min(
        base_fee_rate + compute_dynamic_fee_rate(
            dynamic_fee_info, tick_spacing=tick_spacing
        ),
        MAX_FEE_RATE_NUMERATOR,
    )


def legacy_spacing_bound(
    *, target_price_x64, tick_spacing_index, tick_spacing, liquidity,
    dynamic_fee_info, zero_for_one,
):
    """Historical get_spacing_bounded_price return semantics."""
    if (
        not dynamic_fee_info.enabled
        or liquidity == 0
        or dynamic_fee_info.volatility_accumulator
        == dynamic_fee_info.max_volatility_accumulator
    ):
        return True, target_price_x64, None
    bounded_tick = (
        tick_spacing_index * tick_spacing
        if zero_for_one
        else (tick_spacing_index + 1) * tick_spacing
    )
    from veltrix.raydium_clmm_tick_math import RAYDIUM_MIN_TICK, RAYDIUM_MAX_TICK
    bounded_tick = max(RAYDIUM_MIN_TICK, min(RAYDIUM_MAX_TICK, bounded_tick))
    bounded_price = get_sqrt_price_at_tick(bounded_tick)
    if zero_for_one and target_price_x64 > bounded_price:
        return False, target_price_x64, None
    if not zero_for_one and target_price_x64 < bounded_price:
        return False, target_price_x64, None
    return False, bounded_price, bounded_tick


@pytest.mark.parametrize(
    "zero_for_one,skipped,tick,tick_next,sqrt_a,sqrt_b",
    [
        (True, True, 25, 30, 100, 200),
        (False, True, 25, 30, 100, 200),
        (True, True, 25, 30, 200, 200),
        (False, True, 25, 30, 200, 200),
        (True, False, 25, 30, 100, 200),
        (False, False, 25, 30, 100, 200),
        (True, True, -25, -30, 100, 200),
        (False, True, -25, -30, 200, 200),
    ],
)
def test_legacy_index_transition_matches_canonical_state_transition(
    zero_for_one, skipped, tick, tick_next, sqrt_a, sqrt_b
):
    info = make_info()
    current_index = -2
    legacy_info, legacy_index = legacy_update_index(
        info,
        current_index=current_index,
        tick=tick,
        tick_next=tick_next,
        sqrt_price_x64=sqrt_a,
        sqrt_price_next_x64=sqrt_b,
        tick_spacing=10,
        zero_for_one=zero_for_one,
        is_skipped_tick_spacing=skipped,
    )
    canonical = advance_dynamic_fee_index(
        RaydiumDynamicFeeSwapState(info, 10, 1000, current_index),
        tick=tick,
        tick_next=tick_next,
        sqrt_price_x64=sqrt_a,
        sqrt_price_next_x64=sqrt_b,
        zero_for_one=zero_for_one,
        is_skipped_tick_spacing=skipped,
    )
    assert canonical.info == legacy_info
    assert canonical.tick_spacing_index == legacy_index


@pytest.mark.parametrize(
    "base_fee,accumulator,spacing",
    [(0, 0, 1), (1000, 10_000, 60), (99_000, 100_000, 10)],
)
def test_legacy_total_fee_matches_canonical_total_fee(base_fee, accumulator, spacing):
    info = make_info(
        volatility_accumulator=accumulator,
        max_volatility_accumulator=max(1_000_000, accumulator),
    )
    assert compute_total_fee_rate(
        base_fee_rate=base_fee, info=info, tick_spacing=spacing
    ) == legacy_total_fee(
        base_fee, dynamic_fee_info=info, tick_spacing=spacing
    )


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_legacy_spacing_bound_matches_canonical_spacing_bound(zero_for_one):
    info = make_info()
    state = RaydiumDynamicFeeSwapState(info, 10, 1000, 10)
    target_tick = -100 if zero_for_one else 200
    target = get_sqrt_price_at_tick(target_tick)
    expected = legacy_spacing_bound(
        target_price_x64=target,
        tick_spacing_index=10,
        tick_spacing=10,
        liquidity=1000,
        dynamic_fee_info=info,
        zero_for_one=zero_for_one,
    )
    actual = get_spacing_bounded_price(
        state,
        target_price_x64=target,
        zero_for_one=zero_for_one,
        liquidity=1000,
    )
    assert (actual.reached_target, actual.sqrt_price_x64, actual.bounded_tick) == expected


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_legacy_spacing_bound_target_reached_cases_match(zero_for_one):
    info = make_info(
        max_volatility_accumulator=20_000,
        volatility_accumulator=20_000,
    )
    target = get_sqrt_price_at_tick(50)
    state = RaydiumDynamicFeeSwapState(info, 10, 1000, 1)
    expected = legacy_spacing_bound(
        target_price_x64=target,
        tick_spacing_index=1,
        tick_spacing=10,
        liquidity=1000,
        dynamic_fee_info=info,
        zero_for_one=zero_for_one,
    )
    actual = get_spacing_bounded_price(
        state, target_price_x64=target,
        zero_for_one=zero_for_one, liquidity=1000,
    )
    assert (actual.reached_target, actual.sqrt_price_x64, actual.bounded_tick) == expected
