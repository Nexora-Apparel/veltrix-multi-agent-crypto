"""Bounded provider-to-store OHLCV ingestion service."""
from __future__ import annotations
from dataclasses import dataclass
from .candle_store import SolanaCandleStore
from .geckoterminal_candles import GeckoTerminalCandleProvider

@dataclass(frozen=True)
class CandleIngestionResult:
    fetched: int
    inserted: int
    duplicates: int
    latest_observed_at_ms: int | None
    source: str

class SolanaCandleIngestion:
    def __init__(self, store: SolanaCandleStore, provider: GeckoTerminalCandleProvider | None = None) -> None:
        self.store=store
        self.provider=provider or GeckoTerminalCandleProvider()

    def refresh(self, mint: str, pair_address: str, interval: str, *, limit: int = 500) -> CandleIngestionResult:
        candles=self.provider.fetch_candles(mint,pair_address,interval,limit=limit)
        inserted=sum(1 for candle in candles if self.store.record(candle))
        return CandleIngestionResult(
            fetched=len(candles),inserted=inserted,duplicates=len(candles)-inserted,
            latest_observed_at_ms=max((c.observed_at_ms for c in candles),default=None),
            source=self.provider.name,
        )
