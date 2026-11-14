from __future__ import annotations

import base64
import hashlib

import pytest

from veltrix.solana_message_decoder import (
    ALT_PROGRAM_ID,
    AddressLookupTableSnapshot,
    SolanaMessageDecodeError,
    decode_unsigned_transaction,
)


def compact(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pubkey(byte: int) -> bytes:
    return bytes([byte]) * 32


def b58(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    zeros = len(raw) - len(raw.lstrip(b"\\x00"))
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, rem = divmod(value, 58)
        encoded = alphabet[rem] + encoded
    return "1" * zeros + encoded


def transaction(message: bytes, signature_count: int = 1) -> bytes:
    return compact(signature_count) + bytes(64 * signature_count) + message


def legacy_message(*, instruction: bytes | None = None) -> bytes:
    # One writable signer (fee payer), one readonly program account.
    keys = pubkey(1) + pubkey(2)
    instructions = compact(0) if instruction is None else compact(1) + instruction
    return bytes([1, 0, 1]) + compact(2) + keys + pubkey(9) + instructions


def compiled_instruction(program_index: int = 1, accounts: bytes = b"\\x00", data: bytes = b"\\x01\\x02") -> bytes:
    return bytes([program_index]) + compact(len(accounts)) + accounts + compact(len(data)) + data


def alt_data(addresses: tuple[bytes, ...], *, last_extended_slot: int = 10, start_index: int = 0, deactivation_slot: int = (1 << 64) - 1) -> bytes:
    # LookupTableMeta: state, deactivation slot, last extension slot,
    # last extension start index, authority option + 32-byte authority, padding.
    metadata = (
        (1).to_bytes(4, "little")
        + deactivation_slot.to_bytes(8, "little")
        + last_extended_slot.to_bytes(8, "little")
        + bytes([start_index])
        + bytes([0])
        + bytes(32)
        + bytes(2)
    )
    assert len(metadata) == 56
    return metadata + b"".join(addresses)


def v0_message(table_key: bytes, *, writable: bytes = b"\\x00", readonly: bytes = b"\\x01") -> bytes:
    # Two static keys; two addresses loaded from one table.
    return (
        b"\\x80"
        + bytes([1, 0, 1])
        + compact(2)
        + pubkey(1)
        + pubkey(2)
        + pubkey(9)
        + compact(1)
        + compiled_instruction(program_index=1, accounts=b"\\x00\\x02\\x03")
        + compact(1)
        + table_key
        + compact(len(writable))
        + writable
        + compact(len(readonly))
        + readonly
    )


def snapshot(table_key: bytes, addresses: tuple[bytes, ...], *, slot: int = 11, **kwargs) -> AddressLookupTableSnapshot:
    return AddressLookupTableSnapshot(
        address=b58(table_key),
        owner=ALT_PROGRAM_ID,
        account_data=alt_data(addresses, **kwargs),
        context_slot=slot,
    )


def test_decodes_unsigned_legacy_message_and_hashes_exact_message():
    message = legacy_message(instruction=compiled_instruction())
    decoded = decode_unsigned_transaction(transaction(message))
    assert decoded.version == "legacy"
    assert decoded.message_bytes == message
    assert decoded.message_sha256 == hashlib.sha256(message).hexdigest()
    assert decoded.fee_payer == b58(pubkey(1))
    assert decoded.static_account_keys == (b58(pubkey(1)), b58(pubkey(2)))
    assert decoded.instructions[0].program_id == b58(pubkey(2))
    assert decoded.instructions[0].accounts == (b58(pubkey(1)),)
    assert decoded.instructions[0].data == b"\\x01\\x02"


def test_decodes_v0_and_resolves_loaded_writable_then_readonly_addresses():
    table = pubkey(7)
    writable, readonly = pubkey(3), pubkey(4)
    message = v0_message(table)
    decoded = decode_unsigned_transaction(
        transaction(message),
        lookup_tables={b58(table): snapshot(table, (writable, readonly))},
    )
    assert decoded.version == "v0"
    assert decoded.loaded_writable_addresses == (b58(writable),)
    assert decoded.loaded_readonly_addresses == (b58(readonly),)
    assert decoded.account_keys[-2:] == (b58(writable), b58(readonly))
    assert decoded.instructions[0].accounts == (b58(pubkey(1)), b58(writable), b58(readonly))


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b"", "transaction_size_out_of_range"),
        (b"\\x81", "truncated_compact_length"),
        (compact(1) + bytes(64), "missing_message"),
        (compact(2) + bytes(128) + legacy_message(), "signature_count_header_mismatch"),
        (transaction(bytes([1, 0, 1]) + compact(1) + pubkey(1) + pubkey(9) + compact(0)), "invalid_required_signer_count"),
        (transaction(legacy_message() + b"\\x00"), "trailing_message_bytes"),
        (transaction(b"\\x81" + bytes(40)), "unsupported_message_version"),
        (transaction(legacy_message(), signature_count=1)[:-1], "truncated_message"),
    ],
)
def test_rejects_malformed_or_unsupported_messages(raw, reason):
    with pytest.raises(SolanaMessageDecodeError) as error:
        decode_unsigned_transaction(raw)
    assert error.value.reason_code == reason


def test_rejects_nonzero_signature_slot():
    raw = compact(1) + b"\\x01" + bytes(63) + legacy_message()
    with pytest.raises(SolanaMessageDecodeError, match="signed_transaction_not_accepted"):
        decode_unsigned_transaction(raw)


def test_rejects_noncanonical_compact_length():
    # Compact-u16 value 1 encoded in two bytes.
    raw = b"\\x81\\x00" + bytes(64) + legacy_message()
    with pytest.raises(SolanaMessageDecodeError, match="noncanonical_compact_length"):
        decode_unsigned_transaction(raw)


def test_rejects_missing_alt_snapshot():
    with pytest.raises(SolanaMessageDecodeError, match="lookup_table_snapshot_missing"):
        decode_unsigned_transaction(transaction(v0_message(pubkey(7))))


def test_rejects_alt_wrong_owner():
    table = pubkey(7)
    bad = AddressLookupTableSnapshot(
        address=b58(table), owner=b58(pubkey(8)),
        account_data=alt_data((pubkey(3), pubkey(4))), context_slot=11,
    )
    with pytest.raises(SolanaMessageDecodeError, match="lookup_table_wrong_owner"):
        decode_unsigned_transaction(
            transaction(v0_message(table)),
            lookup_tables={b58(table): bad},
        )


def test_rejects_deactivated_alt():
    table = pubkey(7)
    evidence = snapshot(table, (pubkey(3), pubkey(4)), deactivation_slot=12)
    with pytest.raises(SolanaMessageDecodeError, match="lookup_table_deactivated"):
        decode_unsigned_transaction(
            transaction(v0_message(table)),
            lookup_tables={b58(table): evidence},
        )


def test_rejects_alt_index_out_of_range():
    table = pubkey(7)
    evidence = snapshot(table, (pubkey(3),))
    with pytest.raises(SolanaMessageDecodeError, match="lookup_index_out_of_range"):
        decode_unsigned_transaction(
            transaction(v0_message(table)),
            lookup_tables={b58(table): evidence},
        )


def test_rejects_alt_address_not_warmed_in_snapshot_slot():
    table = pubkey(7)
    evidence = snapshot(table, (pubkey(3), pubkey(4)), slot=10, last_extended_slot=10, start_index=1)
    with pytest.raises(SolanaMessageDecodeError, match="lookup_address_not_yet_warmed"):
        decode_unsigned_transaction(
            transaction(v0_message(table)),
            lookup_tables={b58(table): evidence},
        )


def test_rejects_wrong_lookup_snapshot_address():
    table = pubkey(7)
    evidence = snapshot(pubkey(8), (pubkey(3), pubkey(4)))
    with pytest.raises(SolanaMessageDecodeError, match="lookup_table_snapshot_missing"):
        decode_unsigned_transaction(
            transaction(v0_message(table)),
            lookup_tables={b58(table): evidence},
        )


def test_rejects_program_id_loaded_from_alt():
    table = pubkey(7)
    # Program index 2 points at the first loaded key rather than a static key.
    message = (
        b"\\x80" + bytes([1, 0, 1]) + compact(2) + pubkey(1) + pubkey(2)
        + pubkey(9) + compact(1) + compiled_instruction(program_index=2, accounts=b"")
        + compact(1) + table + compact(1) + b"\\x00" + compact(0)
    )
    evidence = snapshot(table, (pubkey(3),))
    with pytest.raises(SolanaMessageDecodeError, match="program_id_must_be_static"):
        decode_unsigned_transaction(
            transaction(message),
            lookup_tables={b58(table): evidence},
        )


def test_strict_base64_input():
    message = legacy_message()
    encoded = base64.b64encode(transaction(message)).decode()
    assert decode_unsigned_transaction(encoded).message_bytes == message
    with pytest.raises(SolanaMessageDecodeError, match="invalid_transaction_base64"):
        decode_unsigned_transaction(encoded + "!")
