"""One-shot provider connectivity check for Phase 03 discovery.

Run with:
    python -m veltrix.solana_meme.smoke

This opens one short-lived PumpPortal websocket, sends only subscribeNewToken,
checks the two public DEX Screener discovery endpoints, then exits. It does not
start the long-running worker or perform any trading action.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .dexscreener import DexScreenerDiscovery
from .pumpportal import PumpPortalDiscovery


@dataclass(frozen=True)
class DiscoverySmokeResult:
    pumpportal_connected: bool
    dexscreener_healthy_sources: tuple[str, ...]
    dexscreener_candidate_count: int
    dexscreener_errors: tuple[tuple[str, str], ...]


def run_smoke(
    *,
    pumpportal: PumpPortalDiscovery | None = None,
    dexscreener: DexScreenerDiscovery | None = None,
) -> DiscoverySmokeResult:
    """Check provider connectivity without launching the continuous worker."""
    pump = pumpportal or PumpPortalDiscovery(
        endpoint=os.getenv("VELTRIX_PUMPPORTAL_WS_ENDPOINT") or None,
        timeout_seconds=8,
    )
    dex = dexscreener or DexScreenerDiscovery(timeout_seconds=10)

    pump.probe_connection()
    batch = dex.fetch_candidates()
    if not batch.successful_sources:
        details = "; ".join(f"{source}: {reason}" for source, reason in batch.errors)
        raise RuntimeError(f"all DEX Screener discovery endpoints failed: {details}")

    return DiscoverySmokeResult(
        pumpportal_connected=True,
        dexscreener_healthy_sources=batch.successful_sources,
        dexscreener_candidate_count=len(batch.candidates),
        dexscreener_errors=batch.errors,
    )


def main() -> None:
    result = run_smoke()
    print("PumpPortal websocket handshake and new-token subscription: PASS")
    print("DEX Screener healthy sources: " + ", ".join(result.dexscreener_healthy_sources))
    print(f"DEX Screener normalized Solana candidates: {result.dexscreener_candidate_count}")
    if result.dexscreener_errors:
        print(f"DEX Screener partial source failures: {len(result.dexscreener_errors)}")


if __name__ == "__main__":
    main()
