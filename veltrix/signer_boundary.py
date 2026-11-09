"""Contract-only boundary for a future isolated transaction signer.

This module contains no signing implementation, key material, wallet connection,
or transaction submission. It defines immutable message envelopes and a protocol
that a separately reviewed signer service could implement in a later release.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Protocol, runtime_checkable


class SignerBoundaryError(ValueError):
    """Raised when an unsigned or signed transaction envelope is malformed."""


@dataclass(frozen=True, slots=True)
class UnsignedTransactionEnvelope:
    """Exact message approved for signing; contains no secret material."""

    intent_id: str
    chain: str
    network: str
    signer_public_key: str
    message: bytes
    message_sha256: str
    policy_hash: str

    def __post_init__(self) -> None:
        for name in ("intent_id", "chain", "network", "signer_public_key", "policy_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise SignerBoundaryError(f"{name} must be a non-empty canonical string")
        if not isinstance(self.message, bytes) or not self.message:
            raise SignerBoundaryError("message must be non-empty immutable bytes")
        if (
            not isinstance(self.message_sha256, str)
            or len(self.message_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.message_sha256)
        ):
            raise SignerBoundaryError("message_sha256 must be a lowercase SHA-256 hex digest")
        if hashlib.sha256(self.message).hexdigest() != self.message_sha256:
            raise SignerBoundaryError("message_sha256 does not match the exact message bytes")


@dataclass(frozen=True, slots=True)
class SignedTransactionEnvelope:
    """Opaque signed output returned by an isolated signer; not submitted here."""

    intent_id: str
    signer_public_key: str
    chain: str
    network: str
    policy_hash: str
    message_sha256: str
    signed_transaction: bytes
    transaction_signature: str

    def __post_init__(self) -> None:
        for name in ("intent_id", "signer_public_key", "chain", "network", "policy_hash", "transaction_signature"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise SignerBoundaryError(f"{name} must be a non-empty canonical string")
        if not isinstance(self.message_sha256, str) or len(self.message_sha256) != 64:
            raise SignerBoundaryError("message_sha256 must be a SHA-256 hex digest")
        if any(char not in "0123456789abcdef" for char in self.message_sha256):
            raise SignerBoundaryError("message_sha256 must be lowercase hexadecimal")
        if not isinstance(self.signed_transaction, bytes) or not self.signed_transaction:
            raise SignerBoundaryError("signed_transaction must be non-empty immutable bytes")


@runtime_checkable
class SignerBoundary(Protocol):
    """Narrow contract for a future signer running outside the trading process.

    Implementations must be supplied and reviewed separately. This repository
    intentionally provides no implementation and never invokes this protocol.
    """

    def sign(self, request: UnsignedTransactionEnvelope) -> SignedTransactionEnvelope:
        """Sign exactly the supplied message or raise; never broadcast it."""
        ...
