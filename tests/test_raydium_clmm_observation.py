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
    decode_raydium_clmm_observation_state,
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
