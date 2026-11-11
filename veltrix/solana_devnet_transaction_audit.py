"""Read-only semantic audit of the captured Raydium Devnet v0 swap.

Uses canonical Solana Devnet RPC only. It never signs, sends, or broadcasts.
Any unknown account, instruction, privilege, CPI, or account layout fails closed.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import json
import traceback
from pathlib import Path
from typing import Any

import requests
from solders.pubkey import Pubkey

from veltrix.solana_v0_inspector import (
    ALT_PROGRAM_ID, U64_MAX, V0DecodeError, parse_base64_v0_transaction,
    parse_alt_account, resolve_v0_keys,
)

RPC_URL = "https://api.devnet.solana.com"
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
WALLET = "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"
ALT = "EFhMuDw1PKEuckuFRW9PavNfTH4LKP5uKHgyXDmWpFCq"
ROUTER = "DRaybByLpbUL57LJARs3j8BitTxVfzBg351EaMr5UTCd"
CPMM = "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
POOL = "5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo"
INPUT_AMOUNT = 1_000_000
MIN_OUTPUT = 3_396
CPMM_SWAP_BASE_INPUT = bytes([143, 190, 90, 218, 196, 30, 51, 222])
POOL_STATE_DISC = bytes([247, 237, 227, 245, 215, 195, 222, 70])
AMM_CONFIG_DISC = bytes([218, 244, 33, 104, 203, 203, 43, 111])
ROUTER_ACCOUNT_COUNT = 17
POOL_AUTH_SEED = b"vault_and_lp_mint_auth_seed"


class AuditError(RuntimeError):
    pass


def b58decode(value: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for char in value:
        if char not in alphabet:
            raise AuditError("invalid base58 data")
        number = number * 58 + alphabet.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + body


def b58encode(value: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(value, "big")
    output = ""
    while number:
        number, rem = divmod(number, 58)
        output = alphabet[rem] + output
    return "1" * (len(value) - len(value.lstrip(b"\x00"))) + (output or "")


def read_u64(data: bytes, offset: int) -> int:
    if offset + 8 > len(data):
        raise AuditError("truncated u64 instruction/account data")
    return int.from_bytes(data[offset:offset + 8], "little")


def read_rpc_data(value: dict[str, Any]) -> bytes:
    data = value.get("data")
    if not isinstance(data, list) or len(data) < 2 or data[1] != "base64":
        raise AuditError("RPC account data is not base64")
    try:
        return base64.b64decode(data[0], validate=True)
    except Exception as exc:
        raise AuditError("RPC returned malformed base64 account data") from exc


class DevnetRPC:
    def __init__(self, *, session: Any | None = None, timeout: float = 20.0) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.request_id = 0

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        self.request_id += 1
        response = self.session.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params or []},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or "result" not in payload:
            raise AuditError(f"canonical Devnet RPC {method} failed")
        return payload["result"]

    def account(self, address: str, *, min_slot: int | None = None) -> dict[str, Any] | None:
        config: dict[str, Any] = {"encoding": "base64", "commitment": "finalized"}
        if min_slot is not None:
            config["minContextSlot"] = min_slot
        result = self.call("getAccountInfo", [address, config])
        value = result.get("value") if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

    def multiple_accounts(self, addresses: list[str], *, min_slot: int) -> tuple[int, list[dict[str, Any] | None]]:
        result = self.call("getMultipleAccounts", [addresses, {
            "encoding": "base64", "commitment": "finalized", "minContextSlot": min_slot,
        }])
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise AuditError("getMultipleAccounts returned an invalid response")
        return int(result.get("context", {}).get("slot", 0)), result["value"]


@dataclass(frozen=True)
class KeyPrivilege:
    address: str
    signer: bool
    writable: bool
    source: str


def key_privileges(message: Any, resolved_keys: tuple[bytes, ...]) -> list[KeyPrivilege]:
    static_count = len(message.static_keys)
    required = message.required_signatures
    signed_writable_end = required - message.readonly_signed
    unsigned_writable_end = static_count - message.readonly_unsigned
    result: list[KeyPrivilege] = []
    for index, key in enumerate(resolved_keys):
        if index < static_count:
            signer = index < required
            writable = index < signed_writable_end or (required <= index < unsigned_writable_end)
            source = "static"
        else:
            loaded_writable_count = sum(len(item.writable_indexes) for item in message.lookups)
            signer = False
            writable = index < static_count + loaded_writable_count
            source = "lookup_writable" if writable else "lookup_readonly"
        result.append(KeyPrivilege(b58encode(key), signer, writable, source))
    return result


def token_account_fields(account: dict[str, Any], *, expected_programs: set[str]) -> tuple[str, str, int]:
    if account.get("owner") not in expected_programs or account.get("executable") is not False:
        raise AuditError("token account is owned by an unexpected program or is executable")
    data = read_rpc_data(account)
    if len(data) < 165:
        raise AuditError("token account data is shorter than the SPL token account layout")
    mint = b58encode(data[0:32])
    owner = b58encode(data[32:64])
    amount = int.from_bytes(data[64:72], "little")
    state = data[108]
    if state not in (1, 2):
        raise AuditError("token account is not initialized")
    return mint, owner, amount


def associated_token_address(owner: str, mint: str, token_program: str) -> str:
    seeds = [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(mint))]
    address, _ = Pubkey.find_program_address(seeds, Pubkey.from_string(ATA_PROGRAM))
    return str(address)


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def audit_fixture(fixture: dict[str, Any], *, rpc: DevnetRPC | None = None) -> dict[str, Any]:
    rpc = rpc or DevnetRPC()
    _expect(rpc.call("getGenesisHash") == GENESIS, "RPC endpoint is not canonical Solana Devnet")
    slot = int(rpc.call("getSlot", [{"commitment": "finalized"}]))
    message = parse_base64_v0_transaction(fixture["unsigned_transaction_base64"])
    _expect(fixture.get("wallet") == WALLET, "fixture wallet differs from the pinned test wallet")
    _expect(fixture.get("lookup_table") == ALT, "fixture ALT differs from the pinned lookup table")
    _expect(message.required_signatures == 1, "transaction must require exactly one signature")
    _expect(message.static_keys[0] == bytes(Pubkey.from_string(WALLET)), "fee payer is not the pinned wallet")
    _expect(len(message.lookups) == 1 and str(Pubkey.from_bytes(message.lookups[0].account_key)) == ALT,
            "transaction lookup table list is unexpected")

    alt_account = rpc.account(ALT, min_slot=slot)
    _expect(alt_account is not None, "captured address lookup table does not exist on Devnet")
    _expect(alt_account.get("owner") == ALT_PROGRAM_ID, "lookup table has an unexpected account owner")
    _expect(alt_account.get("executable") is False, "lookup table account is unexpectedly executable")
    alt_data = read_rpc_data(alt_account)
    addresses = parse_alt_account(ALT_PROGRAM_ID, base64.b64encode(alt_data).decode(), current_slot=slot)
    _expect(len(addresses) > max(message.lookups[0].writable_indexes + message.lookups[0].readonly_indexes), "lookup table does not contain every referenced address")
    resolved = resolve_v0_keys(
        message, {bytes(Pubkey.from_string(ALT)): (ALT_PROGRAM_ID, base64.b64encode(alt_data).decode())},
        current_slot=slot,
    )
    privileges = key_privileges(message, resolved)
    key_strings = [item.address for item in privileges]
    _expect(len(key_strings) == 18, "resolved v0 account-key count is not 18")
    _expect(key_strings[17] == CPMM, "loaded read-only CPMM program key mismatch")

    # Exact compiled message order: wrap-native, route, close-native, CU price, CU limit.
    _expect(len(message.instructions) == 5, "unexpected top-level instruction count")
    expected_programs = [ROUTER, ROUTER, ROUTER, COMPUTE_BUDGET, COMPUTE_BUDGET]
    actual_programs = [key_strings[ix.program_id_index] for ix in message.instructions]
    _expect(actual_programs == expected_programs, "unexpected top-level program sequence")

    setup, route, cleanup, cu_price, cu_limit = message.instructions
    _expect(setup.data[:1] == b"\x05" and len(setup.data) == 9 and read_u64(setup.data, 1) == INPUT_AMOUNT,
            "router wrapped-SOL setup instruction does not encode the exact input amount")
    _expect(route.data[:1] == b"\x00" and len(route.data) == 17, "router route instruction discriminator/length mismatch")
    _expect(read_u64(route.data, 1) == INPUT_AMOUNT, "router route input amount mismatch")
    _expect(read_u64(route.data, 9) == int(fixture["minimum_output_raw"]) == MIN_OUTPUT,
            "router minimum output does not match the reviewed fixture")
    _expect(cleanup.data == b"\x06", "router wrapped-SOL cleanup instruction mismatch")
    _expect(cu_price.data[:1] == b"\x03" and len(cu_price.data) == 9 and read_u64(cu_price.data, 1) <= 1_000_000,
            "compute unit price instruction is malformed or exceeds the cap")
    _expect(cu_limit.data[:1] == b"\x02" and len(cu_limit.data) == 5 and
            50_000 <= int.from_bytes(cu_limit.data[1:], "little") <= 1_400_000,
            "compute unit limit is malformed or outside safe bounds")

    def accounts(ix: Any) -> list[str]:
        return [key_strings[index] for index in ix.account_indexes]

    setup_accounts = accounts(setup)
    route_accounts = accounts(route)
    cleanup_accounts = accounts(cleanup)
    expected_setup = [WALLET, key_strings[1], WSOL, TOKEN, ATA_PROGRAM, SYSTEM]
    expected_route = [
        TOKEN, TOKEN_2022, ATA_PROGRAM, SYSTEM, WALLET, key_strings[1], key_strings[3],
        CPMM, key_strings[3], WSOL, USDC,
        key_strings[5], key_strings[16], POOL, key_strings[7], key_strings[8], key_strings[9],
    ]
    expected_cleanup = [WALLET, key_strings[1], WALLET, TOKEN, ATA_PROGRAM, SYSTEM]
    _expect(setup_accounts == expected_setup, "wrapped-SOL setup account order/identity mismatch")
    _expect(len(route_accounts) == ROUTER_ACCOUNT_COUNT and route_accounts == expected_route,
            "router CPMM route account order/identity mismatch")
    _expect(cleanup_accounts == expected_cleanup, "wrapped-SOL cleanup account order/recipient mismatch")

    # Verify effective signer/writable privileges after ALT expansion.
    _expect(privileges[0].signer and privileges[0].writable, "fee payer is not the sole writable signer")
    _expect(sum(1 for item in privileges if item.signer) == 1, "resolved message has an unexpected signer")
    _expect(not privileges[17].writable and not privileges[17].signer, "CPMM program key must be read-only and non-signer")
    _expect(not privileges[10].writable and not privileges[10].signer, "router program key must be read-only and non-signer")
    _expect(not privileges[15].writable and not privileges[15].signer, "Compute Budget program key must be read-only")

    # Fetch all accounts at one finalized context and verify program, pool, vault and token ownership.
    addresses_to_fetch = [WALLET, *key_strings[1:]]
    context_slot, account_values = rpc.multiple_accounts(addresses_to_fetch, min_slot=slot)
    _expect(context_slot >= slot, "account snapshot is older than the finalized ALT snapshot")
    account_map = {address: value for address, value in zip(addresses_to_fetch, account_values)}
    cpmm_program = account_map[CPMM]
    router_program = account_map[ROUTER]
    _expect(cpmm_program is not None and cpmm_program.get("executable") is True, "Devnet CPMM program is missing/not executable")
    _expect(router_program is not None and router_program.get("executable") is True, "Devnet router program is missing/not executable")
    _expect(account_map[POOL] is not None and account_map[POOL].get("owner") == CPMM, "pool account is not owned by the pinned CPMM program")
    pool_data = read_rpc_data(account_map[POOL])
    _expect(pool_data[:8] == POOL_STATE_DISC and len(pool_data) >= 329, "CPMM pool state discriminator/layout mismatch")
    pool_config = b58encode(pool_data[8:40])
    pool_vault0 = b58encode(pool_data[72:104])
    pool_vault1 = b58encode(pool_data[104:136])
    pool_mint0 = b58encode(pool_data[168:200])
    pool_mint1 = b58encode(pool_data[200:232])
    pool_token_program0 = b58encode(pool_data[232:264])
    pool_token_program1 = b58encode(pool_data[264:296])
    pool_observation = b58encode(pool_data[296:328])
    auth_bump = pool_data[328]
    _expect(pool_config == key_strings[16], "pool's AMM config differs from the router config")
    _expect({pool_mint0, pool_mint1} == {WSOL, USDC}, "pool state mint pair is not wrapped SOL/Devnet USDC")
    _expect({pool_vault0, pool_vault1} == {key_strings[7], key_strings[8]}, "pool state vaults differ from router vaults")
    _expect(pool_observation == key_strings[9], "pool observation account differs from route")
    _expect(pool_data[329] & 4 == 0, "CPMM pool state has swaps disabled")
    _expect(account_map[key_strings[16]] is not None and account_map[key_strings[16]].get("owner") == CPMM,
            "AMM config account is not owned by CPMM")
    config_data = read_rpc_data(account_map[key_strings[16]])
    _expect(config_data[:8] == AMM_CONFIG_DISC, "AMM config discriminator mismatch")
    _expect(account_map[key_strings[9]] is not None and account_map[key_strings[9]].get("owner") == CPMM,
            "observation account is not owned by CPMM")
    authority, bump = Pubkey.find_program_address([POOL_AUTH_SEED], Pubkey.from_string(CPMM))
    _expect(str(authority) == key_strings[5] and bump == auth_bump, "CPMM authority PDA or bump mismatch")

    input_account = account_map[key_strings[1]]
    output_account = account_map[key_strings[3]]
    _expect(associated_token_address(WALLET, WSOL, TOKEN) == key_strings[1], "input account is not the wallet WSOL ATA")
    _expect(associated_token_address(WALLET, USDC, TOKEN) == key_strings[3], "output account is not the wallet USDC ATA")
    input_account_present = input_account is not None
    output_account_present = output_account is not None
    if input_account_present:
        in_mint, in_owner, _ = token_account_fields(input_account, expected_programs={TOKEN})
        _expect((in_mint, in_owner) == (WSOL, WALLET), "input token account mint/owner mismatch")
    if output_account_present:
        out_mint, out_owner, _ = token_account_fields(output_account, expected_programs={TOKEN})
        _expect((out_mint, out_owner) == (USDC, WALLET), "output token account mint/owner mismatch")

    vault_in = account_map[key_strings[7]]
    vault_out = account_map[key_strings[8]]
    _expect(vault_in is not None and vault_out is not None, "one or more CPMM vault accounts are missing")
    vin_mint, vin_owner, _ = token_account_fields(vault_in, expected_programs={TOKEN, TOKEN_2022})
    vout_mint, vout_owner, _ = token_account_fields(vault_out, expected_programs={TOKEN, TOKEN_2022})
    _expect(vin_mint == WSOL and vout_mint == USDC, "CPMM vault mint order does not match the route")
    _expect(vin_owner == str(authority) and vout_owner == str(authority), "CPMM vault authority owner mismatch")
    _expect(pool_token_program0 in {TOKEN, TOKEN_2022} and pool_token_program1 in {TOKEN, TOKEN_2022},
            "pool state contains an unexpected token program")
    vault0_mint = vin_mint if pool_vault0 == key_strings[7] else vout_mint
    vault1_mint = vin_mint if pool_vault1 == key_strings[7] else vout_mint
    _expect(account_map[pool_vault0].get("owner") == pool_token_program0 and vault0_mint == pool_mint0,
            "token-0 vault program/mint does not match PoolState")
    _expect(account_map[pool_vault1].get("owner") == pool_token_program1 and vault1_mint == pool_mint1,
            "token-1 vault program/mint does not match PoolState")
    _expect(account_map[WSOL] is not None and account_map[USDC] is not None, "one or both mint accounts are missing")
    _expect(account_map[WSOL].get("owner") == TOKEN and account_map[USDC].get("owner") == TOKEN,
            "configured mint accounts are not owned by the classic SPL Token program")
    wsol_mint_data = read_rpc_data(account_map[WSOL])
    usdc_mint_data = read_rpc_data(account_map[USDC])
    _expect(len(wsol_mint_data) >= 82 and wsol_mint_data[45] == 1 and wsol_mint_data[44] == 9,
            "wrapped-SOL mint account layout/decimals mismatch")
    _expect(len(usdc_mint_data) >= 82 and usdc_mint_data[45] == 1 and usdc_mint_data[44] == 6,
            "Devnet USDC mint account layout/decimals mismatch")

    # Simulate the exact captured bytes with inner-instruction recording enabled.
    sim = rpc.call("simulateTransaction", [fixture["unsigned_transaction_base64"], {
        "encoding": "base64", "commitment": "confirmed", "sigVerify": False,
        "replaceRecentBlockhash": True, "innerInstructions": True,
    }])
    value = sim.get("value") if isinstance(sim, dict) else None
    _expect(isinstance(value, dict) and value.get("err") is None, "captured transaction simulation failed")
    inner_groups = value.get("innerInstructions")
    _expect(isinstance(inner_groups, list), "Devnet simulation did not return inner-instruction metadata")
    inner_flat: list[tuple[int, str, list[int], bytes]] = []
    for group in inner_groups:
        outer_index = group.get("index")
        for inner in group.get("instructions", []):
            program_index = inner.get("programIdIndex")
            program_address = inner.get("programId")
            if isinstance(program_index, int) and 0 <= program_index < len(key_strings):
                program_address = key_strings[program_index]
            _expect(isinstance(program_address, str) and bool(program_address),
                    "inner CPI has no resolvable program identity: " + repr(inner))
            data = b58decode(inner.get("data", ""))
            inner_flat.append((outer_index, program_address, list(inner.get("accounts", [])), data, inner))
    def inner_account_names(items: list[Any]) -> list[str]:
        if all(isinstance(index, int) and not isinstance(index, bool) for index in items):
            names = [key_strings[index] for index in items]
        elif all(isinstance(address, str) for address in items):
            names = list(items)
        else:
            raise AuditError("inner CPI account list has an unsupported shape")
        _expect(all(name in key_strings for name in names),
                "inner CPI references an account outside the resolved transaction key list")
        return names

    cpmm_cpis = [item for item in inner_flat if item[1] == CPMM]
    _expect(len(cpmm_cpis) == 1, "simulation must contain exactly one CPMM CPI")
    cpi_outer_index, _, cpi_account_indexes, cpi_data, _cpi_raw = cpmm_cpis[0]
    _expect(cpi_outer_index == 1, "CPMM CPI is not nested under the route instruction")
    _expect(cpi_data[:8] == CPMM_SWAP_BASE_INPUT and len(cpi_data) == 24,
            "inner CPMM instruction is not the pinned SwapBaseInput layout")
    actual_cpi_input = read_u64(cpi_data, 8)
    actual_cpi_minimum = read_u64(cpi_data, 16)
    _expect(actual_cpi_input == INPUT_AMOUNT,
            "inner CPMM input differs from the reviewed amount: " + str(actual_cpi_input))
    unsafe_reasons: list[str] = []
    if actual_cpi_minimum == 0:
        unsafe_reasons.append(
            "CPMM CPI has minimum_amount_out=0; only the opaque router's outer instruction carries the reviewed threshold"
        )
    elif actual_cpi_minimum < MIN_OUTPUT:
        unsafe_reasons.append("CPMM CPI minimum output is below the reviewed threshold")
    expected_cpmm_accounts = [
        WALLET, key_strings[5], key_strings[16], POOL, key_strings[1], key_strings[3],
        key_strings[7], key_strings[8], TOKEN, TOKEN, WSOL, USDC, key_strings[9],
    ]
    _expect(inner_account_names(cpi_account_indexes) == expected_cpmm_accounts,
            "inner CPMM account order differs from the pinned IDL")

    # Reject token authorities, mint/burn, approvals, arbitrary transfers and unknown CPI variants.
    allowed_inner_programs = {CPMM, TOKEN, TOKEN_2022, ATA_PROGRAM, SYSTEM}
    _expect(all(item[1] in allowed_inner_programs for item in inner_flat),
            "simulation contains an unrecognized CPI program")
    allowed_user_atas = {key_strings[1], key_strings[3]}
    created_user_atas: set[str] = set()
    for _, program, account_indexes, data, raw_inner in inner_flat:
        if program == ATA_PROGRAM:
            names = inner_account_names(account_indexes)
            _expect(data in {b"\\x00", b"\\x01"} and len(names) == 6,
                    "associated-token CPI shape mismatch: data=" + data.hex() +
                    ", accounts=" + repr(names) + ", raw=" + repr(raw_inner))
            payer, ata, owner, mint, system_program, token_program = names
            _expect(payer == WALLET and ata in allowed_user_atas and owner == WALLET,
                    "associated-token CPI payer, owner or destination is unexpected")
            _expect((ata, mint, token_program) in {
                (key_strings[1], WSOL, TOKEN), (key_strings[3], USDC, TOKEN)
            } and system_program == SYSTEM, "associated-token CPI mint/program tuple is unexpected")
            created_user_atas.add(ata)
        elif program == SYSTEM:
            _expect(len(data) >= 4, "System Program CPI data is truncated")
            tag = int.from_bytes(data[:4], "little")
            names = [key_strings[index] for index in account_indexes]
            if tag == 0:  # CreateAccount
                _expect(len(data) == 52 and len(names) == 2, "System CreateAccount CPI has unexpected shape")
                lamports = read_u64(data, 4)
                space = read_u64(data, 12)
                owner_program = b58encode(data[20:52])
                _expect(names[0] == WALLET and names[1] in allowed_user_atas,
                        "System CreateAccount payer or destination is unexpected")
                _expect(0 < lamports <= 10_000_000 and space == 165 and owner_program == TOKEN,
                        "System CreateAccount rent, space or owner is unexpected")
            elif tag == 2:  # Transfer
                _expect(len(data) == 12 and len(names) == 2, "System Transfer CPI has unexpected shape")
                _expect(names == [WALLET, key_strings[1]] and read_u64(data, 4) == INPUT_AMOUNT,
                        "System Transfer is not the exact wrapped-SOL funding transfer")
            else:
                raise AuditError(f"disallowed System Program CPI instruction {tag}")
    _expect(input_account_present or key_strings[1] in created_user_atas,
            "missing wrapped-SOL ATA is not created by the inspected setup CPI")
    _expect(output_account_present or key_strings[3] in created_user_atas,
            "missing Devnet USDC ATA is not created by the inspected setup CPI")
    allowed_transfer_pairs = {(key_strings[1], key_strings[7]), (key_strings[8], key_strings[3])}
    token_transfer_amounts: list[tuple[str, str, int]] = []
    for _, program, account_indexes, data in inner_flat:
        if program not in {TOKEN, TOKEN_2022}:
            continue
        _expect(bool(data), "empty SPL Token CPI data")
        tag = data[0]
        account_names = [key_strings[index] for index in account_indexes]
        if tag == 9:  # CloseAccount: account, destination, authority
            _expect(len(account_names) == 3 and account_names == [key_strings[1], WALLET, WALLET],
                    "CloseAccount CPI has an unexpected account or lamport recipient")
        elif tag == 17:  # SyncNative
            _expect(account_names == [key_strings[1]], "SyncNative targets an unexpected token account")
        elif tag == 3:  # Transfer
            _expect(len(data) == 9 and len(account_names) == 3, "SPL Transfer CPI has unexpected shape")
            source, destination, authority_name = account_names
            amount = read_u64(data, 1)
            _expect((source, destination) in allowed_transfer_pairs,
                    "SPL Transfer CPI has an unexpected source or recipient")
            _expect(authority_name in {WALLET, str(authority)}, "SPL Transfer CPI authority is unexpected")
            if source == key_strings[1]:
                _expect(amount == INPUT_AMOUNT, "wrapped-SOL transfer amount is not exact")
            else:
                _expect(amount >= MIN_OUTPUT, "USDC transfer is below the reviewed minimum")
            token_transfer_amounts.append((source, destination, amount))
        elif tag == 12:  # TransferChecked
            _expect(len(data) == 10 and len(account_names) == 4, "SPL TransferChecked CPI has unexpected shape")
            source, mint_name, destination, authority_name = account_names
            amount = read_u64(data, 1)
            _expect((source, destination) in allowed_transfer_pairs,
                    "SPL TransferChecked has an unexpected source or recipient")
            _expect(mint_name == (WSOL if source == key_strings[1] else USDC),
                    "SPL TransferChecked mint does not match its token accounts")
            _expect(authority_name in {WALLET, str(authority)}, "SPL TransferChecked authority is unexpected")
            if source == key_strings[1]:
                _expect(amount == INPUT_AMOUNT, "wrapped-SOL TransferChecked amount is not exact")
            else:
                _expect(amount >= MIN_OUTPUT, "USDC TransferChecked amount is below the reviewed minimum")
            token_transfer_amounts.append((source, destination, amount))
        else:
            raise AuditError(f"disallowed SPL Token instruction tag {tag}")
    _expect(any(src == key_strings[1] and dst == key_strings[7] and amount == INPUT_AMOUNT
                for src, dst, amount in token_transfer_amounts),
            "no exact wrapped-SOL transfer into the CPMM input vault was observed")
    _expect(any(src == key_strings[8] and dst == key_strings[3] and amount >= MIN_OUTPUT
                for src, dst, amount in token_transfer_amounts),
            "no minimum-sized USDC transfer into the wallet output account was observed")
    close_cpis = [
        item for item in inner_flat
        if item[1] in {TOKEN, TOKEN_2022} and item[3][:1] == b"\x09"
    ]
    _expect(len(close_cpis) == 1, "expected exactly one SPL CloseAccount CPI")
    _, _, close_accounts, close_data = close_cpis[0]
    _expect(len(close_data) == 1 and len(close_accounts) == 3, "CloseAccount CPI has unexpected shape")
    _expect(inner_account_names(close_accounts) == [key_strings[1], WALLET, WALLET],
            "temporary wrapped-SOL account is not closed to the connected wallet")

    return {
        "evidence_type": "raydium_devnet_transaction_semantic_audit",
        "status": "rejected" if unsafe_reasons else "passed",
        "cluster": "devnet",
        "rpc_endpoint": RPC_URL,
        "unsafe_reasons": unsafe_reasons,
        "cpmm_cpi_minimum_output_raw": actual_cpi_minimum,
        "finalized_slot": slot,
        "account_snapshot_slot": context_slot,
        "wallet": WALLET,
        "lookup_table": ALT,
        "lookup_table_owner": alt_account["owner"],
        "lookup_table_address_count": len(addresses),
        "lookup_table_writable_indexes": list(message.lookups[0].writable_indexes),
        "lookup_table_readonly_indexes": list(message.lookups[0].readonly_indexes),
        "resolved_keys": [asdict(item) for item in privileges],
        "top_level_instruction_programs": actual_programs,
        "router_instruction_tags": [setup.data[0], route.data[0], cleanup.data[0]],
        "input_lamports": INPUT_AMOUNT,
        "minimum_output_raw": MIN_OUTPUT,
        "pool": POOL,
        "cpmm_program": CPMM,
        "cpmm_cpi_count": len(cpmm_cpis),
        "inner_instruction_count": len(inner_flat),
        "simulation_logs": value.get("logs", [])[-50:],
        "transaction_signed": False,
        "transaction_submitted": False,
        "transaction_broadcast": False,
        "swap_executed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit of captured Raydium Devnet transaction")
    parser.add_argument("--fixture", default="tests/fixtures/raydium_devnet_swap_v0_fixture.json")
    parser.add_argument("--output", default="artifacts/raydium-devnet-transaction-audit.json")
    args = parser.parse_args()
    try:
        fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        evidence = audit_fixture(fixture)
        code = 0 if evidence.get("status") == "passed" else 2
    except Exception as exc:
        evidence = {
            "evidence_type": "raydium_devnet_transaction_semantic_audit",
            "status": "failed",
            "cluster": "devnet",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=8),
            "transaction_signed": False,
            "transaction_submitted": False,
            "transaction_broadcast": False,
            "swap_executed": False,
        }
        code = 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
