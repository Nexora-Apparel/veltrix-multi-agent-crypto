import base64
import json
from pathlib import Path

import pytest

from veltrix.solana_v0_inspector import (
    ALT_PROGRAM_ID, V0DecodeError, parse_base64_v0_transaction,
    parse_alt_account, resolve_v0_keys,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "raydium_devnet_swap_v0_fixture.json").read_text())


def test_captured_raydium_transaction_v0_structure():
    message = parse_base64_v0_transaction(FIXTURE["unsigned_transaction_base64"])
    assert message.required_signatures == 1
    assert len(message.static_keys) == 16
    assert len(message.instructions) == 5
    assert len(message.lookups) == 1
    assert len(message.lookups[0].writable_indexes) == 1
    assert len(message.lookups[0].readonly_indexes) == 1
    assert message.static_keys[0] == _decode_base58(FIXTURE["wallet"])


def _decode_base58(value):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for char in value:
        number = number * 58 + alphabet.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\\x00" * (len(value) - len(value.lstrip("1"))) + raw


def test_alt_wrong_owner_rejected():
    with pytest.raises(V0DecodeError, match="wrong owner"):
        parse_alt_account("11111111111111111111111111111111", "", current_slot=1)


def test_alt_bad_length_rejected():
    encoded = base64.b64encode(b"short").decode()
    with pytest.raises(V0DecodeError, match="invalid length"):
        parse_alt_account(ALT_PROGRAM_ID, encoded, current_slot=1)


def test_missing_alt_rejected_for_captured_transaction():
    message = parse_base64_v0_transaction(FIXTURE["unsigned_transaction_base64"])
    with pytest.raises(V0DecodeError, match="not supplied"):
        resolve_v0_keys(message, {}, current_slot=1)


def test_truncated_transaction_rejected():
    encoded = base64.b64encode(b"\\x01\\x00").decode()
    with pytest.raises(V0DecodeError):
        parse_base64_v0_transaction(encoded)
