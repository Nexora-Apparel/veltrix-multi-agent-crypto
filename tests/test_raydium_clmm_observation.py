"""Adversarial tests for pinned Raydium CLMM ObservationState decoding."""
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_PROGRAM_ID
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RAYDIUM_OBSERVATION_RECORD_LENGTH,
    RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    RaydiumClmmObservationError,
    apply_raydium_observation_update,
    decode_raydium_clmm_observation_state,
    encode_raydium_clmm_observation_state,
    derive_raydium_clmm_observation_address,
)


def fixture(*, initialized=False, observation_index=0, pool=None, padding=bytes(32)):
    pool = pool or str(Pubkey.new_unique())
    address, _ = derive_raydium_clmm_observation_address(pool)
    data = bytearray(RAYDIUM_OBSERVATION_STATE_LENGTH)
    data[:8] = RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR
    data[8] = int(initialized)
    data[9:17] = (123).to_bytes(8, "little")
    data[17:19] = observation_index.to_bytes(2, "little")
    data[19:51] = bytes(Pubkey.from_string(pool))
    if initialized:
        offset = 51 + observation_index * RAYDIUM_OBSERVATION_RECORD_LENGTH
        data[offset:offset + 4] = (1_700_000_000).to_bytes(4, "little")
        data[offset + 4:offset + 12] = (-123456).to_bytes(8, "little", signed=True)
    data[-32:] = padding
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
    return account, address, pool


def decode(account, address, pool):
    return decode_raydium_clmm_observation_state(
        account, expected_address=address, expected_pool_address=pool
    )


def test_decodes_uninitialized_observation_state_with_exact_layout():
    account, address, pool = fixture()
    result = decode(account, address, pool)
    assert len(result.observations) == RAYDIUM_OBSERVATION_COUNT
    assert result.initialized is False
    assert result.observation_index == 0
    assert result.pool_id == pool
    assert result.recent_epoch == 123


def test_decodes_initialized_observation_ring_entry():
    account, address, pool = fixture(initialized=True, observation_index=9)
    result = decode(account, address, pool)
    assert result.initialized is True
    assert result.observation_index == 9
    assert result.observations[9].block_timestamp == 1_700_000_000
    assert result.observations[9].tick_cumulative == -123456


@pytest.mark.parametrize("index", [100, 65535])
def test_rejects_observation_ring_index_out_of_bounds(index):
    account, address, pool = fixture(observation_index=index)
    with pytest.raises(RaydiumClmmObservationError, match="ring index"):
        decode(account, address, pool)


def test_rejects_wrong_pool_association_and_noncanonical_pda():
    account, address, pool = fixture()
    other_pool = str(Pubkey.new_unique())
    with pytest.raises(RaydiumClmmObservationError, match="different pool"):
        decode(account, address, other_pool)
    wrong_address = str(Pubkey.new_unique())
    with pytest.raises(RaydiumClmmObservationError, match="address"):
        decode(account, wrong_address, pool)


def test_rejects_owner_hash_and_reserved_padding_tampering():
    account, address, pool = fixture()
    bad_owner = DexPoolAccountEvidence(
        amm_key=account.amm_key, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=str(Pubkey.new_unique()), lamports=1,
        executable=False, data_length=account.data_length,
        data_sha256=account.data_sha256, data_bytes=account.data_bytes,
    )
    with pytest.raises(RaydiumClmmObservationError, match="owner"):
        decode(bad_owner, address, pool)
    bad_hash = DexPoolAccountEvidence(
        amm_key=account.amm_key, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=account.data_length,
        data_sha256="0" * 64, data_bytes=account.data_bytes,
    )
    with pytest.raises(RaydiumClmmObservationError, match="hash"):
        decode(bad_hash, address, pool)
    padded, padded_address, padded_pool = fixture(padding=bytes([1]) + bytes(31))
    with pytest.raises(RaydiumClmmObservationError, match="account padding"):
        decode(padded, padded_address, padded_pool)


def test_rejects_nonzero_observation_record_padding():
    account, address, pool = fixture(initialized=True)
    raw = bytearray(account.data_bytes)
    raw[51 + 12] = 1
    changed = DexPoolAccountEvidence(
        amm_key=account.amm_key, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(), data_bytes=bytes(raw),
    )
    with pytest.raises(RaydiumClmmObservationError, match="record 0"):
        decode(changed, address, pool)


def test_rejects_populated_records_when_observation_state_is_uninitialized():
    account, address, pool = fixture()
    raw = bytearray(account.data_bytes)
    raw[51:55] = (100).to_bytes(4, "little")
    changed = DexPoolAccountEvidence(
        amm_key=account.amm_key, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(), data_bytes=bytes(raw),
    )
    with pytest.raises(RaydiumClmmObservationError, match="uninitialized"):
        decode(changed, address, pool)


def test_first_observation_write_initializes_current_slot_without_advancing():
    account, address, pool = fixture(observation_index=4)
    state = decode(account, address, pool)
    updated = apply_raydium_observation_update(
        state, block_timestamp=1_700_000_100, tick=-17
    )
    assert updated.initialized is True
    assert updated.observation_index == 4
    assert updated.observations[4].block_timestamp == 1_700_000_100
    assert updated.observations[4].tick_cumulative == 0


def test_observation_update_respects_15_second_boundary():
    account, address, pool = fixture(initialized=True, observation_index=5)
    state = decode(account, address, pool)
    assert apply_raydium_observation_update(
        state, block_timestamp=1_700_000_014, tick=10
    ) == state
    updated = apply_raydium_observation_update(
        state, block_timestamp=1_700_000_015, tick=10
    )
    assert updated.observation_index == 6
    assert updated.observations[6].block_timestamp == 1_700_000_015
    assert updated.observations[6].tick_cumulative == -123306


def test_observation_ring_wraps_and_i64_cumulative_uses_wrapping_addition():
    account, address, pool = fixture(initialized=True, observation_index=99)
    raw = bytearray(account.data_bytes)
    offset = 51 + 99 * RAYDIUM_OBSERVATION_RECORD_LENGTH
    raw[offset + 4:offset + 12] = ((1 << 63) - 1).to_bytes(8, "little", signed=True)
    updated_account = DexPoolAccountEvidence(
        amm_key=account.amm_key, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(), data_bytes=bytes(raw),
    )
    state = decode(updated_account, address, pool)
    updated = apply_raydium_observation_update(
        state, block_timestamp=1_700_000_015, tick=1
    )
    assert updated.observation_index == 0
    assert updated.observations[0].block_timestamp == 1_700_000_015
    assert updated.observations[0].tick_cumulative == -(1 << 63) + 14


def test_timestamp_regression_saturates_elapsed_time_to_zero():
    account, address, pool = fixture(initialized=True)
    state = decode(account, address, pool)
    assert apply_raydium_observation_update(
        state, block_timestamp=1_699_999_999, tick=100
    ) == state


@pytest.mark.parametrize("timestamp,tick", [
    (-1, 0), (1 << 32, 0), (1, -(1 << 31) - 1), (1, 1 << 31),
])
def test_observation_update_rejects_values_outside_source_integer_domains(timestamp, tick):
    account, address, pool = fixture(initialized=True)
    state = decode(account, address, pool)
    with pytest.raises(RaydiumClmmObservationError):
        apply_raydium_observation_update(
            state, block_timestamp=timestamp, tick=tick
        )


def test_observation_state_serializer_roundtrips_exact_account_bytes():
    account, address, pool = fixture(initialized=True, observation_index=9)
    state = decode(account, address, pool)
    assert encode_raydium_clmm_observation_state(state) == account.data_bytes


def test_observation_mutations_return_hash_of_exact_serialized_account():
    account, address, pool = fixture(observation_index=4)
    state = decode(account, address, pool)
    updated = apply_raydium_observation_update(
        state, block_timestamp=1_700_000_100, tick=-17
    )
    encoded = encode_raydium_clmm_observation_state(updated)
    assert updated.data_sha256 == hashlib.sha256(encoded).hexdigest()
    assert len(encoded) == RAYDIUM_OBSERVATION_STATE_LENGTH
    assert encode_raydium_clmm_observation_state(
        decode(
            DexPoolAccountEvidence(
                amm_key=address,
                dex_label="Raydium CLMM Observation",
                expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
                owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
                lamports=1,
                executable=False,
                data_length=len(encoded),
                data_sha256=hashlib.sha256(encoded).hexdigest(),
                data_bytes=encoded,
            ),
            address,
            pool,
        )
    ) == encoded
