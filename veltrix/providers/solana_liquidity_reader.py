"""Finalized read-only Raydium V4/CPMM inventory and UNCX V4 locker reader.

Pool state and locker state are read through separate RPC endpoints. The
reader requires the same finalized slot across all account queries, verifies
deployed ELF hashes, decodes Anchor TokenLock PDAs, derives UNCX custody/vault
PDAs, and reconciles token-account balances. CPMM pools are inventoried but
remain unverified until a source-verifiable CPMM locker is available.
"""
import base64
import hashlib
import struct
import time

import requests
from solders.pubkey import Pubkey

from ..liquidity_evidence import (
    LiquidityInventoryBatch, LockerDeployment, LockerPositionSnapshot,
    PoolLiquiditySnapshot, SOLANA_UNCX_PROGRAM_CANDIDATES,
)
from .base import ProviderError


RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
UNCX_PROGRAM = "GsSCS3vPWrtJ5Y9aEVVT65fmrex5P5RGHXdZvsdbWgfo"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
BPF_UPGRADEABLE_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
RAYDIUM_V4_STATE_SIZE = 752
RAYDIUM_CPMM_STATE_SIZE = 637
RAYDIUM_CPMM_POOL_DISCRIMINATOR = hashlib.sha256(b"account:PoolState").digest()[:8]
UNCX_TOKEN_LOCK_SIZE = 146
TOKEN_LOCK_DISCRIMINATOR = hashlib.sha256(b"account:TokenLock").digest()[:8]
TOKEN_LOCK_LP_MINT_OFFSET = 41


def _b58(pubkey_bytes):
    return str(Pubkey.from_bytes(bytes(pubkey_bytes)))


def _decode_account_data(value, label):
    if not isinstance(value, dict) or not isinstance(value.get("data"), list) or len(value["data"]) < 1:
        raise ProviderError(f"{label} account data is malformed")
    try:
        return base64.b64decode(value["data"][0], validate=True)
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"{label} account data is not valid base64") from exc


def _account_value(response, label):
    if not isinstance(response, dict) or not isinstance(response.get("context"), dict):
        raise ProviderError(f"{label} response omitted context")
    value = response.get("value")
    if value is None:
        raise ProviderError(f"{label} account does not exist")
    return int(response["context"]["slot"]), value


class _SolanaRpc:
    def __init__(self, url, *, provider_id, session=None, timeout=25):
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("Solana liquidity RPC endpoints must use HTTPS")
        self.url = url
        self.provider_id = provider_id
        self.session = session or requests.Session()
        self.timeout = timeout
        self.counter = 0

    def call(self, method, params):
        self.counter += 1
        try:
            response = self.session.post(
                self.url,
                json={"jsonrpc": "2.0", "id": self.counter,
                      "method": method, "params": params},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"{method} Solana RPC request failed") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"{method} returned a Solana RPC error")
        return payload["result"]


class SolanaRaydiumV4CpmmSnapshotReader:
    """Exhaustive Raydium V4 + CPMM pool scan; UNCX lock decoder for V4 only.

    CLMM, Meteora DLMM, Orca and PumpSwap require separate state layouts and
    are not silently included in the declared supported-AMM inventory.
    CPMM pools are included but cannot pass the liquidity gate without a
    separately verified CPMM locker.
    """
    def __init__(
        self, pool_rpc_url, locker_rpc_url, *,
        expected_uncx_executable_hash=None,
        expected_raydium_executable_hash=None,
        expected_raydium_cpmm_executable_hash=None,
        registry_complete=False,
        pool_independence_group="solana-raydium-indexer",
        locker_independence_group="solana-uncx-locker-rpc",
        session=None, max_pools=20_000, max_locks_per_pool=20_000,
    ):
        if not pool_rpc_url or not locker_rpc_url or pool_rpc_url == locker_rpc_url:
            raise ValueError("independent Solana pool and locker RPC endpoints are required")
        if pool_independence_group == locker_independence_group:
            raise ValueError("Solana pool and locker independence groups must differ")
        if not 1 <= max_pools <= 100_000 or not 1 <= max_locks_per_pool <= 100_000:
            raise ValueError("invalid Solana liquidity reader bounds")
        candidate = SOLANA_UNCX_PROGRAM_CANDIDATES["raydium-amm-v4"]
        self.expected_hash = expected_uncx_executable_hash or candidate["published_executable_sha256"]
        if self.expected_hash != candidate["published_executable_sha256"]:
            raise ValueError("UNCX executable hash does not match the published candidate")
        if (not isinstance(expected_raydium_executable_hash, str)
                or len(expected_raydium_executable_hash) != 64
                or any(ch not in "0123456789abcdef" for ch in expected_raydium_executable_hash)):
            raise ValueError("a pinned Raydium AMM V4 executable SHA-256 hash is required")
        if (not isinstance(expected_raydium_cpmm_executable_hash, str)
                or len(expected_raydium_cpmm_executable_hash) != 64
                or any(ch not in "0123456789abcdef" for ch in expected_raydium_cpmm_executable_hash)):
            raise ValueError("a pinned Raydium CPMM executable SHA-256 hash is required")
        if registry_complete is not True:
            raise ValueError("Solana supported-AMM registry must be explicitly certified complete")
        self.expected_raydium_hash = expected_raydium_executable_hash
        self.expected_raydium_cpmm_hash = expected_raydium_cpmm_executable_hash
        self.pool_rpc = _SolanaRpc(
            pool_rpc_url, provider_id="solana-raydium-pool-rpc", session=session
        )
        self.locker_rpc = _SolanaRpc(
            locker_rpc_url, provider_id="solana-uncx-locker-rpc", session=session
        )
        self.pool_group = pool_independence_group
        self.locker_group = locker_independence_group
        self.max_pools = max_pools
        self.max_locks_per_pool = max_locks_per_pool
        self.provider_id = "solana-raydium-v4-cpmm-finalized-pool-inventory"
        self.amm_program = Pubkey.from_string(RAYDIUM_AMM_V4_PROGRAM)
        self.cpmm_program = Pubkey.from_string(RAYDIUM_CPMM_PROGRAM)
        self.uncx_program = Pubkey.from_string(UNCX_PROGRAM)
        self.token_program = Pubkey.from_string(TOKEN_PROGRAM)

    @staticmethod
    def _context_slot(response, label, expected=None):
        if not isinstance(response, dict) or not isinstance(response.get("context"), dict):
            raise ProviderError(f"{label} omitted RPC context slot")
        slot = response["context"].get("slot")
        if not isinstance(slot, int) or slot < 1:
            raise ProviderError(f"{label} returned an invalid context slot")
        if expected is not None and slot != expected:
            raise ProviderError(f"{label} was not read at the same finalized slot")
        return slot

    def _get_account(self, rpc, address, slot):
        result = rpc.call("getAccountInfo", [
            address, {"encoding": "base64", "commitment": "finalized",
                      "minContextSlot": slot},
        ])
        actual_slot, value = _account_value(result, f"account {address}")
        if actual_slot != slot:
            raise ProviderError("Solana account reads did not share one finalized slot")
        return value

    def _get_account_crosschecked(self, address, slot):
        pool_value = self._get_account(self.pool_rpc, address, slot)
        locker_value = self._get_account(self.locker_rpc, address, slot)
        if pool_value != locker_value:
            raise ProviderError("independent Solana RPCs disagree on finalized account state")
        return locker_value

    def _verify_program_executable(self, program_id, expected_hash, slot, label):
        program_account = self._get_account_crosschecked(program_id, slot)
        if (program_account.get("executable") is not True
                or program_account.get("owner") != BPF_UPGRADEABLE_LOADER):
            raise ProviderError(f"{label} program is not an upgradeable-loader executable")
        program_data = _decode_account_data(program_account, f"{label} program")
        if len(program_data) != 36 or struct.unpack_from("<I", program_data, 0)[0] != 2:
            raise ProviderError(f"{label} program account has an unexpected loader state")
        programdata_address = str(Pubkey.from_bytes(program_data[4:36]))
        programdata = self._get_account_crosschecked(programdata_address, slot)
        if programdata.get("owner") != BPF_UPGRADEABLE_LOADER:
            raise ProviderError(f"{label} ProgramData account owner is invalid")
        data = _decode_account_data(programdata, f"{label} ProgramData")
        if len(data) < 13 or struct.unpack_from("<I", data, 0)[0] != 3:
            raise ProviderError(f"{label} ProgramData account layout is invalid")
        authority_option = data[12]
        if authority_option == 1:
            if len(data) < 45:
                raise ProviderError(f"{label} ProgramData authority field is truncated")
            executable = data[45:]
        elif authority_option == 0:
            executable = data[13:]
        else:
            raise ProviderError(f"{label} ProgramData authority option is invalid")
        observed = hashlib.sha256(executable).hexdigest()
        if observed != expected_hash:
            raise ProviderError(f"deployed {label} executable hash does not match pinned source")
        return observed

    def _verify_uncx_program(self, slot):
        return self._verify_program_executable(
            UNCX_PROGRAM, self.expected_hash, slot, "UNCX locker"
        )

    def _verify_raydium_program(self, slot):
        return self._verify_program_executable(
            RAYDIUM_AMM_V4_PROGRAM, self.expected_raydium_hash, slot, "Raydium AMM V4"
        )

    def _verify_raydium_cpmm_program(self, slot):
        return self._verify_program_executable(
            RAYDIUM_CPMM_PROGRAM, self.expected_raydium_cpmm_hash, slot, "Raydium CPMM"
        )

    def _scan_raydium_pools(self, rpc, token_mint, slot):
        mint_key = str(Pubkey.from_string(token_mint))
        rows = {}
        for offset in (400, 432):
            response = rpc.call("getProgramAccounts", [
                RAYDIUM_AMM_V4_PROGRAM,
                {"encoding": "base64", "commitment": "finalized",
                 "withContext": True, "minContextSlot": slot,
                 "filters": [
                     {"dataSize": RAYDIUM_V4_STATE_SIZE},
                     {"memcmp": {"offset": offset, "bytes": mint_key}},
                 ]},
            ])
            self._context_slot(response, "Raydium pool discovery", slot)
            values = response.get("value")
            if not isinstance(values, list) or len(values) >= self.max_pools:
                raise ProviderError("Raydium pool discovery is malformed or at its result cap")
            for item in values:
                address = item.get("pubkey")
                account = item.get("account") or {}
                if account.get("owner") != RAYDIUM_AMM_V4_PROGRAM:
                    raise ProviderError("Raydium pool account owner mismatch")
                data = _decode_account_data(account, "Raydium pool")
                if len(data) != RAYDIUM_V4_STATE_SIZE:
                    raise ProviderError("Raydium V4 pool state has an unexpected size")
                base_mint = str(Pubkey.from_bytes(data[400:432]))
                quote_mint = str(Pubkey.from_bytes(data[432:464]))
                lp_mint = str(Pubkey.from_bytes(data[464:496]))
                if mint_key not in {base_mint, quote_mint}:
                    raise ProviderError("Raydium memcmp result does not contain requested mint")
                if address in rows:
                    if rows[address]["lp_mint"] != lp_mint:
                        raise ProviderError("Raydium pool appeared with conflicting state")
                    continue
                if not lp_mint or lp_mint == "11111111111111111111111111111111":
                    raise ProviderError("Raydium pool has an invalid LP mint")
                rows[address] = {
                    "amm_id": address, "lp_mint": lp_mint,
                    "base_mint": base_mint, "quote_mint": quote_mint,
                }
        if len(rows) > self.max_pools:
            raise ProviderError("Raydium pool inventory exceeds configured cap")
        return rows

    def _discover_raydium_pools(self, token_mint, slot):
        pool_rows = self._scan_raydium_pools(self.pool_rpc, token_mint, slot)
        locker_rows = self._scan_raydium_pools(self.locker_rpc, token_mint, slot)
        if pool_rows != locker_rows:
            raise ProviderError("independent Solana RPCs disagree on complete Raydium pool inventory")
        return tuple(pool_rows.values())

    def _scan_cpmm_pools(self, rpc, token_mint, slot):
        mint_key = str(Pubkey.from_string(token_mint))
        rows = {}
        for offset in (168, 200):
            response = rpc.call("getProgramAccounts", [
                RAYDIUM_CPMM_PROGRAM,
                {"encoding": "base64", "commitment": "finalized",
                 "withContext": True, "minContextSlot": slot,
                 "filters": [
                     {"dataSize": RAYDIUM_CPMM_STATE_SIZE},
                     {"memcmp": {"offset": offset, "bytes": mint_key}},
                 ]},
            ])
            self._context_slot(response, "Raydium CPMM pool discovery", slot)
            values = response.get("value")
            if not isinstance(values, list) or len(values) >= self.max_pools:
                raise ProviderError("Raydium CPMM pool discovery is malformed or at its result cap")
            for item in values:
                address = item.get("pubkey")
                account = item.get("account") or {}
                if account.get("owner") != RAYDIUM_CPMM_PROGRAM:
                    raise ProviderError("Raydium CPMM pool account owner mismatch")
                data = _decode_account_data(account, "Raydium CPMM pool")
                if (len(data) != RAYDIUM_CPMM_STATE_SIZE
                        or data[:8] != RAYDIUM_CPMM_POOL_DISCRIMINATOR):
                    raise ProviderError("Raydium CPMM PoolState discriminator or size mismatch")
                vault0 = str(Pubkey.from_bytes(data[72:104]))
                vault1 = str(Pubkey.from_bytes(data[104:136]))
                lp_mint = str(Pubkey.from_bytes(data[136:168]))
                token0_key = Pubkey.from_bytes(data[168:200])
                token1_key = Pubkey.from_bytes(data[200:232])
                token0 = str(token0_key)
                token1 = str(token1_key)
                if bytes(token0_key) >= bytes(token1_key):
                    raise ProviderError("Raydium CPMM token mint ordering is invalid")
                expected_lp, _ = Pubkey.find_program_address(
                    [b"pool_lp_mint", bytes(Pubkey.from_string(address))],
                    self.cpmm_program,
                )
                expected_vault0, _ = Pubkey.find_program_address(
                    [b"pool_vault", bytes(Pubkey.from_string(address)), bytes(token0_key)],
                    self.cpmm_program,
                )
                expected_vault1, _ = Pubkey.find_program_address(
                    [b"pool_vault", bytes(Pubkey.from_string(address)), bytes(token1_key)],
                    self.cpmm_program,
                )
                if (str(expected_lp) != lp_mint or str(expected_vault0) != vault0
                        or str(expected_vault1) != vault1):
                    raise ProviderError("Raydium CPMM LP mint or vault PDA derivation mismatch")
                if mint_key not in {token0, token1}:
                    raise ProviderError("Raydium CPMM memcmp result does not contain requested mint")
                if address in rows:
                    if rows[address]["lp_mint"] != lp_mint:
                        raise ProviderError("Raydium CPMM pool appeared with conflicting state")
                    continue
                rows[address] = {
                    "amm_id": address, "lp_mint": lp_mint,
                    "base_mint": token0, "quote_mint": token1,
                    "pool_type": "raydium-cpmm",
                }
        if len(rows) >= self.max_pools:
            raise ProviderError("Raydium CPMM pool inventory reached configured cap")
        return rows

    def _discover_cpmm_pools(self, token_mint, slot):
        pool_rows = self._scan_cpmm_pools(self.pool_rpc, token_mint, slot)
        locker_rows = self._scan_cpmm_pools(self.locker_rpc, token_mint, slot)
        if pool_rows != locker_rows:
            raise ProviderError("independent Solana RPCs disagree on complete Raydium CPMM pool inventory")
        return tuple(pool_rows.values())

    def _decode_token_lock(self, address, account):
        if account.get("owner") != UNCX_PROGRAM:
            raise ProviderError("UNCX lock account is not owned by the pinned program")
        data = _decode_account_data(account, "UNCX TokenLock")
        if len(data) != UNCX_TOKEN_LOCK_SIZE or data[:8] != TOKEN_LOCK_DISCRIMINATOR:
            raise ProviderError("UNCX TokenLock account discriminator or size mismatch")
        bump = data[8]
        amm_id = Pubkey.from_bytes(data[9:41])
        lp_mint = str(Pubkey.from_bytes(data[41:73]))
        lock_id = struct.unpack_from("<Q", data, 73)[0]
        lock_date = struct.unpack_from("<q", data, 81)[0]
        unlock_date = struct.unpack_from("<q", data, 89)[0]
        initial_amount = struct.unpack_from("<Q", data, 98)[0]
        current_amount = struct.unpack_from("<Q", data, 106)[0]
        owner = str(Pubkey.from_bytes(data[114:146]))
        expected, expected_bump = Pubkey.find_program_address(
            [b"uncx_locker", lock_id.to_bytes(8, "little")], self.uncx_program
        )
        if str(expected) != address or bump != expected_bump:
            raise ProviderError("UNCX lock account PDA or bump does not match its global lock ID")
        if lock_date <= 0 or unlock_date <= 0 or initial_amount < current_amount:
            raise ProviderError("UNCX TokenLock contains invalid timestamps or amounts")
        return {
            "address": address, "amm_id": str(amm_id), "lp_mint": lp_mint,
            "lock_id": lock_id, "lock_date": lock_date, "unlock_date": unlock_date,
            "initial_amount": initial_amount, "amount": current_amount, "owner": owner,
        }

    def _scan_lock_records(self, rpc, lp_mint, slot):
        response = rpc.call("getProgramAccounts", [
            UNCX_PROGRAM,
            {"encoding": "base64", "commitment": "finalized",
             "withContext": True, "minContextSlot": slot,
             "filters": [
                 {"dataSize": UNCX_TOKEN_LOCK_SIZE},
                 {"memcmp": {"offset": TOKEN_LOCK_LP_MINT_OFFSET, "bytes": lp_mint}},
             ]},
        ])
        self._context_slot(response, "UNCX lock enumeration", slot)
        values = response.get("value")
        if not isinstance(values, list) or len(values) >= self.max_locks_per_pool:
            raise ProviderError("UNCX lock enumeration is malformed or at its result cap")
        records = tuple(self._decode_token_lock(row.get("pubkey"), row.get("account") or {})
                         for row in values)
        return tuple(sorted(records, key=lambda record: record["address"]))

    def _read_lock_records(self, lp_mint, slot):
        pool_records = self._scan_lock_records(self.pool_rpc, lp_mint, slot)
        locker_records = self._scan_lock_records(self.locker_rpc, lp_mint, slot)
        if pool_records != locker_records:
            raise ProviderError("independent Solana RPCs disagree on complete UNCX lock account inventory")
        return locker_records

    def _read_lp_supply(self, lp_mint, slot):
        value = self._get_account_crosschecked(lp_mint, slot)
        if value.get("owner") != TOKEN_PROGRAM:
            raise ProviderError("Raydium LP mint is not owned by the classic SPL Token program")
        data = _decode_account_data(value, "LP mint")
        if len(data) < 82 or data[45] != 1:
            raise ProviderError("LP mint layout or initialized flag is invalid")
        return struct.unpack_from("<Q", data, 36)[0]

    def _read_custody_vault(self, amm_id, lp_mint, slot):
        authority, _ = Pubkey.find_program_address([b"uncx_authority"], self.uncx_program)
        vault, _ = Pubkey.find_program_address(
            [b"uncx_lp_vault", bytes(Pubkey.from_string(amm_id))], self.uncx_program
        )
        value = self._get_account_crosschecked(str(vault), slot)
        if value.get("owner") != TOKEN_PROGRAM:
            raise ProviderError("UNCX LP vault is not owned by the classic SPL Token program")
        data = _decode_account_data(value, "UNCX LP vault")
        if len(data) != 165:
            raise ProviderError("UNCX LP vault token-account layout is invalid")
        mint = str(Pubkey.from_bytes(data[0:32]))
        owner = str(Pubkey.from_bytes(data[32:64]))
        amount = struct.unpack_from("<Q", data, 64)[0]
        if mint != lp_mint or owner != str(authority) or data[108] != 1:
            raise ProviderError("UNCX LP vault mint, authority or initialized state mismatch")
        return str(vault), str(authority), amount

    def fetch_inventory(self, token_address):
        token_key = str(Pubkey.from_string(token_address))
        pool_slot_result = self.pool_rpc.call("getSlot", [{"commitment": "finalized"}])
        if not isinstance(pool_slot_result, int) or pool_slot_result < 1:
            raise ProviderError("Solana RPC did not return a finalized slot")
        slot = pool_slot_result
        locker_slot_result = self.locker_rpc.call("getSlot", [{"commitment": "finalized"}])
        if not isinstance(locker_slot_result, int) or locker_slot_result < slot:
            raise ProviderError("Solana locker RPC is behind the pool RPC finalized slot")

        executable_hash = self._verify_uncx_program(slot)
        self._verify_raydium_program(slot)
        self._verify_raydium_cpmm_program(slot)
        v4_pools = self._discover_raydium_pools(token_key, slot)
        cpmm_pools = self._discover_cpmm_pools(token_key, slot)
        discovered = tuple(
            dict(pool, pool_type="raydium-amm-v4") for pool in v4_pools
        ) + cpmm_pools
        if not discovered:
            raise ProviderError("no pools were discovered in the configured Raydium AMM V4 and CPMM scope")
        if len(discovered) >= self.max_pools:
            raise ProviderError("combined Raydium V4 and CPMM inventory reached configured cap")

        block_options = {
            "commitment": "finalized", "transactionDetails": "none",
            "rewards": False, "maxSupportedTransactionVersion": 0,
        }
        pool_block = self.pool_rpc.call("getBlock", [slot, block_options])
        locker_block = self.locker_rpc.call("getBlock", [slot, block_options])
        if (not isinstance(pool_block, dict) or not isinstance(locker_block, dict)
                or not pool_block.get("blockhash")
                or pool_block.get("blockhash") != locker_block.get("blockhash")
                or not isinstance(pool_block.get("blockTime"), int)
                or pool_block.get("blockTime") <= 0
                or locker_block.get("blockTime") != pool_block.get("blockTime")):
            raise ProviderError("independent Solana RPCs disagree on finalized slot block identity")
        block_time = pool_block["blockTime"]
        observed_ms = block_time * 1000
        finalized_ref = f"slot:{slot}:{pool_block['blockhash']}"

        pools = []
        positions = []
        for pool in discovered:
            lp_mint = pool["lp_mint"]
            supply = self._read_lp_supply(lp_mint, slot)
            if supply <= 0:
                raise ProviderError("Raydium LP mint supply is zero or unavailable")

            if pool["pool_type"] == "raydium-cpmm":
                # No independently verified UNCX CPMM locker implementation is
                # available. Keep every CPMM pool in the inventory with zero
                # verified custody so it blocks the all-pool safety claim.
                pools.append(PoolLiquiditySnapshot(
                    chain="solana", token_address=token_key, pool_address=pool["amm_id"],
                    lp_token_address=lp_mint, total_lp_supply=supply,
                    observed_at_ms=observed_ms, finalized_ref=finalized_ref,
                    inventory_provider=self.provider_id, inventory_complete=True,
                    pool_set_id="solana:raydium-amm-v4+cpmm:finalized",
                    locker_custody_amount=0,
                ))
                continue

            records = self._read_lock_records(lp_mint, slot)
            if any(record["amm_id"] != pool["amm_id"] for record in records):
                raise ProviderError("UNCX LP mint lock records reference a different AMM pool")
            vault, authority, custody_amount = self._read_custody_vault(
                pool["amm_id"], lp_mint, slot
            )
            locked_sum = sum(record["amount"] for record in records)
            if locked_sum != custody_amount:
                raise ProviderError("UNCX TokenLock records do not reconcile to LP vault balance")
            for record in records:
                if record["amount"] == 0:
                    continue
                positions.append(LockerPositionSnapshot(
                    chain="solana", pool_address=pool["amm_id"],
                    lp_token_address=lp_mint, locker_address=UNCX_PROGRAM,
                    position_id=record["address"], amount=record["amount"],
                    unlock_at_ms=record["unlock_date"] * 1000,
                    observed_at_ms=observed_ms, owner=record["owner"],
                    evidence_ref=f"{finalized_ref}:lock:{record['lock_id']}",
                    account_owner=UNCX_PROGRAM, custody_account=vault,
                    custody_authority=authority, custody_token_mint=lp_mint,
                    custody_account_owner=TOKEN_PROGRAM,
                    finalized_ref=finalized_ref,
                ))
            pools.append(PoolLiquiditySnapshot(
                chain="solana", token_address=token_key, pool_address=pool["amm_id"],
                lp_token_address=lp_mint, total_lp_supply=supply,
                observed_at_ms=observed_ms, finalized_ref=finalized_ref,
                inventory_provider=self.provider_id, inventory_complete=True,
                pool_set_id="solana:raydium-amm-v4+cpmm:finalized",
                locker_custody_amount=custody_amount,
            ))

        deployment = LockerDeployment(
            chain="solana", provider=self.locker_rpc.provider_id,
            address=UNCX_PROGRAM, abi_profile="uncx-anchor-token-lock-v1",
            expected_code_hash=self.expected_hash,
            observed_code_hash=executable_hash,
            source_ref="https://github.com/uncx-network/raydium-amm-lp-locker",
            independence_group=self.locker_group,
            program_executable_hash=executable_hash,
            custody_authority=str(Pubkey.find_program_address(
                [b"uncx_authority"], self.uncx_program
            )[0]),
        )
        return LiquidityInventoryBatch(
            chain="solana", token_address=token_key, provider=self.provider_id,
            independence_group=self.pool_group, observed_at_ms=observed_ms,
            finalized_ref=finalized_ref,
            pool_set_id="solana:raydium-amm-v4+cpmm:finalized",
            inventory_complete=True, pools=tuple(pools), positions=tuple(positions),
            deployments=(deployment,),
        )
