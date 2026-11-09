"""Read-only coordinator for primary and secondary Solana discovery feeds."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .dexscreener import DexScreenerDiscovery
from .launch_stream import LaunchStore, LaunchStreamManager


@dataclass(frozen=True)
class DiscoveryCycleResult:
    candidates_received: int
    candidates_stored: int
    healthy_sources: tuple[str, ...]
    source_errors: tuple[tuple[str, str], ...]


class SolanaDiscoveryEngine:
    """Persist PumpPortal stream events and periodically poll DEX Screener leads.

    This stage discovers and records candidate mints only. It deliberately does
    not enrich market data, score candidates, assess risk, or submit trades.
    """

    def __init__(
        self,
        store: LaunchStore,
        secondary_source: DexScreenerDiscovery,
        *,
        primary_stream: LaunchStreamManager | None = None,
        poll_interval_seconds: float = 30.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not 5 <= poll_interval_seconds <= 3600
        ):
            raise ValueError("poll_interval_seconds must be between 5 and 3600")
        self.store = store
        self.secondary_source = secondary_source
        self.primary_stream = primary_stream
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def poll_once(self) -> DiscoveryCycleResult:
        """Fetch the bounded secondary feeds once and persist normalized leads."""
        try:
            batch = self.secondary_source.fetch_candidates()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.store.record_outage(
                reason,
                source=self.secondary_source.name,
                occurred_at_ms=self.clock_ms(),
            )
            return DiscoveryCycleResult(0, 0, (), ((self.secondary_source.name, reason),))

        timestamp = self.clock_ms()
        for source in batch.successful_sources:
            self.store.record_source_healthy(source, occurred_at_ms=timestamp)
        for source, reason in batch.errors:
            self.store.record_outage(
                reason,
                source=source,
                occurred_at_ms=timestamp,
            )
        for candidate in batch.candidates:
            self.store.record_candidate(candidate)

        return DiscoveryCycleResult(
            candidates_received=len(batch.candidates),
            candidates_stored=len(batch.candidates),
            healthy_sources=batch.successful_sources,
            source_errors=batch.errors,
        )

    def run(self, stop_event: threading.Event) -> None:
        """Run secondary polling and, optionally, the primary stream supervisor."""
        primary_thread: threading.Thread | None = None
        if self.primary_stream is not None and not stop_event.is_set():
            primary_thread = threading.Thread(
                target=self.primary_stream.run,
                args=(stop_event,),
                name="veltrix-pumpportal-discovery",
                daemon=True,
            )
            primary_thread.start()

        while not stop_event.is_set():
            self.poll_once()
            if stop_event.wait(self.poll_interval_seconds):
                break

        if primary_thread is not None:
            primary_thread.join(timeout=35.0)
