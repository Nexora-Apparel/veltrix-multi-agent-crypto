"""Adversarial tests for source-pinned Raydium auxiliary account layouts."""
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_aux_accounts import (
    AMM_CONFIG_DISCRIMINATOR,
    OBSERVATION_STATE_DISCRIMINATOR,
    AMM_CONFIG_DATA_LENGTH,
    OBSERVATION_STATE_DATA_LENGTH,
    RaydiumClmmAuxAccountError,
    RaydiumClmmAuxAccountPolicy,
    decode_raydium_clmm_amm_config,
    decode_raydium_clmm_observation_state,
    derive_raydium_clmm_amm_config_address,
    derive_raydium_clmm_observation_address,
)
from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_PROGRAM_ID


def config_fixture(index=7, tick_spacing=10):
    address, bump = derive_raydium_clmm_amm_config_address(index=index)
    owner = bytes(Pubkey.new_unique())
    fund_owner = bytes(Pubkey.new_unique())
    data = (
        AMM_CONFIG_DISCRIMINATOR
        + bytes((bump,))
        + index.to_bytes(2, "little")
        + owner
        + (1_000).to_bytes(4, "little")
        + (3_000).to_bytes(4, "little")
        + tick_spacing.to_bytes(2, "little")
        + (1_000).to_bytes(4, "little")
        + bytes(4)
        + fund_owner
        + bytes(24)
    )
    assert len(data) == AMM_CONFIG_DATA_LENGTH
    return address, data


def observation_fixture(*, now_ms=1_700_000_000_000, age=10, pool=None, index=4):
    pool = pool or str(Pubkey.new_unique())
    address = derive_raydium_clmm_observation_address(pool_address=pool)
    data = bytearray(OBSERVATION_STATE_DATA_LENGTH)
    data[:8] = OBSERVATION_STATE_DISCRIMINATOR
    data[8] = 1
    data[9:17] = (123).to_bytes(8, "little")
    data[17:19] = index.to_bytes(2, "little")
    data[19:51] = bytes(Pubkey.from_string(pool))
    timestamp = ((now_ms // 1000) - age) & 0xFFFF_FFFF
    offset = 51 + index * 44
    data[offset:offset + 4] = timestamp.to_bytes(4, "little")
    data[offset + 4:offset + 12] = (-1234).to_bytes(8, "little", signed=True)
    return address, pool, bytes(data)


def test_decodes_canonical_amm_config_and_fee_parameters():
    address, data = config_fixture()
    result = decode_raydium_clmm_amm_config(
        address=address, owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1, executable=False, data=data,
    )
    assert result.address == address
    assert result.index == 7
    assert result.tick_spacing == 10
    assert result.trade_fee_rate == 3_000


@pytest.mark.parametrize("mutator,match", [
    (lambda data: bytes(8) + data[8:], "discriminator"),
    (lambda data: data[:-1], "117-byte layout"),
    (lambda data: data[:47] + (1_000_001).to_bytes(4, "little") + data[51:], "trade fee rate"),
    (lambda data: data[:53] + (1_000_001).to_bytes(4, "little") + data[57:], "fund plus protocol fee"),
])
def test_rejects_invalid_amm_config_layout_and_fee_policy(mutator, match):
    address, data = config_fixture()
    with pytest.raises(RaydiumClmmAuxAccountError, match=match):
        decode_raydium_clmm_amm_config(
            address=address, owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            lamports=1, executable=False, data=mutator(data),
        )


def test_rejects_amm_config_wrong_owner_and_wrong_pda():
    address, data = config_fixture()
    with pytest.raises(RaydiumClmmAuxAccountError, match="owner"):
        decode_raydium_clmm_amm_config(
            address=address, owner_program_id=str(Pubkey.new_unique()),
            lamports=1, executable=False, data=data,
        )
    with pytest.raises(RaydiumClmmAuxAccountError, match="canonical index PDA"):
        decode_raydium_clmm_amm_config(
            address=str(Pubkey.new_unique()), owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            lamports=1, executable=False, data=data,
        )


def test_decodes_pool_bound_fresh_observation_state():
    now_ms = 1_700_000_000_000
    address, pool, data = observation_fixture(now_ms=now_ms, age=15)
    result = decode_raydium_clmm_observation_state(
        address=address, expected_pool_address=pool,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data=data, observed_at_epoch_ms=now_ms,
    )
    assert result.pool_address == pool
    assert result.observation_index == 4
    assert result.latest_tick_cumulative == -1234


def test_rejects_uninitialized_stale_future_or_wrong_pool_observation():
    now_ms = 1_700_000_000_000
    address, pool, data = observation_fixture(now_ms=now_ms, age=90_000)
    with pytest.raises(RaydiumClmmAuxAccountError, match="stale"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms,
        )
    address, pool, data = observation_fixture(now_ms=now_ms, age=0)
    with pytest.raises(RaydiumClmmAuxAccountError, match="future"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms - 10_000,
        )
    wrong_pool = str(Pubkey.new_unique())
    with pytest.raises(RaydiumClmmAuxAccountError, match="different pool"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=wrong_pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms,
        )
    uninitialized = bytearray(data)
    uninitialized[8] = 0
    with pytest.raises(RaydiumClmmAuxAccountError, match="not initialized"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=bytes(uninitialized), observed_at_epoch_ms=now_ms,
        )


def test_rejects_invalid_observation_ring_index_and_wrong_owner():
    now_ms = 1_700_000_000_000
    address, pool, data = observation_fixture(now_ms=now_ms)
    invalid_index = bytearray(data)
    invalid_index[17:19] = (100).to_bytes(2, "little")
    with pytest.raises(RaydiumClmmAuxAccountError, match="ring index"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=bytes(invalid_index), observed_at_epoch_ms=now_ms,
        )
    with pytest.raises(RaydiumClmmAuxAccountError, match="owner"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=str(Pubkey.new_unique()), lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms,
        )
