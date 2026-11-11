"""Differential regression vectors for canonical Raydium ObservationState updates.

The reference transition preserves the historical Phase 09.35 implementation
from PR #75. It is intentionally test-only; production uses the canonical
ObservationState module.
"""
from dataclasses import replace

import pytest

from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    apply_raydium_observation_update,
)


I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1


def observation_state(*, initialized, index, timestamp=0, cumulative=0):
    observations = [RaydiumClmmObservation(0, 0, bytes(32))
                    for _ in range(RAYDIUM_OBSERVATION_COUNT)]
    observations[index] = RaydiumClmmObservation(timestamp, cumulative, bytes(32))
    return RaydiumClmmObservationState(
        address="observation-test-pda",
        initialized=initialized,
        recent_epoch=1,
        observation_index=index,
        pool_id="pool-test",
        observations=tuple(observations),
        padding=bytes(32),
        data_sha256="0" * 64,
    )


# Frozen transition from PR #75, commit 9ccdc1ab91c255e2a110c73dc1b5d407a31bc182.
def legacy_observation_update(state, timestamp, tick):
    records = list(state.observations)
    index = state.observation_index
    if not state.initialized:
        old = records[index]
        records[index] = replace(
            old, block_timestamp=timestamp, tick_cumulative=0
        )
        return replace(state, initialized=True, observations=tuple(records))

    last = records[index]
    elapsed = max(0, timestamp - last.block_timestamp)
    if elapsed < 15:
        return state

    delta = max(I64_MIN, min(I64_MAX, tick * elapsed))
    cumulative = (
        (last.tick_cumulative + delta - I64_MIN) % (1 << 64)
    ) + I64_MIN
    next_index = 0 if index == RAYDIUM_OBSERVATION_COUNT - 1 else index + 1
    records[next_index] = replace(
        records[next_index],
        block_timestamp=timestamp,
        tick_cumulative=cumulative,
    )
    return replace(
        state, observation_index=next_index, observations=tuple(records)
    )


def comparable(state):
    return (
        state.initialized,
        state.observation_index,
        tuple(
            (item.block_timestamp, item.tick_cumulative, item.padding)
            for item in state.observations
        ),
    )


@pytest.mark.parametrize("index", [0, 1, 49, 98, 99])
def test_first_write_matches_historical_initialization_without_ring_advance(index):
    original = observation_state(initialized=False, index=index)
    expected = legacy_observation_update(original, 1_700_000_100, -17)
    actual = apply_raydium_observation_update(
        original, block_timestamp=1_700_000_100, tick=-17
    )
    assert comparable(actual) == comparable(expected)


@pytest.mark.parametrize("elapsed", [0, 1, 14, 15, 16, 29, 30, 31])
def test_cadence_and_decay_boundary_match_historical_transition(elapsed):
    original = observation_state(
        initialized=True, index=5, timestamp=1_700_000_000,
        cumulative=-123_456,
    )
    timestamp = 1_700_000_000 + elapsed
    expected = legacy_observation_update(original, timestamp, -37)
    actual = apply_raydium_observation_update(
        original, block_timestamp=timestamp, tick=-37
    )
    assert comparable(actual) == comparable(expected)


@pytest.mark.parametrize(
    "index,timestamp,tick,cumulative",
    [
        (0, 1000, 1, 0),
        (1, 1000, -1, 25),
        (50, 1000, 17, -500),
        (98, 1000, -17, 500),
        (99, 1000, 1, I64_MAX),
    ],
)
def test_ring_position_and_signed_cumulative_match_historical(
    index, timestamp, tick, cumulative
):
    original = observation_state(
        initialized=True, index=index, timestamp=timestamp,
        cumulative=cumulative,
    )
    expected = legacy_observation_update(original, timestamp + 15, tick)
    actual = apply_raydium_observation_update(
        original, block_timestamp=timestamp + 15, tick=tick
    )
    assert comparable(actual) == comparable(expected)


@pytest.mark.parametrize(
    "tick,elapsed,initial",
    [
        (1, 0xFFFFFFFF, 0),
        (-1, 0xFFFFFFFF, 0),
        ((1 << 31) - 1, 0xFFFFFFFF, 0),
        (-(1 << 31), 0xFFFFFFFF, 0),
        (1, 15, I64_MAX),
        (-1, 15, I64_MIN),
    ],
)
def test_saturating_multiply_and_wrapping_add_match_historical(
    tick, elapsed, initial
):
    original = observation_state(
        initialized=True, index=99, timestamp=0, cumulative=initial
    )
    expected = legacy_observation_update(original, elapsed, tick)
    actual = apply_raydium_observation_update(
        original, block_timestamp=elapsed, tick=tick
    )
    assert comparable(actual) == comparable(expected)


def test_timestamp_regression_is_a_noop_in_both_implementations():
    original = observation_state(
        initialized=True, index=17, timestamp=2000, cumulative=-9
    )
    expected = legacy_observation_update(original, 1000, 500)
    actual = apply_raydium_observation_update(
        original, block_timestamp=1000, tick=500
    )
    assert comparable(actual) == comparable(expected)


def test_every_untouched_ring_slot_is_preserved():
    original = observation_state(
        initialized=True, index=99, timestamp=1000, cumulative=7
    )
    actual = apply_raydium_observation_update(
        original, block_timestamp=1015, tick=2
    )
    for index, (before, after) in enumerate(zip(
        original.observations, actual.observations
    )):
        if index != 0:
            assert after == before
