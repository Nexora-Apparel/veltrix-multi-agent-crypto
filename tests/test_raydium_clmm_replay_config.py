"""Tests for pinned Raydium CLMM AMM config evidence."""
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_PROGRAM_ID
from veltrix.raydium_clmm_replay_config import (
    RAYDIUM_AMM_CONFIG_DISCRIMINATOR,
    RAYDIUM_AMM_CONFIG_LENGTH,
    RaydiumClmmReplayConfigError,
    decode_raydium_clmm_amm_config,
    derive_raydium_clmm_amm_config_address,
)


def fixture(*, index=7, tick_spacing=10, protocol_fee=1000, trade_fee=3000, fund_fee=2000,
            padding_u32=0, padding=bytes(24), wrong_bump=False):
    address, bump = derive_raydium_clmm_amm_config_address(index)
    owner = str(Pubkey.new_unique())
    fund_owner = str(Pubkey.new_unique())
    data = bytearray(RAYDIUM_AMM_CONFIG_LENGTH)
    data[:8] = RAYDIUM_AMM_CONFIG_DISCRIMINATOR
    data[8] = (bump + 1) % 256 if wrong_bump else bump
    data[9:11] = index.to_bytes(2, "little")
    data[11:43] = bytes(Pubkey.from_string(owner))
    data[43:47] = protocol_fee.to_bytes(4, "little")
    data[47:51] = trade_fee.to_bytes(4, "little")
    data[51:53] = tick_spacing.to_bytes(2, "little")
    data[53:57] = fund_fee.to_bytes(4, "little")
    data[57:61] = padding_u32.to_bytes(4, "little")
    data[61:93] = bytes(Pubkey.from_string(fund_owner))
    data[93:117] = padding
    raw = bytes(data)
    account = DexPoolAccountEvidence(
        amm_key=address,
        dex_label="Raydium CLMM AMM Config",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1,
        executable=False,
        data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(),
        data_bytes=raw,
    )
    return account, address


def test_decodes_and_rederives_pinned_amm_config_pda_and_fee_rates():
    account, address = fixture()
    result = decode_raydium_clmm_amm_config(
        account, expected_address=address, expected_tick_spacing=10
    )
    assert result.address == address
    assert result.index == 7
    assert result.tick_spacing == 10
    assert result.trade_fee_rate == 3000
    assert result.protocol_fee_rate == 1000
    assert result.fund_fee_rate == 2000


def test_rejects_amm_config_tick_spacing_mismatch():
    account, address = fixture(tick_spacing=60)
    with pytest.raises(RaydiumClmmReplayConfigError, match="tick spacing"):
        decode_raydium_clmm_amm_config(
            account, expected_address=address, expected_tick_spacing=10
        )


@pytest.mark.parametrize("kwargs", [
    {"trade_fee": 1_000_000},
    {"protocol_fee": 1_000_001},
    {"fund_fee": 1_000_001},
    {"protocol_fee": 700_000, "fund_fee": 400_000},
    {"padding_u32": 1},
    {"padding": bytes([1]) + bytes(23)},
    {"wrong_bump": True},
])
def test_rejects_invalid_amm_config_fee_or_reserved_state(kwargs):
    account, address = fixture(**kwargs)
    with pytest.raises(RaydiumClmmReplayConfigError):
        decode_raydium_clmm_amm_config(
            account, expected_address=address, expected_tick_spacing=10
        )


def test_rejects_amm_config_owner_and_account_hash_tampering():
    account, address = fixture()
    bad_owner = DexPoolAccountEvidence(
        amm_key=account.amm_key, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=str(Pubkey.new_unique()), lamports=1,
        executable=False, data_length=account.data_length,
        data_sha256=account.data_sha256, data_bytes=account.data_bytes,
    )
    with pytest.raises(RaydiumClmmReplayConfigError, match="owner"):
        decode_raydium_clmm_amm_config(
            bad_owner, expected_address=address, expected_tick_spacing=10
        )
    bad_hash = DexPoolAccountEvidence(
        amm_key=account.amm_key, dex_label=account.dex_label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=account.data_length,
        data_sha256="0" * 64, data_bytes=account.data_bytes,
    )
    with pytest.raises(RaydiumClmmReplayConfigError, match="hash"):
        decode_raydium_clmm_amm_config(
            bad_hash, expected_address=address, expected_tick_spacing=10
        )
