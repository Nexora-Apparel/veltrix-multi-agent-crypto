"""Long-running entry point for the read-only Solana discovery worker.

Run with:
    python -m veltrix.solana_meme.worker

Configuration:
    VELTRIX_SOLANA_DISCOVERY_DB=data/solana_discovery.sqlite3
    VELTRIX_SOLANA_DISCOVERY_POLL_SECONDS=30
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any

from .dexscreener import DexScreenerDiscovery
from .discovery import SolanaDiscoveryEngine
from .launch_stream import LaunchStore, LaunchStreamManager
from .pumpportal import PumpPortalDiscovery

LOGGER = logging.getLogger("veltrix.solana_discovery")


def build_engine(
    *,
    database_path: str | Path | None = None,
    poll_interval_seconds: float | None = None,
    http_session: Any | None = None,
    websocket_factory: Any | None = None,
) -> SolanaDiscoveryEngine:
    """Construct the production discovery runtime without opening network sockets."""
    db_path = database_path or os.getenv(
        "VELTRIX_SOLANA_DISCOVERY_DB",
        "data/solana_discovery.sqlite3",
    )
    raw_interval = (
        poll_interval_seconds
        if poll_interval_seconds is not None
        else os.getenv("VELTRIX_SOLANA_DISCOVERY_POLL_SECONDS", "30")
    )
    try:
        interval = float(raw_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("VELTRIX_SOLANA_DISCOVERY_POLL_SECONDS must be numeric") from exc

    store = LaunchStore(db_path)
    primary_adapter = PumpPortalDiscovery(websocket_factory=websocket_factory)
    primary_manager = LaunchStreamManager(primary_adapter, store)
    secondary_adapter = DexScreenerDiscovery(session=http_session)
    return SolanaDiscoveryEngine(
        store,
        secondary_adapter,
        primary_stream=primary_manager,
        poll_interval_seconds=interval,
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("VELTRIX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        LOGGER.info("received shutdown signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    engine = build_engine()
    LOGGER.info(
        "starting read-only Solana discovery worker; poll_interval_seconds=%s",
        engine.poll_interval_seconds,
    )
    engine.run(stop_event)
    LOGGER.info("Solana discovery worker stopped")


if __name__ == "__main__":
    main()
