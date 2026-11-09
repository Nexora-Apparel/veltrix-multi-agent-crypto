"""Run the read-only Solana meme discovery and market-data pipeline.

Usage: python -m veltrix.solana_meme.runtime --database veltrix_solana.sqlite3
"""
from __future__ import annotations

import argparse
import threading
import time

from .enrichment import CandidateEnrichmentWorker
from .launch_stream import LaunchStore, LaunchStreamManager
from .market_data import SolanaMarketData
from .market_refresh import MarketRefreshWorker
from .market_store import MarketSnapshotStore
from .pipeline import SolanaMemePipeline
from .pumpportal import PumpPortalDiscovery


def run_service(
    *,
    database: str,
    poll_seconds: float = 15.0,
    batch_size: int = 25,
    duration_seconds: float = 0,
) -> None:
    if poll_seconds < 5 or poll_seconds > 300:
        raise ValueError("poll_seconds must be between 5 and 300")
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if duration_seconds < 0:
        raise ValueError("duration_seconds cannot be negative")

    launches = LaunchStore(database)
    markets = MarketSnapshotStore(database)
    market_data = SolanaMarketData()
    enrichment = CandidateEnrichmentWorker(launches, market_data, markets)
    refresh = MarketRefreshWorker(launches, market_data, markets)
    pipeline = SolanaMemePipeline(enrichment, refresh)

    stop_event = threading.Event()
    stream = LaunchStreamManager(PumpPortalDiscovery(), launches)
    stream_thread = threading.Thread(
        target=stream.run, args=(stop_event,), name="veltrix-solana-launch-feed", daemon=True,
    )
    stream_thread.start()

    deadline = time.monotonic() + duration_seconds if duration_seconds else None
    print("VELTRIX Solana read-only discovery and market pipeline started.")
    print("No wallet, signer, paper broker, or live execution is connected.")
    try:
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                result = pipeline.run_once(limit=batch_size)
                print(
                    "Pipeline cycle: "
                    f"launches discovered for enrichment={result.enrichment.discovered}, "
                    f"enriched={result.enrichment.enriched}, "
                    f"enrichment failures={result.enrichment.failed}, "
                    f"market refreshes={result.refresh.refreshed}, "
                    f"unchanged={result.refresh.unchanged}, "
                    f"refresh failures={result.refresh.failed}, "
                    f"refresh jobs deferred={result.refresh.deferred}"
                )
            except Exception as exc:
                launches.record_event(
                    "market_pipeline_cycle_error",
                    details={"error_type": type(exc).__name__, "message": str(exc)[:240]},
                )
                print(f"Pipeline cycle failed safely: {type(exc).__name__}")
            remaining = poll_seconds
            if deadline is not None:
                remaining = min(remaining, max(0.0, deadline - time.monotonic()))
            if stop_event.wait(remaining):
                break
    except KeyboardInterrupt:
        print("Shutdown requested.")
    finally:
        stop_event.set()
        stream_thread.join(timeout=10)
        print(
            "VELTRIX pipeline stopped. "
            f"launch candidates stored={launches.candidate_count()}, "
            f"market snapshots stored={markets.count()}, "
            f"active feed outages={launches.active_outage_count()}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VELTRIX read-only Solana meme-coin discovery and market pipeline"
    )
    parser.add_argument("--database", default="veltrix_solana_meme.sqlite3")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--duration-seconds", type=float, default=0,
                        help="0 runs until interrupted")
    args = parser.parse_args()
    run_service(
        database=args.database,
        poll_seconds=args.poll_seconds,
        batch_size=args.batch_size,
        duration_seconds=args.duration_seconds,
    )


if __name__ == "__main__":
    main()
