"""Contract tests for the signer boundary; no signing or network calls occur."""
from dataclasses import FrozenInstanceError
import hashlib

import pytest

from veltrix.signer_boundary import (
    SignerBoundary,
    SignerBoundaryError,
    SignedTransactionEnvelope,
    UnsignedTransactionEnvelope,
)


def request(**overrides):
    message = b"serialized-solana-message"
    values = {
        "intent_id": "intent-001",
        "chain": "solana",
        "network": "devnet",
        "signer_public_key": "PublicKeyExample",
        "message": message,
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "policy_hash": "a" * 64,
    }
    values.update(overrides)
    return UnsignedTransactionEnvelope(**values)


def test_unsigned_envelope_is_immutable_and_contains_exact_message():
    item = request()
    assert item.message == b"serialized-solana-message"
    with pytest.raises(FrozenInstanceError):
        item.intent_id = "changed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("intent_id", ""),
        ("chain", ""),
        ("network", ""),
        ("signer_public_key", ""),
        ("policy_hash", ""),
        ("message", b""),
        ("message", bytearray(b"mutable")),
        ("message_sha256", "not-a-digest"),
        ("message_sha256", "0" * 64),
    ],
)
def test_unsigned_envelope_rejects_malformed_fields(field, value):
    with pytest.raises(SignerBoundaryError):
        request(**{field: value})


def test_signed_envelope_is_opaque_immutable_output():
    result = SignedTransactionEnvelope(
        intent_id="intent-001",
        signer_public_key="PublicKeyExample",
        message_sha256="b" * 64,
        signed_transaction=b"opaque-signed-transaction",
        transaction_signature="signature-reference",
    )
    assert result.signed_transaction == b"opaque-signed-transaction"
    with pytest.raises(FrozenInstanceError):
        result.intent_id = "other"


@pytest.mark.parametrize(
    "field,value",
    [
        ("intent_id", ""),
        ("signer_public_key", ""),
        ("transaction_signature", ""),
        ("message_sha256", "z" * 64),
        ("signed_transaction", b""),
        ("signed_transaction", bytearray(b"mutable")),
    ],
)
def test_signed_envelope_rejects_malformed_fields(field, value):
    values = {
        "intent_id": "intent-001",
        "signer_public_key": "PublicKeyExample",
        "message_sha256": "b" * 64,
        "signed_transaction": b"opaque-signed-transaction",
        "transaction_signature": "signature-reference",
    }
    values[field] = value
    with pytest.raises(SignerBoundaryError):
        SignedTransactionEnvelope(**values)


def test_protocol_defines_sign_contract_without_providing_implementation():
    assert "sign" in SignerBoundary.__dict__
    assert not any(
        name in SignerBoundary.__dict__
        for name in ("broadcast", "submit", "load_key", "export_key")
    )
