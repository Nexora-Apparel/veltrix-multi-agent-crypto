"""Run the read-only Solana meme discovery and market-data pipeline.

Usage: python -m veltrix.solana_meme.runtime --database veltrix_solana.sqlite3
"""
from __future__ import annotations

import argparse
import math
import threading
import time

from .enrichment import CandidateEnrichmentWorker
from .launch_stream import LaunchStore, LaunchStreamManager
from .market_data import SolanaMarketData
from .market_refresh import MarketRefreshWorker
from .market_store import MarketSnapshotStore
from .pipeline import SolanaMemePipeline
from .pumpportal import PumpPortalDiscovery
from .risk_store import SolanaRiskEvidenceStore
from .risk_worker import SolanaRiskEvidenceWorker
from .developer_store import DeveloperConcentrationStore
from .developer_worker import SolanaDeveloperEvidenceWorker
from .bundle_store import LaunchBundleEvidenceStore
from .sellability_store import SolanaSellabilityEvidenceStore
from .sell_simulation_store import SolanaSellSimulationStore
from .simulation_preflight_store import SolanaSimulationPreflightStore
from ..config import settings as load_settings
from ..providers.factory import (
    build_solana_risk_evidence_collector,
    build_solana_developer_evidence_worker,
    build_solana_launch_bundle_worker,
    build_solana_sellability_worker,
    build_solana_sell_simulation_worker,
)


def run_service(
    *,
    database: str,
    poll_seconds: float = 15.0,
    batch_size: int = 25,
    duration_seconds: float = 0,
) -> None:
    if (not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool)
            or not math.isfinite(poll_seconds) or poll_seconds < 5 or poll_seconds > 300):
        raise ValueError("poll_seconds must be finite and between 5 and 300")
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if (not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool)
            or not math.isfinite(duration_seconds) or duration_seconds < 0):
        raise ValueError("duration_seconds must be finite and non-negative")
    if not isinstance(database, str) or not database.strip():
        raise ValueError("database path is required")

    launches = LaunchStore(database)
    markets = MarketSnapshotStore(database)
    market_data = SolanaMarketData()
    enrichment = CandidateEnrichmentWorker(launches, market_data, markets)
    refresh = MarketRefreshWorker(launches, market_data, markets)
    configured = load_settings()
    risk_collector = build_solana_risk_evidence_collector(configured)
    risk_worker = None
    risk_store = None
    if risk_collector is not None:
        risk_store = SolanaRiskEvidenceStore(database)
        risk_worker = SolanaRiskEvidenceWorker(launches, risk_collector, risk_store)
    developer_store = DeveloperConcentrationStore(database)
    developer_worker = build_solana_developer_evidence_worker(
        configured, launches, developer_store,
    )
    bundle_store = LaunchBundleEvidenceStore(database)
    bundle_worker = build_solana_launch_bundle_worker(
        configured, launches, bundle_store,
    )
    sellability_store = SolanaSellabilityEvidenceStore(database)
    sellability_worker = build_solana_sellability_worker(
        configured, launches, risk_store, sellability_store, market_store=markets,
    )
    sell_simulation_store = SolanaSellSimulationStore(database)
    simulation_preflight_store = SolanaSimulationPreflightStore(database)
    sell_simulation_worker = build_solana_sell_simulation_worker(
        configured, database, sell_simulation_store,
        preflight_store=simulation_preflight_store,
    )
    pipeline = SolanaMemePipeline(
        enrichment, refresh, risk_worker, developer_worker, bundle_worker,
        sellability_worker=sellability_worker,
        sell_simulation_worker=sell_simulation_worker,
    )

    stop_event = threading.Event()
    stream = LaunchStreamManager(PumpPortalDiscovery(), launches)
    stream_thread = threading.Thread(
        target=stream.run, args=(stop_event,), name="veltrix-solana-launch-feed", daemon=True,
    )
    stream_thread.start()

    deadline = time.monotonic() + duration_seconds if duration_seconds else None
    print("VELTRIX Solana read-only discovery and market pipeline started.")
    print("No wallet, signer, paper broker, or live execution is connected.")
    if risk_worker is None:
        print("Risk evidence collection is disabled: configure existing GOPLUS_ACCESS_TOKEN and SOLANA_RPC_URL.")
    if developer_worker is None:
        print("Developer ancestry collection is disabled until signed-root endpoint/key and all three provider trust lists are configured.")
    if bundle_worker is None:
        print("Launch-bundle analysis is disabled until Helius/Solana RPC and launch, bundle, and funding provider trust lists are configured.")
    if sellability_worker is None:
        print("Sell-route evidence is disabled: set SOLANA_SELLABILITY_ENABLED=true and configure GoPlus plus Solana RPC evidence. Jupiter keyless quotes require no new secret.")
    if sell_simulation_worker is None:
        print("Unsigned sell simulation is disabled: it requires explicit test-wallet public key and mint/raw-amount cases; no private key is accepted.")
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
                    + (
                        f", risk reports stored={result.risk.stored}, risk failures={result.risk.failed}"
                        if result.risk is not None else ", risk collection disabled"
                    )
                    + (
                        f", developer reports stored={result.developer.stored}, incomplete={result.developer.incomplete}, failures={result.developer.failed}"
                        if result.developer is not None else ", developer ancestry disabled"
                    )
                    + (
                        f", bundle reports stored={result.bundle.stored}, complete={result.bundle.complete}, incomplete={result.bundle.incomplete}, failures={result.bundle.failed}"
                        if result.bundle is not None else ", bundle analysis disabled"
                    )
                    + (
                        f", sell routes observed={result.sellability.route_observed}, no routes={result.sellability.no_route}, unverified={result.sellability.unverified}, failures={result.sellability.failed}"
                        if result.sellability is not None else ", sell-route evidence disabled"
                    )
                    + (
                        f", sell simulations succeeded={result.sell_simulation.succeeded}, simulation failures={result.sell_simulation.simulation_failed}, preflight blocked={result.sell_simulation.preflight_blocked}, provider failures={result.sell_simulation.provider_failed}"
                        if result.sell_simulation is not None else ", sell simulation disabled"
                    )
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
            f"risk reports stored={risk_store.count() if risk_store is not None else 0}, "
            f"developer reports stored={developer_store.count()}, "
            f"bundle reports stored={bundle_store.count()}, "
            f"sell simulations stored={sell_simulation_store.count()}, "
            f"simulation preflights stored={simulation_preflight_store.count()}, "
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
