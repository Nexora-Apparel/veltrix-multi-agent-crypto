"""Fail-closed decoder for unsigned Solana legacy and v0 transactions.

This module parses the wire format directly. It does not trust a caller-provided
instruction manifest and does not sign, submit, or broadcast transactions.
ALT account bytes are parsed and validated locally before any loaded address is
used. The ALT account owner must be the canonical Address Lookup Table program.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import re
from typing import Mapping


ALT_PROGRAM_ID = "AddressLookupTab1e1111111111111111111111111"
_U64_MAX = (1 << 64) - 1
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(_BASE58)}


class SolanaMessageDecodeError(ValueError):
    """Raised when transaction bytes or lookup-table evidence are invalid."""

    def __init__(self, reason_code: str):
        if not isinstance(reason_code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", reason_code):
            raise ValueError("reason_code is malformed")
        self.reason_code = reason_code
        super().__init__(reason_code)


def _b58encode(raw: bytes) -> str:
    if not isinstance(raw, bytes):
        raise SolanaMessageDecodeError("invalid_public_key_bytes")
    zeros = len(raw) - len(raw.lstrip(b"\\x00"))
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58[remainder] + encoded
    return "1" * zeros + (encoded or ("" if zeros else "1"))


def _b58decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SolanaMessageDecodeError("invalid_lookup_table_address")
    number = 0
    for char in value:
        digit = _BASE58_INDEX.get(char)
        if digit is None:
            raise SolanaMessageDecodeError("invalid_lookup_table_address")
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\\x00" * (len(value) - len(value.lstrip("1"))) + body


def _read_compact_u16(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    start = offset
    for _ in range(3):
        if offset >= len(data):
            raise SolanaMessageDecodeError("truncated_compact_length")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if value > 0xFFFF:
                raise SolanaMessageDecodeError("compact_length_overflow")
            encoded_length = offset - start
            minimum_length = 1 if value < 0x80 else 2 if value < 0x4000 else 3
            if encoded_length != minimum_length:
                raise SolanaMessageDecodeError("noncanonical_compact_length")
            return value, offset
        shift += 7
    raise SolanaMessageDecodeError("compact_length_overflow")


def _take(data: bytes, offset: int, length: int, reason: str = "truncated_message") -> tuple[bytes, int]:
    if length < 0 or offset < 0 or offset + length > len(data):
        raise SolanaMessageDecodeError(reason)
    return data[offset:offset + length], offset + length


@dataclass(frozen=True, slots=True)
class AddressLookupTableSnapshot:
    """Raw RPC account snapshot; addresses are derived from account_data bytes."""

    address: str
    owner: str
    account_data: bytes
    context_slot: int


@dataclass(frozen=True, slots=True)
class CompiledInstruction:
    program_id_index: int
    program_id: str
    account_indices: tuple[int, ...]
    accounts: tuple[str, ...]
    data: bytes
    data_sha256: str


@dataclass(frozen=True, slots=True)
class DecodedLookup:
    table_address: str
    writable_indices: tuple[int, ...]
    readonly_indices: tuple[int, ...]
    writable_addresses: tuple[str, ...]
    readonly_addresses: tuple[str, ...]
    account_data_sha256: str


@dataclass(frozen=True, slots=True)
class DecodedSolanaTransaction:
    version: str
    message_bytes: bytes
    message_sha256: str
    transaction_sha256: str
    signatures: tuple[bytes, ...]
    required_signatures: int
    fee_payer: str
    static_account_keys: tuple[str, ...]
    loaded_writable_addresses: tuple[str, ...]
    loaded_readonly_addresses: tuple[str, ...]
    account_keys: tuple[str, ...]
    instructions: tuple[CompiledInstruction, ...]
    address_lookups: tuple[DecodedLookup, ...]
    recent_blockhash: str
    header: tuple[int, int, int]


def _decode_alt_snapshot(snapshot: AddressLookupTableSnapshot) -> tuple[bytes, ...]:
    if not isinstance(snapshot, AddressLookupTableSnapshot):
        raise SolanaMessageDecodeError("invalid_lookup_table_snapshot")
    if snapshot.owner != ALT_PROGRAM_ID:
        raise SolanaMessageDecodeError("lookup_table_wrong_owner")
    if isinstance(snapshot.context_slot, bool) or not isinstance(snapshot.context_slot, int) or snapshot.context_slot < 0:
        raise SolanaMessageDecodeError("invalid_lookup_table_context_slot")
    raw = snapshot.account_data
    if not isinstance(raw, bytes) or len(raw) < 56 or (len(raw) - 56) % 32:
        raise SolanaMessageDecodeError("malformed_lookup_table_account_data")
    state = int.from_bytes(raw[0:4], "little")
    deactivation_slot = int.from_bytes(raw[4:12], "little")
    last_extended_slot = int.from_bytes(raw[12:20], "little")
    last_extended_start_index = raw[20]
    if state != 1:
        raise SolanaMessageDecodeError("lookup_table_not_initialized")
    if deactivation_slot != _U64_MAX:
        raise SolanaMessageDecodeError("lookup_table_deactivated")
    addresses_blob = raw[56:]
    addresses = tuple(addresses_blob[i:i + 32] for i in range(0, len(addresses_blob), 32))
    if last_extended_start_index > len(addresses):
        raise SolanaMessageDecodeError("lookup_table_invalid_extension_index")
    if snapshot.context_slot < last_extended_slot:
        raise SolanaMessageDecodeError("lookup_table_snapshot_before_extension")
    return addresses


def decode_unsigned_transaction(
    transaction: bytes | bytearray | memoryview | str,
    *,
    lookup_tables: Mapping[str, AddressLookupTableSnapshot] | None = None,
    max_transaction_bytes: int = 1232,
) -> DecodedSolanaTransaction:
    """Decode a serialized unsigned transaction and resolve every v0 ALT.

    A string input is strict standard base64. Every signature slot must be all
    zero bytes; callers must not pass an already-signed transaction here.
    """
    if isinstance(transaction, str):
        try:
            raw = base64.b64decode(transaction, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise SolanaMessageDecodeError("invalid_transaction_base64") from exc
    elif isinstance(transaction, (bytes, bytearray, memoryview)):
        raw = bytes(transaction)
    else:
        raise SolanaMessageDecodeError("invalid_transaction_type")
    if not raw or len(raw) > max_transaction_bytes:
        raise SolanaMessageDecodeError("transaction_size_out_of_range")

    signature_count, offset = _read_compact_u16(raw, 0)
    signature_bytes, offset = _take(raw, offset, signature_count * 64)
    signatures = tuple(signature_bytes[i:i + 64] for i in range(0, len(signature_bytes), 64))
    message = raw[offset:]
    if not message:
        raise SolanaMessageDecodeError("missing_message")

    cursor = 0
    prefix = message[0]
    if prefix & 0x80:
        version_number = prefix & 0x7F
        if version_number != 0:
            raise SolanaMessageDecodeError("unsupported_message_version")
        version = "v0"
        cursor = 1
    else:
        version = "legacy"

    header_bytes, cursor = _take(message, cursor, 3)
    required_signatures, readonly_signed, readonly_unsigned = header_bytes
    if signature_count != required_signatures:
        raise SolanaMessageDecodeError("signature_count_header_mismatch")

    static_count, cursor = _read_compact_u16(message, cursor)
    if not 1 <= static_count <= 256:
        raise SolanaMessageDecodeError("invalid_static_account_count")
    static_blob, cursor = _take(message, cursor, static_count * 32)
    static_keys_raw = tuple(static_blob[i:i + 32] for i in range(0, len(static_blob), 32))
    if required_signatures < 1 or required_signatures > static_count:
        raise SolanaMessageDecodeError("invalid_required_signer_count")
    if readonly_signed > required_signatures:
        raise SolanaMessageDecodeError("invalid_readonly_signed_count")
    if readonly_unsigned > static_count - required_signatures:
        raise SolanaMessageDecodeError("invalid_readonly_unsigned_count")
    if readonly_signed == required_signatures:
        raise SolanaMessageDecodeError("fee_payer_not_writable")

    blockhash_raw, cursor = _take(message, cursor, 32)
    instruction_count, cursor = _read_compact_u16(message, cursor)
    if instruction_count > 64:
        raise SolanaMessageDecodeError("instruction_count_out_of_range")
    raw_instructions = []
    for _ in range(instruction_count):
        program_index_bytes, cursor = _take(message, cursor, 1)
        program_index = program_index_bytes[0]
        account_count, cursor = _read_compact_u16(message, cursor)
        account_blob, cursor = _take(message, cursor, account_count)
        data_length, cursor = _read_compact_u16(message, cursor)
        instruction_data, cursor = _take(message, cursor, data_length)
        raw_instructions.append((program_index, tuple(account_blob), instruction_data))

    raw_lookups = []
    if version == "v0":
        lookup_count, cursor = _read_compact_u16(message, cursor)
        if lookup_count > 32:
            raise SolanaMessageDecodeError("lookup_count_out_of_range")
        for _ in range(lookup_count):
            table_key, cursor = _take(message, cursor, 32)
            writable_count, cursor = _read_compact_u16(message, cursor)
            writable_blob, cursor = _take(message, cursor, writable_count)
            readonly_count, cursor = _read_compact_u16(message, cursor)
            readonly_blob, cursor = _take(message, cursor, readonly_count)
            raw_lookups.append((table_key, tuple(writable_blob), tuple(readonly_blob)))
    if cursor != len(message):
        raise SolanaMessageDecodeError("trailing_message_bytes")

    static_keys = tuple(_b58encode(key) for key in static_keys_raw)
    if len(set(static_keys)) != len(static_keys):
        raise SolanaMessageDecodeError("duplicate_static_account_key")
    if not static_keys:
        raise SolanaMessageDecodeError("missing_fee_payer")
    fee_payer = static_keys[0]

    lookup_map = lookup_tables or {}
    if not isinstance(lookup_map, Mapping):
        raise SolanaMessageDecodeError("invalid_lookup_table_mapping")
    loaded_writable: list[bytes] = []
    loaded_readonly: list[bytes] = []
    decoded_lookups: list[DecodedLookup] = []
    seen_tables: set[str] = set()
    for table_key_raw, writable_indices, readonly_indices in raw_lookups:
        table_address = _b58encode(table_key_raw)
        if table_address in seen_tables:
            raise SolanaMessageDecodeError("duplicate_lookup_table")
        seen_tables.add(table_address)
        snapshot = lookup_map.get(table_address)
        if snapshot is None or snapshot.address != table_address:
            raise SolanaMessageDecodeError("lookup_table_snapshot_missing")
        addresses = _decode_alt_snapshot(snapshot)
        if len(set(writable_indices + readonly_indices)) != len(writable_indices) + len(readonly_indices):
            raise SolanaMessageDecodeError("duplicate_lookup_index")
        if any(index >= len(addresses) for index in writable_indices + readonly_indices):
            raise SolanaMessageDecodeError("lookup_index_out_of_range")
        last_extended_slot = int.from_bytes(snapshot.account_data[12:20], "little")
        start_index = snapshot.account_data[20]
        if snapshot.context_slot == last_extended_slot and any(
            index >= start_index for index in writable_indices + readonly_indices
        ):
            raise SolanaMessageDecodeError("lookup_address_not_yet_warmed")
        writable = tuple(addresses[index] for index in writable_indices)
        readonly = tuple(addresses[index] for index in readonly_indices)
        loaded_writable.extend(writable)
        loaded_readonly.extend(readonly)
        decoded_lookups.append(DecodedLookup(
            table_address=table_address,
            writable_indices=writable_indices,
            readonly_indices=readonly_indices,
            writable_addresses=tuple(_b58encode(item) for item in writable),
            readonly_addresses=tuple(_b58encode(item) for item in readonly),
            account_data_sha256=hashlib.sha256(snapshot.account_data).hexdigest(),
        ))

    all_keys_raw = static_keys_raw + tuple(loaded_writable) + tuple(loaded_readonly)
    if len(all_keys_raw) > 256:
        raise SolanaMessageDecodeError("resolved_account_count_out_of_range")
    all_keys = tuple(_b58encode(key) for key in all_keys_raw)
    instructions = []
    for program_index, account_indices, instruction_data in raw_instructions:
        if program_index >= len(all_keys_raw):
            raise SolanaMessageDecodeError("program_account_index_out_of_range")
        if program_index >= static_count:
            raise SolanaMessageDecodeError("program_id_must_be_static")
        if any(index >= len(all_keys_raw) for index in account_indices):
            raise SolanaMessageDecodeError("instruction_account_index_out_of_range")
        instructions.append(CompiledInstruction(
            program_id_index=program_index,
            program_id=all_keys[program_index],
            account_indices=account_indices,
            accounts=tuple(all_keys[index] for index in account_indices),
            data=instruction_data,
            data_sha256=hashlib.sha256(instruction_data).hexdigest(),
        ))

    if any(signature != bytes(64) for signature in signatures):
        raise SolanaMessageDecodeError("signed_transaction_not_accepted")
    return DecodedSolanaTransaction(
        version=version,
        message_bytes=message,
        message_sha256=hashlib.sha256(message).hexdigest(),
        transaction_sha256=hashlib.sha256(raw).hexdigest(),
        signatures=signatures,
        required_signatures=required_signatures,
        fee_payer=fee_payer,
        static_account_keys=static_keys,
        loaded_writable_addresses=tuple(_b58encode(item) for item in loaded_writable),
        loaded_readonly_addresses=tuple(_b58encode(item) for item in loaded_readonly),
        account_keys=all_keys,
        instructions=tuple(instructions),
        address_lookups=tuple(decoded_lookups),
        recent_blockhash=_b58encode(blockhash_raw),
        header=(required_signatures, readonly_signed, readonly_unsigned),
    )
