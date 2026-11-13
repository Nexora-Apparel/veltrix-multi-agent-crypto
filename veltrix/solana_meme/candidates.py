"""Canonical, fail-closed Solana meme-coin candidate model.

Discovery adapters may emit untrusted payloads. This module validates token
identity and timestamps before candidates enter downstream intelligence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from solders.pubkey import Pubkey


class CandidateRejected(ValueError):
    """Raised when an untrusted discovery payload cannot be normalized."""


def canonical_mint(value: Any) -> str:
    """Return canonical base58 Solana public key or reject the value."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise CandidateRejected("mint must be a non-empty, whitespace-free string")
    try:
        key = Pubkey.from_string(value)
    except (ValueError, TypeError):
        raise CandidateRejected("mint is not a valid Solana public key") from None
    canonical = str(key)
    if canonical != value:
        raise CandidateRejected("mint is not in canonical base58 form")
    return canonical


@dataclass(frozen=True)
class SolanaCandidate:
    mint: str
    source: str
    discovered_at_ms: int
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Enforce invariants on the public constructor, not only on adapters.
        mint = canonical_mint(self.mint)
        if not isinstance(self.source, str) or not self.source.strip():
            raise CandidateRejected("source identity is required")
        if (
            isinstance(self.discovered_at_ms, bool)
            or not isinstance(self.discovered_at_ms, int)
            or self.discovered_at_ms <= 0
        ):
            raise CandidateRejected("discovered_at_ms must be a positive integer")
        if not isinstance(self.sources, tuple):
            raise CandidateRejected("sources must be a tuple of non-empty strings")

        source = self.source.strip()
        normalized_sources = {source}
        for item in self.sources:
            if not isinstance(item, str) or not item.strip():
                raise CandidateRejected("sources must be a tuple of non-empty strings")
            normalized_sources.add(item.strip())

        # Frozen instances are canonicalized at construction. The primary
        # source is always represented in the complete provenance set.
        object.__setattr__(self, "mint", mint)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "sources", tuple(sorted(normalized_sources)))

    @property
    def chain(self) -> str:
        return "solana"


def validate_candidate(candidate: Any) -> SolanaCandidate:
    """Reconstruct and revalidate a candidate at every persistence boundary.

    Frozen dataclasses can still be forged with object.__new__ or mutated via
    object.__setattr__. Never rely on isinstance() as proof of valid fields.
    """
    if not isinstance(candidate, SolanaCandidate):
        raise CandidateRejected("only SolanaCandidate values can be accepted")
    try:
        return SolanaCandidate(
            mint=candidate.mint,
            source=candidate.source,
            discovered_at_ms=candidate.discovered_at_ms,
            sources=candidate.sources,
        )
    except CandidateRejected:
        raise
    except (AttributeError, TypeError, ValueError):
        raise CandidateRejected("candidate object failed canonical validation") from None


def _validated_chain(payload: dict[str, Any]) -> None:
    """Require explicit, consistent Solana chain metadata."""
    has_chain_id = "chainId" in payload
    has_chain = "chain" in payload
    if not has_chain_id and not has_chain:
        raise CandidateRejected("explicit Solana chain metadata is required")

    values = []
    if has_chain_id:
        values.append(payload["chainId"])
    if has_chain:
        values.append(payload["chain"])

    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CandidateRejected("chain metadata must contain non-empty strings")

    normalized = {value.strip().lower() for value in values}
    if normalized != {"solana"}:
        raise CandidateRejected("only consistent Solana chain metadata is accepted")


def normalize_candidate(
    payload: dict[str, Any],
    *,
    source: str,
    observed_at_ms: int,
) -> SolanaCandidate:
    """Normalize supported feed shapes without trusting their chain labels."""
    if not isinstance(payload, dict):
        raise CandidateRejected("candidate payload must be an object")
    _validated_chain(payload)

    address_fields = [
        key for key in ("tokenAddress", "token_address", "mint") if key in payload
    ]
    if not address_fields:
        raise CandidateRejected("candidate mint field is required")
    canonical_values = [canonical_mint(payload[key]) for key in address_fields]
    if len(set(canonical_values)) != 1:
        raise CandidateRejected("candidate mint fields conflict")
    mint = canonical_values[0]

    if not isinstance(source, str) or not source.strip():
        raise CandidateRejected("source identity is required")
    if (
        isinstance(observed_at_ms, bool)
        or not isinstance(observed_at_ms, int)
        or observed_at_ms <= 0
    ):
        raise CandidateRejected("observed_at_ms must be a positive integer")

    normalized_source = source.strip()
    return SolanaCandidate(
        mint=mint,
        source=normalized_source,
        discovered_at_ms=observed_at_ms,
        sources=(normalized_source,),
    )


def deduplicate_candidates(
    candidates: Iterable[SolanaCandidate],
) -> tuple[SolanaCandidate, ...]:
    """Merge duplicate mint observations, preserving latest time and all sources."""
    merged: dict[str, SolanaCandidate] = {}
    for raw_candidate in candidates:
        candidate = validate_candidate(raw_candidate)
        current = merged.get(candidate.mint)
        if current is None:
            merged[candidate.mint] = candidate
            continue
        sources = tuple(sorted(set(current.sources) | set(candidate.sources)))
        latest = candidate if candidate.discovered_at_ms > current.discovered_at_ms else current
        merged[candidate.mint] = SolanaCandidate(
            mint=candidate.mint,
            source=latest.source,
            discovered_at_ms=max(current.discovered_at_ms, candidate.discovered_at_ms),
            sources=sources,
        )
    return tuple(sorted(merged.values(), key=lambda item: item.mint))
