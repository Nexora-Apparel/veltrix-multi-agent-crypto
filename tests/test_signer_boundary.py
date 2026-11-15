"""Contract tests for the signer boundary; no signing or network calls occur."""
from dataclasses import FrozenInstanceError
import hashlib

import pytest

from veltrix.signer_boundary import (
    SignerBoundary,
    SignerBoundaryError,
    SignedTransactionEnvelope,
    UnsignedTransactionEnvelope,
    validate_signed_response,
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


def signed_response(**overrides):
    values = {
        "intent_id": "intent-001",
        "signer_public_key": "PublicKeyExample",
        "chain": "solana",
        "network": "devnet",
        "policy_hash": "a" * 64,
        "message_sha256": hashlib.sha256(b"serialized-solana-message").hexdigest(),
        "signed_transaction": b"opaque-signed-transaction",
        "transaction_signature": "signature-reference",
    }
    values.update(overrides)
    return SignedTransactionEnvelope(**values)


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
    result = signed_response()
    assert result.signed_transaction == b"opaque-signed-transaction"
    with pytest.raises(FrozenInstanceError):
        result.intent_id = "other"


@pytest.mark.parametrize(
    "field,value",
    [
        ("intent_id", ""),
        ("signer_public_key", ""),
        ("chain", ""),
        ("network", ""),
        ("policy_hash", ""),
        ("transaction_signature", ""),
        ("message_sha256", "z" * 64),
        ("signed_transaction", b""),
        ("signed_transaction", bytearray(b"mutable")),
    ],
)
def test_signed_envelope_rejects_malformed_fields(field, value):
    with pytest.raises(SignerBoundaryError):
        signed_response(**{field: value})


def test_signed_response_must_match_every_approved_request_binding():
    approved = request()
    validate_signed_response(approved, signed_response())


@pytest.mark.parametrize(
    "field,value",
    [
        ("intent_id", "other-intent"),
        ("signer_public_key", "OtherPublicKey"),
        ("chain", "base"),
        ("network", "mainnet"),
        ("policy_hash", "b" * 64),
        ("message_sha256", "c" * 64),
    ],
)
def test_signed_response_rejects_binding_mismatch(field, value):
    with pytest.raises(SignerBoundaryError, match=field):
        validate_signed_response(request(), signed_response(**{field: value}))


@pytest.mark.parametrize(
    "approved,response",
    [
        (object(), signed_response()),
        (request(), object()),
    ],
)
def test_signed_response_rejects_wrong_envelope_types(approved, response):
    with pytest.raises(SignerBoundaryError):
        validate_signed_response(approved, response)


def test_protocol_defines_sign_contract_without_providing_implementation():
    assert "sign" in SignerBoundary.__dict__
    assert not any(
        name in SignerBoundary.__dict__
        for name in ("broadcast", "submit", "load_key", "export_key")
    )
