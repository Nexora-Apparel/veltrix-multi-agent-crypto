import pytest

from veltrix.solana_meme.dexscreener import DexScreenerBatch
from veltrix.solana_meme.smoke import run_smoke


class FakePumpPortal:
    def __init__(self, error=None):
        self.error = error
        self.probed = False

    def probe_connection(self):
        self.probed = True
        if self.error:
            raise self.error


class FakeDexScreener:
    def __init__(self, batch):
        self.batch = batch

    def fetch_candidates(self):
        return self.batch


def test_smoke_checks_both_sources_and_returns_partial_failures():
    pump = FakePumpPortal()
    dex = FakeDexScreener(DexScreenerBatch(
        candidates=(),
        successful_sources=("dexscreener-latest-profile",),
        errors=(("dexscreener-latest-boost", "HTTP 503"),),
    ))

    result = run_smoke(pumpportal=pump, dexscreener=dex)

    assert pump.probed
    assert result.pumpportal_connected is True
    assert result.dexscreener_healthy_sources == ("dexscreener-latest-profile",)
    assert result.dexscreener_errors == (("dexscreener-latest-boost", "HTTP 503"),)


def test_smoke_fails_when_all_dexscreener_sources_are_unavailable():
    dex = FakeDexScreener(DexScreenerBatch(
        candidates=(),
        successful_sources=(),
        errors=(
            ("dexscreener-latest-profile", "HTTP 503"),
            ("dexscreener-latest-boost", "HTTP 503"),
        ),
    ))

    with pytest.raises(RuntimeError, match="all DEX Screener"):
        run_smoke(pumpportal=FakePumpPortal(), dexscreener=dex)


def test_smoke_fails_if_pumpportal_handshake_fails():
    with pytest.raises(ConnectionError):
        run_smoke(
            pumpportal=FakePumpPortal(ConnectionError("socket unavailable")),
            dexscreener=FakeDexScreener(DexScreenerBatch((), (), ())),
        )
