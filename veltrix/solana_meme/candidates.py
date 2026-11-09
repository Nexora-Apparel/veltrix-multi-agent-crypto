"""Canonical, fail-closed Solana meme-coin candidate model.

Discovery adapters may emit untrusted payloads. This module validates token
identity and timestamps before candidates enter downstream intelligence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from solders.pubkey import Pubkey


@dataclass(frozen=True)
class SolanaCandidate:
    mint: str
    source: str
    discovered_at_ms: int
    sources: tuple[str, ...] = ()

    @property
    def chain(self) -> str:
        return "solana"


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
    mint = payload.get("tokenAddress", payload.get("token_address", payload.get("mint")))
    mint = canonical_mint(mint)
    if not isinstance(source, str) or not source.strip():
        raise CandidateRejected("source identity is required")
    if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int) or observed_at_ms <= 0:
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
    for candidate in candidates:
        if not isinstance(candidate, SolanaCandidate):
            raise CandidateRejected("dedupe accepts only normalized SolanaCandidate values")
        current = merged.get(candidate.mint)
        if current is None:
            merged[candidate.mint] = candidate
            continue
        sources = tuple(sorted(set(current.sources or (current.source,)) |
                               set(candidate.sources or (candidate.source,))))
        latest = candidate if candidate.discovered_at_ms > current.discovered_at_ms else current
        merged[candidate.mint] = SolanaCandidate(
            mint=candidate.mint,
            source=latest.source,
            discovered_at_ms=max(current.discovered_at_ms, candidate.discovered_at_ms),
            sources=sources,
        )
    return tuple(sorted(merged.values(), key=lambda item: item.mint))
