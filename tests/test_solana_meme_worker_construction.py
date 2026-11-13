import pytest

from veltrix.solana_meme.discovery import SolanaDiscoveryEngine
from veltrix.solana_meme.worker import build_engine


def test_preview_worker_construction_is_read_only_and_does_not_open_networks(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.delenv("VELTRIX_DEPLOYMENT_TARGET", raising=False)
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("LIVE_TRADING", "false")

    network_attempts = []

    def forbidden_websocket(*args, **kwargs):
        network_attempts.append(("websocket", args, kwargs))
        raise AssertionError("worker construction must not open a websocket")

    class UnusedHttpSession:
        def get(self, *args, **kwargs):
            network_attempts.append(("http", args, kwargs))
            raise AssertionError("worker construction must not make HTTP requests")

    database = tmp_path / "preview" / "discovery.sqlite3"
    session = UnusedHttpSession()
    engine = build_engine(
        database_path=database,
        poll_interval_seconds=30,
        http_session=session,
        websocket_factory=forbidden_websocket,
    )

    assert isinstance(engine, SolanaDiscoveryEngine)
    assert engine.store.database_path == str(database)
    assert engine.poll_interval_seconds == 30
    assert engine.secondary_source.session is session
    assert engine.primary_stream is not None
    assert network_attempts == []
    assert engine.store.candidate_count() == 0


@pytest.mark.parametrize(
    "name,value",
    [
        ("VELTRIX_DEPLOYMENT_TARGET", "production"),
        ("TELEGRAM_ENABLED", "true"),
        ("SCHEDULER_ENABLED", "true"),
        ("LIVE_TRADING", "true"),
    ],
)
def test_discovery_worker_builder_rejects_release_activation(
    tmp_path, monkeypatch, name, value
):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError):
        build_engine(database_path=tmp_path / f"blocked-{name}.sqlite3")
