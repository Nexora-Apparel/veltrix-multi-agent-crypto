import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from veltrix.solana_devnet_transaction_audit import (
    AuditError, b58decode, b58encode, key_privileges, read_u64,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "raydium_devnet_swap_v0_fixture.json").read_text())


def test_base58_round_trip_for_captured_lookup_table():
    value = FIXTURE["lookup_table"]
    assert b58encode(b58decode(value)) == value


def test_privilege_reconstruction_preserves_static_and_loaded_order():
    message = SimpleNamespace(
        static_keys=(bytes([1]) * 32, bytes([2]) * 32, bytes([3]) * 32, bytes([4]) * 32),
        required_signatures=1,
        readonly_signed=0,
        readonly_unsigned=2,
        lookups=(SimpleNamespace(writable_indexes=(0,), readonly_indexes=(1,)),),
    )
    keys = message.static_keys + (bytes([5]) * 32, bytes([6]) * 32)
    privileges = key_privileges(message, keys)
    assert [p.signer for p in privileges] == [True, False, False, False, False, False]
    assert [p.writable for p in privileges] == [True, True, False, False, True, False]
    assert [p.source for p in privileges] == [
        "static", "static", "static", "static", "lookup_writable", "lookup_readonly"
    ]


def test_read_u64_decodes_little_endian_and_rejects_truncation():
    assert read_u64(bytes.fromhex("40420f0000000000"), 0) == 1_000_000
    with pytest.raises(AuditError, match="truncated"):
        read_u64(b"\\x01\\x02", 0)


def test_captured_fixture_contains_only_the_expected_exact_swap_intent():
    from veltrix.solana_v0_inspector import parse_base64_v0_transaction

    message = parse_base64_v0_transaction(FIXTURE["unsigned_transaction_base64"])
    assert message.required_signatures == 1
    assert len(message.instructions) == 5
    assert message.instructions[0].data == b"\\x05" + (1_000_000).to_bytes(8, "little")
    assert message.instructions[1].data == (
        b"\\x00" + (1_000_000).to_bytes(8, "little") + (3_396).to_bytes(8, "little")
    )
    assert message.instructions[2].data == b"\\x06"
