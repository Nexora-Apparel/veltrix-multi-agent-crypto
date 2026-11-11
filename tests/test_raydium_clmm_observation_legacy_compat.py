"""Differential tests for the PR #75 -> PR #80 ObservationState transition.

The legacy dataclasses and transition below are a test-only transcription of
the historical PR #75 update API. Identical verified account bytes are decoded
by the canonical decoder, mapped into the legacy state shape, and passed
through both transition implementations.
"""
from dataclasses import dataclass, replace
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_PROGRAM_ID
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR,
    RAYDIUM_OBSERVATION_RECORD_LENGTH,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    apply_raydium_observation_update,
    decode_raydium_clmm_observation_state,
    derive_raydium_clmm_observation_address,
)


@dataclass(frozen=True, slots=True)
class LegacyObservation:
    block_timestamp: int
    tick_cumulative: int
    padding: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class LegacyObservationState:
    address: str
    initialized: bool
    recent_epoch: int
    observation_index: int
    pool_id: str
    observations: tuple[LegacyObservation, ...]
    padding: tuple[int, int, int, int]
    data_sha256: str


def legacy_apply_update(state, *, block_timestamp, tick):
    """Historical PR #75 apply_raydium_observation_update transition."""
    observations = list(state.observations)
    index = state.observation_index
    if not state.initialized:
        observations[index] = LegacyObservation(
            block_timestamp, 0, (0, 0, 0, 0)
        )
        return replace(
            state, initialized=True, observations=tuple(observations)
        )

    last = observations[index]
    delta_time = max(0, block_timestamp - last.block_timestamp)
    if delta_time < 15:
        return state

    delta_cumulative = tick * delta_time
    delta_cumulative = max(
        -(1 << 63), min((1 << 63) - 1, delta_cumulative)
    )
    cumulative = (
        (last.tick_cumulative + delta_cumulative + (1 << 63)) % (1 << 64)
    ) - (1 << 63)
    next_index = 0 if index == 99 else index + 1
    observations[next_index] = LegacyObservation(
        block_timestamp, cumulative, (0, 0, 0, 0)
    )
    return replace(
        state, observation_index=next_index,
        observations=tuple(observations),
    )


def account_fixture(*, initialized, index, last_timestamp, last_cumulative):
    pool = str(Pubkey.new_unique())
    address, _ = derive_raydium_clmm_observation_address(pool)
    data = bytearray(RAYDIUM_OBSERVATION_STATE_LENGTH)
    data[:8] = RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR
    data[8] = int(initialized)
    data[9:17] = (123).to_bytes(8, "little")
    data[17:19] = index.to_bytes(2, "little")
    data[19:51] = bytes(Pubkey.from_string(pool))
    offset = 51 + index * RAYDIUM_OBSERVATION_RECORD_LENGTH
    data[offset:offset + 4] = last_timestamp.to_bytes(4, "little")
    data[offset + 4:offset + 12] = last_cumulative.to_bytes(
        8, "little", signed=True
    )
    raw = bytes(data)
    account = DexPoolAccountEvidence(
        amm_key=address,
        dex_label="Raydium CLMM Observation",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1,
        executable=False,
        data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(),
        data_bytes=raw,
    )
    return account, pool, address


def decode_canonical_and_map_legacy(account, pool, address):
    canonical = decode_raydium_clmm_observation_state(
        account, expected_address=address, expected_pool_address=pool
    )
    # Canonical records contain the same 32 zero padding bytes represented as
    # four zero u64 values in the historical PR #75 data model.
    assert canonical.padding == bytes(32)
    legacy = LegacyObservationState(
        address=canonical.address,
        initialized=canonical.initialized,
        recent_epoch=canonical.recent_epoch,
        observation_index=canonical.observation_index,
        pool_id=canonical.pool_id,
        observations=tuple(
            LegacyObservation(
                obs.block_timestamp,
                obs.tick_cumulative,
                tuple(
                    int.from_bytes(obs.padding[i:i + 8], "little")
                    for i in range(0, 32, 8)
                ),
            )
            for obs in canonical.observations
        ),
        padding=tuple(
            int.from_bytes(canonical.padding[i:i + 8], "little")
            for i in range(0, 32, 8)
        ),
        data_sha256=canonical.data_sha256,
    )
    return canonical, legacy


def assert_states_equal(canonical, legacy):
    assert canonical.address == legacy.address
    assert canonical.initialized == legacy.initialized
    assert canonical.recent_epoch == legacy.recent_epoch
    assert canonical.observation_index == legacy.observation_index
    assert canonical.pool_id == legacy.pool_id
    assert canonical.padding == bytes().join(
        value.to_bytes(8, "little") for value in legacy.padding
    )
    assert len(canonical.observations) == len(legacy.observations)
    for current, old in zip(canonical.observations, legacy.observations):
        assert current.block_timestamp == old.block_timestamp
        assert current.tick_cumulative == old.tick_cumulative
        assert current.padding == bytes().join(
            value.to_bytes(8, "little") for value in old.padding
        )


@pytest.mark.parametrize(
    "initialized,index,last_timestamp,last_cumulative,now,tick",
    [
        (False, 4, 0, 0, 1234, -17),                # first write, no advance
        (True, 5, 1000, 100, 1014, 10),             # 14-second hold
        (True, 5, 1000, 100, 1015, 10),             # exact 15-second update
        (True, 99, 1000, (1 << 63) - 1, 1015, 1),   # ring + i64 wrap
        (True, 0, 2000, 5, 1000, 100),              # timestamp regression
        (True, 0, 0, 0, (1 << 32) - 1, (1 << 31) - 1),  # i64 delta saturation
    ],
)
def test_legacy_and_canonical_observation_updates_are_identical(
    initialized, index, last_timestamp, last_cumulative, now, tick
):
    account, pool, address = account_fixture(
        initialized=initialized,
        index=index,
        last_timestamp=last_timestamp,
        last_cumulative=last_cumulative,
    )
    canonical, legacy = decode_canonical_and_map_legacy(
        account, pool, address
    )
    actual = apply_raydium_observation_update(
        canonical, block_timestamp=now, tick=tick
    )
    expected = legacy_apply_update(
        legacy, block_timestamp=now, tick=tick
    )
    assert_states_equal(actual, expected)


def test_canonical_decoder_and_legacy_layout_have_identical_ring_dimensions():
    account, pool, address = account_fixture(
        initialized=True, index=99, last_timestamp=77, last_cumulative=-9
    )
    canonical, legacy = decode_canonical_and_map_legacy(
        account, pool, address
    )
    assert len(canonical.observations) == RAYDIUM_OBSERVATION_COUNT
    assert len(legacy.observations) == RAYDIUM_OBSERVATION_COUNT
    assert canonical.observation_index == legacy.observation_index == 99
    assert canonical.observations[99].block_timestamp == 77
    assert canonical.observations[99].tick_cumulative == -9
