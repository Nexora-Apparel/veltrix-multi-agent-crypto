import base64
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.providers.solana_liquidity_reader import (
    BPF_UPGRADEABLE_LOADER, RAYDIUM_AMM_V4_PROGRAM, RAYDIUM_CPMM_PROGRAM,
    RAYDIUM_CPMM_STATE_SIZE, TOKEN_PROGRAM, UNCX_PROGRAM,
    TOKEN_LOCK_DISCRIMINATOR, SolanaRaydiumV4UncxSnapshotReader,
)


PROGRAM = Pubkey.from_string(UNCX_PROGRAM)
LP_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
AMM = Pubkey.from_string("11111111111111111111111111111111")
OWNER = Pubkey.from_string("Vote111111111111111111111111111111111111111")
LOCK_ID = 17


def reader():
    return SolanaRaydiumV4UncxSnapshotReader(
        "https://solana-pools.example", "https://solana-locks.example",
        expected_raydium_executable_hash="a" * 64,
        expected_raydium_cpmm_executable_hash="b" * 64,
        registry_complete=True,
    )


def token_lock_account():
    lock_address, bump = Pubkey.find_program_address(
        [b"uncx_locker", LOCK_ID.to_bytes(8, "little")], PROGRAM
    )
    data = bytearray(146)
    data[:8] = TOKEN_LOCK_DISCRIMINATOR
    data[8] = bump
    data[9:41] = bytes(AMM)
    data[41:73] = bytes(LP_MINT)
    struct.pack_into("<Q", data, 73, LOCK_ID)
    struct.pack_into("<q", data, 81, 1_900_000_000)
    struct.pack_into("<q", data, 89, 2_100_000_000)
    data[97] = 0
    struct.pack_into("<Q", data, 98, 900)
    struct.pack_into("<Q", data, 106, 850)
    data[114:146] = bytes(OWNER)
    return str(lock_address), bytes(data)


def account(data, owner, *, executable=False):
    return {
        "owner": owner, "executable": executable,
        "data": [base64.b64encode(data).decode(), "base64"],
    }


def test_uncx_token_lock_layout_and_pda_are_verified():
    instance = reader()
    address, data = token_lock_account()
    decoded = instance._decode_token_lock(
        address, account(data, UNCX_PROGRAM)
    )
    assert decoded["lock_id"] == LOCK_ID
    assert decoded["amm_id"] == str(AMM)
    assert decoded["lp_mint"] == str(LP_MINT)
    assert decoded["amount"] == 850
    assert decoded["unlock_date"] == 2_100_000_000


def test_uncx_lock_account_with_wrong_pda_or_program_owner_is_rejected():
    instance = reader()
    address, data = token_lock_account()
    wrong = str(Pubkey.from_bytes(bytes([9]) * 32))
    with pytest.raises(Exception, match="PDA or bump"):
        instance._decode_token_lock(wrong, account(data, UNCX_PROGRAM))
    with pytest.raises(Exception, match="not owned by the pinned program"):
        instance._decode_token_lock(address, account(data, RAYDIUM_AMM_V4_PROGRAM))


def test_uncx_vault_pda_authority_mint_and_amount_are_reconciled():
    instance = reader()
    authority, _ = Pubkey.find_program_address([b"uncx_authority"], PROGRAM)
    vault, _ = Pubkey.find_program_address(
        [b"uncx_lp_vault", bytes(AMM)], PROGRAM
    )
    data = bytearray(165)
    data[:32] = bytes(LP_MINT)
    data[32:64] = bytes(authority)
    struct.pack_into("<Q", data, 64, 850)
    data[108] = 1

    class Rpc:
        def call(self, method, params):
            raise AssertionError("direct RPC is not used by this unit test")

    instance.locker_rpc = Rpc()
    instance._get_account = lambda rpc, address, slot: account(bytes(data), TOKEN_PROGRAM)
    got_vault, got_authority, amount = instance._read_custody_vault(
        str(AMM), str(LP_MINT), 99
    )
    assert got_vault == str(vault)
    assert got_authority == str(authority)
    assert amount == 850


def test_uncx_vault_with_wrong_token_account_authority_is_rejected():
    instance = reader()
    data = bytearray(165)
    data[:32] = bytes(LP_MINT)
    data[32:64] = bytes(Pubkey.from_bytes(bytes([7]) * 32))
    struct.pack_into("<Q", data, 64, 850)
    data[108] = 1
    instance._get_account = lambda rpc, address, slot: account(bytes(data), TOKEN_PROGRAM)
    with pytest.raises(Exception, match="mint, authority or initialized"):
        instance._read_custody_vault(str(AMM), str(LP_MINT), 99)


def test_only_upgradeable_program_data_layout_is_supported():
    instance = reader()
    data = struct.pack("<I", 2) + bytes(Pubkey.from_bytes(bytes([3]) * 32))
    instance._get_account = lambda rpc, address, slot: account(
        data, BPF_UPGRADEABLE_LOADER, executable=True
    )
    with pytest.raises(Exception, match="ProgramData account layout is invalid"):
        instance._verify_uncx_program(99)


def test_cpmm_pool_state_is_discovered_by_token_mint_and_lp_offsets():
    instance = reader()
    target = str(LP_MINT)
    quote = str(OWNER)
    lp_mint = str(Pubkey.from_string("So11111111111111111111111111111111111111113"))
    pool_address = str(Pubkey.from_string("Vote111111111111111111111111111111111111111"))
    data = bytearray(RAYDIUM_CPMM_STATE_SIZE)
    data[136:168] = bytes(Pubkey.from_string(lp_mint))
    data[168:200] = bytes(LP_MINT)
    data[200:232] = bytes(OWNER)
    encoded = base64.b64encode(bytes(data)).decode()

    class Rpc:
        provider_id = "rpc"
        def call(self, method, params):
            if method == "getProgramAccounts":
                return {
                    "context": {"slot": 900},
                    "value": [{
                        "pubkey": pool_address,
                        "account": {
                            "owner": RAYDIUM_CPMM_PROGRAM,
                            "data": [encoded, "base64"],
                        },
                    }],
                }
            raise AssertionError(method)

    rows = instance._scan_cpmm_pools(Rpc(), target, 900)
    assert len(rows) == 1
    pool = rows[pool_address]
    assert pool["lp_mint"] == lp_mint
    assert pool["base_mint"] == target
    assert pool["quote_mint"] == quote
    assert pool["pool_type"] == "raydium-cpmm"


def test_cpmm_pool_discovery_rejects_rpc_inventory_disagreement():
    instance = reader()
    target = str(LP_MINT)

    class Rpc:
        def __init__(self, values):
            self.values = values
        def call(self, method, params):
            return {"context": {"slot": 900}, "value": self.values}

    instance.pool_rpc = Rpc([])
    instance.locker_rpc = Rpc([{"pubkey": "missing", "account": {}}])
    with pytest.raises(Exception, match="independent Solana RPCs disagree"):
        instance._discover_cpmm_pools(target, 900)


def test_solana_reader_requires_both_rayidium_hashes_and_complete_scope():
    with pytest.raises(ValueError, match="CPMM executable SHA-256"):
        SolanaRaydiumV4UncxSnapshotReader(
            "https://solana-pools.example", "https://solana-locks.example",
            expected_raydium_executable_hash="a" * 64,
            expected_raydium_cpmm_executable_hash="",
            registry_complete=True,
        )
    with pytest.raises(ValueError, match="registry must be explicitly certified"):
        SolanaRaydiumV4UncxSnapshotReader(
            "https://solana-pools.example", "https://solana-locks.example",
            expected_raydium_executable_hash="a" * 64,
            expected_raydium_cpmm_executable_hash="b" * 64,
            registry_complete=False,
        )
