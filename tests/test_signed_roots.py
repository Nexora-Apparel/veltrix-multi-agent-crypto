import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from veltrix.providers.base import ProviderError
from veltrix.providers.signed_roots import SignedDeveloperRootProvider


NOW = 9_000_000


class Response:
    def __init__(self, payload):
        self.content = json.dumps(payload).encode()
    def raise_for_status(self):
        return None


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return Response(self.payload)


def signed_payload(private_key, payload):
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return {
        "payload": payload,
        "signature": base64.b64encode(private_key.sign(canonical)).decode(),
    }


def setup_provider(payload, *, private_key=None):
    private_key = private_key or Ed25519PrivateKey.generate()
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    session = Session(signed_payload(private_key, payload))
    provider = SignedDeveloperRootProvider(
        "https://attest.example/v1/roots",
        base64.b64encode(public).decode(),
        provider_id="independent-root-registry",
        session=session,
        clock_ms=lambda: NOW,
    )
    return provider, session, private_key


def payload(**overrides):
    data = {
        "chain": "base",
        "token_address": "0x" + "a" * 40,
        "provider": "independent-root-registry",
        "evidence_id": "signed-root-evidence-01",
        "observed_at_ms": NOW - 100,
        "roots": ["0x" + "b" * 40],
        "coverage_complete": True,
        "source_ref": "https://attest.example/evidence/01",
    }
    data.update(overrides)
    return data


def test_signed_root_provider_verifies_ed25519_and_binds_request():
    provider, session, _ = setup_provider(payload())
    result = provider.fetch_attestation("base", "0x" + "a" * 40)
    assert result.provider == "independent-root-registry"
    assert result.roots == ("0x" + "b" * 40,)
    assert result.coverage_complete
    assert session.calls[0]["params"] == {
        "chain": "base", "token_address": "0x" + "a" * 40,
    }


def test_signed_root_provider_rejects_payload_tampering():
    provider, _, _ = setup_provider(payload())
    provider.session.payload["payload"]["roots"] = ["0x" + "c" * 40]
    with pytest.raises(ProviderError, match="signature verification failed"):
        provider.fetch_attestation("base", "0x" + "a" * 40)


def test_signed_root_provider_rejects_invalid_signature():
    provider, _, _ = setup_provider(payload())
    provider.session.payload["signature"] = base64.b64encode(b"not-a-valid-ed25519-signature").decode()
    with pytest.raises(ProviderError, match="signature verification failed"):
        provider.fetch_attestation("base", "0x" + "a" * 40)


@pytest.mark.parametrize("changes,reason", [
    ({"token_address": "0x" + "c" * 40}, "does not match"),
    ({"provider": "different-provider"}, "identity mismatch"),
    ({"coverage_complete": False}, "not complete"),
    ({"observed_at_ms": NOW - 400_000}, "stale"),
    ({"roots": ["not-an-evm-address"]}, "address inventory"),
    ({"source_ref": "http://attest.example/evidence"}, "provenance"),
])
def test_signed_root_provider_rejects_invalid_claims(changes, reason):
    provider, _, _ = setup_provider(payload(**changes))
    with pytest.raises(ProviderError, match=reason):
        provider.fetch_attestation("base", "0x" + "a" * 40)


def test_signed_root_provider_rejects_duplicate_roots():
    root = "0x" + "b" * 40
    provider, _, _ = setup_provider(payload(roots=[root, root]))
    with pytest.raises(ProviderError, match="address inventory"):
        provider.fetch_attestation("base", "0x" + "a" * 40)


def test_signed_root_provider_rejects_non_https_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        SignedDeveloperRootProvider("http://attest.example/roots", "invalid")
