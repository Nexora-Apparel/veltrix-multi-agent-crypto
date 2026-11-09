"""Read-only DEX Screener discovery leads for Solana.

Profiles and boosts are candidate-discovery feeds only. They are not evidence
of organic demand, token safety, liquidity quality, or a trade recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import requests

from .candidates import SolanaCandidate, deduplicate_candidates, normalize_candidate


class DexScreenerError(RuntimeError):
    """Raised when a DEX Screener discovery endpoint cannot be read safely."""


@dataclass(frozen=True)
class DexScreenerBatch:
    candidates: tuple[SolanaCandidate, ...]
    successful_sources: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]


class DexScreenerDiscovery:
    name = "dexscreener"
    base_url = "https://api.dexscreener.com"
    endpoints = (
        ("/token-profiles/latest/v1", "dexscreener-latest-profile"),
        ("/token-boosts/latest/v1", "dexscreener-latest-boost"),
    )

    def __init__(
        self,
        *,
        session: Any | None = None,
        timeout_seconds: float = 10.0,
        base_url: str | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.clock_ms = clock_ms or __import__("time").time_ns
        self._clock_ms = clock_ms or (lambda: __import__("time").time_ns() // 1_000_000)

    @staticmethod
    def _entries(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        raise DexScreenerError("unexpected response shape; expected object or array")

    def _fetch_endpoint(self, path: str, source: str) -> tuple[SolanaCandidate, ...]:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise DexScreenerError(f"{source} request failed: {type(exc).__name__}: {exc}") from exc

        candidates = []
        for item in self._entries(payload):
            chain = item.get("chainId")
            if not isinstance(chain, str) or chain.strip().lower() != "solana":
                continue
            try:
                candidate = normalize_candidate(
                    item,
                    source=source,
                    observed_at_ms=self._clock_ms(),
                )
            except ValueError:
                continue
            candidates.append(candidate)
        return deduplicate_candidates(candidates)

    def fetch_candidates(self) -> DexScreenerBatch:
        """Fetch both latest lead feeds, preserving partial success per endpoint."""
        candidates: list[SolanaCandidate] = []
        successful: list[str] = []
        errors: list[tuple[str, str]] = []
        for path, source in self.endpoints:
            try:
                candidates.extend(self._fetch_endpoint(path, source))
                successful.append(source)
            except DexScreenerError as exc:
                errors.append((source, str(exc)))
        return DexScreenerBatch(
            candidates=deduplicate_candidates(candidates),
            successful_sources=tuple(successful),
            errors=tuple(errors),
        )
