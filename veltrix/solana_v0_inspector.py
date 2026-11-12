"""Structural decoder for Solana v0 transactions and ALT account data.

This module deliberately does not authorize signing. It parses bytes, validates
bounds and resolves lookup-table contents; semantic policy is a separate layer.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
from typing import Mapping

ALT_PROGRAM_ID = "AddressLookupTab1e1111111111111111111111111"
ALT_META_SIZE = 56
U64_MAX = (1 << 64) - 1


class V0DecodeError(ValueError):
    pass


@dataclass(frozen=True)
class Lookup:
    account_key: bytes
    writable_indexes: tuple[int, ...]
    readonly_indexes: tuple[int, ...]


@dataclass(frozen=True)
class CompiledInstruction:
    program_id_index: int
    account_indexes: tuple[int, ...]
    data: bytes


@dataclass(frozen=True)
class V0Message:
    signatures: tuple[bytes, ...]
    required_signatures: int
    readonly_signed: int
    readonly_unsigned: int
    static_keys: tuple[bytes, ...]
    recent_blockhash: bytes
    instructions: tuple[CompiledInstruction, ...]
    lookups: tuple[Lookup, ...]


def _shortvec(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    for _ in range(5):
        if offset >= len(data):
            raise V0DecodeError("truncated shortvec")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise V0DecodeError("shortvec exceeds five bytes")


def _take(data: bytes, offset: int, size: int) -> tuple[bytes, int]:
    if size < 0 or offset + size > len(data):
        raise V0DecodeError("truncated transaction")
    return data[offset:offset + size], offset + size


def parse_v0_transaction(raw: bytes) -> V0Message:
    offset = 0
    signature_count, offset = _shortvec(raw, offset)
    if signature_count > 64:
        raise V0DecodeError("unreasonable signature count")
    signatures = []
    for _ in range(signature_count):
        sig, offset = _take(raw, offset, 64)
        signatures.append(sig)

    version, offset = _take(raw, offset, 1)
    if version[0] != 0x80:
        raise V0DecodeError("only Solana v0 messages are supported")
    header, offset = _take(raw, offset, 3)
    required, readonly_signed, readonly_unsigned = header
    key_count, offset = _shortvec(raw, offset)
    if not key_count or required > key_count or readonly_signed > required or readonly_unsigned > key_count - required:
        raise V0DecodeError("invalid message header")
    if signature_count != required:
        raise V0DecodeError("signature vector does not match required signer count")
    keys = []
    for _ in range(key_count):
        key, offset = _take(raw, offset, 32)
        keys.append(key)
    blockhash, offset = _take(raw, offset, 32)
    instruction_count, offset = _shortvec(raw, offset)
    instructions = []
    for _ in range(instruction_count):
        program, offset = _take(raw, offset, 1)
        account_count, offset = _shortvec(raw, offset)
        account_bytes, offset = _take(raw, offset, account_count)
        data_count, offset = _shortvec(raw, offset)
        instruction_data, offset = _take(raw, offset, data_count)
        instructions.append(CompiledInstruction(program[0], tuple(account_bytes), instruction_data))
    lookup_count, offset = _shortvec(raw, offset)
    lookups = []
    for _ in range(lookup_count):
        key, offset = _take(raw, offset, 32)
        writable_count, offset = _shortvec(raw, offset)
        writable, offset = _take(raw, offset, writable_count)
        readonly_count, offset = _shortvec(raw, offset)
        readonly, offset = _take(raw, offset, readonly_count)
        lookups.append(Lookup(key, tuple(writable), tuple(readonly)))
    if offset != len(raw):
        raise V0DecodeError("trailing bytes after v0 message")
    total_keys = len(keys) + sum(len(x.writable_indexes) + len(x.readonly_indexes) for x in lookups)
    for ix in instructions:
        if ix.program_id_index >= total_keys or any(i >= total_keys for i in ix.account_indexes):
            raise V0DecodeError("compiled instruction account index out of bounds")
    return V0Message(tuple(signatures), required, readonly_signed, readonly_unsigned,
                     tuple(keys), blockhash, tuple(instructions), tuple(lookups))


def parse_alt_account(owner: str, encoded_data: str, *, current_slot: int) -> tuple[bytes, ...]:
    if owner != ALT_PROGRAM_ID:
        raise V0DecodeError("lookup table account has the wrong owner")
    try:
        data = base64.b64decode(encoded_data, validate=True)
    except Exception as exc:
        raise V0DecodeError("lookup table data is not valid base64") from exc
    if len(data) < ALT_META_SIZE or (len(data) - ALT_META_SIZE) % 32:
        raise V0DecodeError("lookup table account data has invalid length")
    state = int.from_bytes(data[0:4], "little")
    deactivation_slot = int.from_bytes(data[4:12], "little")
    last_extended_slot = int.from_bytes(data[12:20], "little")
    last_extended_start = data[20]
    if state != 1:
        raise V0DecodeError("lookup table is not initialized")
    if deactivation_slot != U64_MAX:
        raise V0DecodeError("lookup table is deactivated or deactivating")
    addresses = tuple(data[i:i + 32] for i in range(ALT_META_SIZE, len(data), 32))
    if last_extended_slot > current_slot:
        raise V0DecodeError("lookup table extension slot is in the future")
    if last_extended_slot == current_slot and last_extended_start > len(addresses):
        raise V0DecodeError("lookup table extension metadata is invalid")
    return addresses


def resolve_v0_keys(message: V0Message, lookup_accounts: Mapping[bytes, tuple[str, str]],
                    *, current_slot: int) -> tuple[bytes, ...]:
    writable: list[bytes] = []
    readonly: list[bytes] = []
    for lookup in message.lookups:
        account = lookup_accounts.get(lookup.account_key)
        if account is None:
            raise V0DecodeError("required lookup table was not supplied")
        owner, encoded_data = account
        addresses = parse_alt_account(owner, encoded_data, current_slot=current_slot)
        raw_table = base64.b64decode(encoded_data, validate=True)
        last_extended_slot = int.from_bytes(raw_table[12:20], "little")
        last_extended_start = raw_table[20]
        for index in lookup.writable_indexes:
            if index >= len(addresses):
                raise V0DecodeError("writable lookup index is out of bounds")
            if last_extended_slot == current_slot and index >= last_extended_start:
                raise V0DecodeError("writable lookup address was extended in the current slot")
            writable.append(addresses[index])
        for index in lookup.readonly_indexes:
            if index >= len(addresses):
                raise V0DecodeError("readonly lookup index is out of bounds")
            if last_extended_slot == current_slot and index >= last_extended_start:
                raise V0DecodeError("readonly lookup address was extended in the current slot")
            readonly.append(addresses[index])
    keys = tuple(message.static_keys) + tuple(writable) + tuple(readonly)
    for ix in message.instructions:
        if ix.program_id_index >= len(keys) or any(i >= len(keys) for i in ix.account_indexes):
            raise V0DecodeError("resolved instruction index is out of bounds")
    return keys


def parse_base64_v0_transaction(encoded: str) -> V0Message:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise V0DecodeError("transaction is not valid base64") from exc
    return parse_v0_transaction(raw)
