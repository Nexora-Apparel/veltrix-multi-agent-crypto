"""Tests for the pinned Raydium ObservationState account and update behavior."""
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_PROGRAM_ID
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RAYDIUM_OBSERVATION_DISCRIMINATOR,
    RAYDIUM_OBSERVATION_RECORD_LENGTH,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    RaydiumObservationError,
    apply_raydium_observation_update,
    decode_raydium_observation_state,
    derive_raydium_observation_address,
)


def fixture(*, initialized=False, index=0, last_timestamp=0, last_cumulative=0):
    pool = str(Pubkey.new_unique())
    address, _ = derive_raydium_observation_address(pool)
    data = bytearray(RAYDIUM_OBSERVATION_STATE_LENGTH)
    data[:8] = RAYDIUM_OBSERVATION_DISCRIMINATOR
    data[8] = int(initialized)
    data[9:17] = (123).to_bytes(8, "little")
    data[17:19] = index.to_bytes(2, "little")
    data[19:51] = bytes(Pubkey.from_string(pool))
    offset = 51 + index * RAYDIUM_OBSERVATION_RECORD_LENGTH
    data[offset:offset + 4] = last_timestamp.to_bytes(4, "little")
    data[offset + 4:offset + 12] = last_cumulative.to_bytes(8, "little", signed=True)
    raw = bytes(data)
    account = DexPoolAccountEvidence(
        amm_key=address, dex_label="Raydium CLMM Observation",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1, executable=False, data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(), data_bytes=raw,
    )
    return account, pool, address


def decode(values):
    account, pool, address = values
    return decode_raydium_observation_state(
        account, expected_pool_address=pool, expected_address=address
    )


def test_decodes_exact_pda_and_100_slot_observation_ring():
    values = fixture(initialized=True, index=17, last_timestamp=1000, last_cumulative=-50)
    state = decode(values)
    assert state.initialized is True
    assert state.observation_index == 17
    assert state.pool_id == values[1]
    assert len(state.observations) == RAYDIUM_OBSERVATION_COUNT
    assert state.observations[17].block_timestamp == 1000
    assert state.observations[17].tick_cumulative == -50


def test_rejects_wrong_pool_pda_discriminator_length_and_owner():
    account, pool, address = fixture()
    with pytest.raises(RaydiumObservationError, match="canonical PDA"):
        decode_raydium_observation_state(
            account, expected_pool_address=str(Pubkey.new_unique()), expected_address=address
        )
    wrong_data = bytearray(account.data_bytes)
    wrong_data[:8] = bytes(8)
    wrong_raw = bytes(wrong_data)
    wrong_discriminator = DexPoolAccountEvidence(
        amm_key=address, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=len(wrong_raw),
        data_sha256=hashlib.sha256(wrong_raw).hexdigest(), data_bytes=wrong_raw,
    )
    with pytest.raises(RaydiumObservationError, match="discriminator"):
        decode_raydium_observation_state(
            wrong_discriminator, expected_pool_address=pool, expected_address=address
        )
    wrong_owner = DexPoolAccountEvidence(
        amm_key=address, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=str(Pubkey.new_unique()), lamports=1,
        executable=False, data_length=account.data_length,
        data_sha256=account.data_sha256, data_bytes=account.data_bytes,
    )
    with pytest.raises(RaydiumObservationError, match="owner"):
        decode_raydium_observation_state(
            wrong_owner, expected_pool_address=pool, expected_address=address
        )


def test_rejects_invalid_initialized_flag_index_and_reserved_padding():
    account, pool, address = fixture()
    for offset, payload, message in [
        (8, b"\x02", "initialized flag"),
        (17, (100).to_bytes(2, "little"), "outside the 100-slot"),
        (51 + 12, (1).to_bytes(8, "little"), "Observation record"),
        (RAYDIUM_OBSERVATION_STATE_LENGTH - 8, (1).to_bytes(8, "little"), "reserved padding"),
    ]:
        data = bytearray(account.data_bytes)
        data[offset:offset + len(payload)] = payload
        raw = bytes(data)
        changed = DexPoolAccountEvidence(
            amm_key=address, dex_label=account.dex_label,
            expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data_length=len(raw),
            data_sha256=hashlib.sha256(raw).hexdigest(), data_bytes=raw,
        )
        with pytest.raises(RaydiumObservationError, match=message):
            decode_raydium_observation_state(
                changed, expected_pool_address=pool, expected_address=address
            )


def test_first_update_initializes_current_observation_without_advancing_index():
    state = decode(fixture(initialized=False, index=4))
    updated = apply_raydium_observation_update(state, block_timestamp=1234, tick=-17)
    assert updated.initialized is True
    assert updated.observation_index == 4
    assert updated.observations[4].block_timestamp == 1234
    assert updated.observations[4].tick_cumulative == 0


def test_observation_update_waits_15_seconds_then_advances_and_accumulates_tick():
    state = decode(fixture(initialized=True, index=5, last_timestamp=1000, last_cumulative=100))
    assert apply_raydium_observation_update(
        state, block_timestamp=1014, tick=10
    ) == state
    updated = apply_raydium_observation_update(
        state, block_timestamp=1015, tick=10
    )
    assert updated.observation_index == 6
    assert updated.observations[6].block_timestamp == 1015
    assert updated.observations[6].tick_cumulative == 250


def test_observation_ring_wraps_from_slot_99_to_slot_0_and_cumulative_wraps_i64():
    state = decode(fixture(
        initialized=True, index=99, last_timestamp=1000,
        last_cumulative=(1 << 63) - 1,
    ))
    updated = apply_raydium_observation_update(
        state, block_timestamp=1015, tick=1
    )
    assert updated.observation_index == 0
    assert updated.observations[0].block_timestamp == 1015
    assert updated.observations[0].tick_cumulative == -(1 << 63) + 14


def test_observation_timestamp_saturating_sub_does_not_advance_on_older_time():
    state = decode(fixture(initialized=True, index=0, last_timestamp=2000, last_cumulative=5))
    assert apply_raydium_observation_update(
        state, block_timestamp=1000, tick=100
    ) == state
