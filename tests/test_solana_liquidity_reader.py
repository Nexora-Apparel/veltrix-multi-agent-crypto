import base64
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.providers.solana_liquidity_reader import (
    BPF_UPGRADEABLE_LOADER, RAYDIUM_AMM_V4_PROGRAM, RAYDIUM_CPMM_PROGRAM,
    RAYDIUM_CPMM_STATE_SIZE, RAYDIUM_CPMM_POOL_DISCRIMINATOR, TOKEN_PROGRAM, UNCX_PROGRAM,
    TOKEN_LOCK_DISCRIMINATOR, SolanaRaydiumV4CpmmSnapshotReader,
)


PROGRAM = Pubkey.from_string(UNCX_PROGRAM)
LP_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
AMM = Pubkey.from_string("11111111111111111111111111111111")
OWNER = Pubkey.from_string("Vote111111111111111111111111111111111111111")
LOCK_ID = 17


def reader():
    return SolanaRaydiumV4CpmmSnapshotReader(
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


def cpmm_pool_row():
    instance_program = Pubkey.from_string(RAYDIUM_CPMM_PROGRAM)
    pool_key = Pubkey.from_bytes(bytes([8]) * 32)
    token0_key, token1_key = sorted((LP_MINT, OWNER), key=bytes)
    lp_mint_key, _ = Pubkey.find_program_address(
        [b"pool_lp_mint", bytes(pool_key)], instance_program
    )
    vault0, _ = Pubkey.find_program_address(
        [b"pool_vault", bytes(pool_key), bytes(token0_key)], instance_program
    )
    vault1, _ = Pubkey.find_program_address(
        [b"pool_vault", bytes(pool_key), bytes(token1_key)], instance_program
    )
    data = bytearray(RAYDIUM_CPMM_STATE_SIZE)
    data[:8] = RAYDIUM_CPMM_POOL_DISCRIMINATOR
    data[72:104] = bytes(vault0)
    data[104:136] = bytes(vault1)
    data[136:168] = bytes(lp_mint_key)
    data[168:200] = bytes(token0_key)
    data[200:232] = bytes(token1_key)
    encoded = base64.b64encode(bytes(data)).decode()
    return {
        "pubkey": str(pool_key),
        "account": {"owner": RAYDIUM_CPMM_PROGRAM, "data": [encoded, "base64"]},
    }, str(lp_mint_key), str(token0_key), str(token1_key)


def test_cpmm_pool_state_is_discovered_by_token_mint_and_lp_offsets():
    instance = reader()
    row, lp_mint, token0, token1 = cpmm_pool_row()
    target = str(LP_MINT)

    class Rpc:
        provider_id = "rpc"
        def call(self, method, params):
            if method == "getProgramAccounts":
                return {"context": {"slot": 900}, "value": [row]}
            raise AssertionError(method)

    rows = instance._scan_cpmm_pools(Rpc(), target, 900)
    assert len(rows) == 1
    pool = rows[row["pubkey"]]
    assert pool["lp_mint"] == lp_mint
    assert {pool["base_mint"], pool["quote_mint"]} == {token0, token1}
    assert target in {token0, token1}
    assert pool["pool_type"] == "raydium-cpmm"


def test_cpmm_pool_discovery_rejects_rpc_inventory_disagreement():
    instance = reader()
    row, _lp_mint, _token0, _token1 = cpmm_pool_row()

    class Rpc:
        def __init__(self, values):
            self.values = values
        def call(self, method, params):
            return {"context": {"slot": 900}, "value": self.values}

    instance.pool_rpc = Rpc([])
    instance.locker_rpc = Rpc([row])
    with pytest.raises(Exception, match="independent Solana RPCs disagree"):
        instance._discover_cpmm_pools(str(LP_MINT), 900)


def test_solana_reader_requires_both_rayidium_hashes_and_complete_scope():
    with pytest.raises(ValueError, match="CPMM executable SHA-256"):
        SolanaRaydiumV4CpmmSnapshotReader(
            "https://solana-pools.example", "https://solana-locks.example",
            expected_raydium_executable_hash="a" * 64,
            expected_raydium_cpmm_executable_hash="",
            registry_complete=True,
        )
    with pytest.raises(ValueError, match="registry must be explicitly certified"):
        SolanaRaydiumV4CpmmSnapshotReader(
            "https://solana-pools.example", "https://solana-locks.example",
            expected_raydium_executable_hash="a" * 64,
            expected_raydium_cpmm_executable_hash="b" * 64,
            registry_complete=False,
        )
