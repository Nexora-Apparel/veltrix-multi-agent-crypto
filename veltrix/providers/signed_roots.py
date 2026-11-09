"""Cryptographically verified developer-root attestation adapter.

The remote source must return a signed, canonical JSON payload. Ed25519
verification binds the provider identity, chain, token, root addresses,
coverage statement, timestamp and evidence reference. A signature proves
the configured provider issued the claim; it does not independently prove
that a root is a human developer unless the provider's methodology does so.
"""
import base64
import hashlib
import json
import time
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..developer_evidence import DeveloperRootAttestation
from .base import ProviderError


def _root_key(chain, address):
    return address.lower() if chain in {"base", "bsc"} else address


class SignedDeveloperRootProvider:
    provider_id = "signed-developer-roots"

    def __init__(self, endpoint, public_key_b64, *, provider_id=None,
                 session=None, timeout=15.0, max_response_bytes=128_000,
                 max_roots=256, max_age_ms=300_000, clock_ms=None):
        parsed = urlparse(endpoint or "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Developer-root endpoint must use HTTPS")
        try:
            raw_key = base64.b64decode(public_key_b64, validate=True)
            self.public_key = Ed25519PublicKey.from_public_bytes(raw_key)
        self.signing_key_fingerprint = hashlib.sha256(raw_key).hexdigest()
        except (ValueError, TypeError) as exc:
            raise ValueError("Developer-root Ed25519 public key must be base64-encoded 32 bytes") from exc
        self.endpoint = endpoint
        self.provider_id = provider_id or self.provider_id
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_roots = max_roots
        self.max_age_ms = max_age_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _valid_root(chain, address):
        if not isinstance(address, str) or not address:
            return False
        if chain in {"base", "bsc"}:
            return len(address) == 42 and address.startswith("0x") and all(
                char in "0123456789abcdefABCDEF" for char in address[2:]
            )
        if chain == "solana":
            alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
            return 32 <= len(address) <= 44 and all(char in alphabet for char in address)
        return False

    def fetch_attestation(self, chain, token_address):
        chain = str(chain).lower()
        if chain not in {"solana", "base", "bsc"}:
            raise ProviderError("Unsupported chain for developer-root attestation")
        try:
            response = self.session.get(
                self.endpoint, params={"chain": chain, "token_address": token_address},
                headers={"Accept": "application/json"}, timeout=self.timeout,
            )
            response.raise_for_status()
            raw = response.content
            if not isinstance(raw, bytes) or len(raw) > self.max_response_bytes:
                raise ProviderError("Developer-root response exceeds size limit")
            envelope = json.loads(raw.decode("utf-8"))
        except ProviderError:
            raise
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            raise ProviderError("Developer-root attestation request or response failed") from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            raise ProviderError("Developer-root signed envelope is malformed")
        payload = envelope["payload"]
        signature_text = envelope.get("signature")
        if not isinstance(signature_text, str):
            raise ProviderError("Developer-root signature is missing")
        try:
            signature = base64.b64decode(signature_text, validate=True)
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
            self.public_key.verify(signature, canonical)
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise ProviderError("Developer-root Ed25519 signature verification failed") from exc

        roots = payload.get("roots")
        observed = payload.get("observed_at_ms")
        token = payload.get("token_address")
        payload_chain = payload.get("chain")
        evidence_id = payload.get("evidence_id")
        source_ref = payload.get("source_ref")
        provider = payload.get("provider")
        if payload_chain != chain or token != token_address:
            raise ProviderError("Signed developer-root claim does not match requested chain and token")
        if provider != self.provider_id:
            raise ProviderError("Signed developer-root provider identity mismatch")
        if (not isinstance(observed, int) or isinstance(observed, bool)
                or observed > self.clock_ms() or self.clock_ms() - observed > self.max_age_ms):
            raise ProviderError("Signed developer-root attestation is stale or from the future")
        if (not isinstance(evidence_id, str) or not evidence_id
                or not isinstance(source_ref, str)
                or urlparse(source_ref).scheme != "https"
                or not urlparse(source_ref).netloc):
            raise ProviderError("Signed developer-root evidence provenance is incomplete")
        if payload.get("coverage_complete") is not True:
            raise ProviderError("Signed developer-root coverage is not complete")
        if (not isinstance(roots, list) or not roots or len(roots) > self.max_roots
                or any(not self._valid_root(chain, root) for root in roots)
                or len({_root_key(chain, root) for root in roots}) != len(roots)):
            raise ProviderError("Signed developer-root address inventory is invalid")
        return DeveloperRootAttestation(
            chain=chain, token_address=token, provider=provider,
            evidence_id=evidence_id, observed_at_ms=observed,
            roots=tuple(roots), coverage_complete=True, source_ref=source_ref,
            signature_verified=True,
            signing_key_fingerprint=self.signing_key_fingerprint,
        )
