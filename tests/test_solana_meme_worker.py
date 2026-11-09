import pytest

from veltrix.solana_meme.worker import build_engine


class FakeHttpSession:
    pass


def test_build_engine_uses_configured_database_and_poll_interval(tmp_path):
    database = tmp_path / "runtime" / "solana.sqlite3"
    engine = build_engine(
        database_path=database,
        poll_interval_seconds=15,
        http_session=FakeHttpSession(),
        websocket_factory=lambda *args, **kwargs: None,
    )

    assert engine.store.database_path == str(database)
    assert engine.poll_interval_seconds == 15
    assert engine.primary_stream is not None
    assert engine.secondary_source.session.__class__ is FakeHttpSession


def test_build_engine_reads_environment_configuration(tmp_path, monkeypatch):
    database = tmp_path / "env.sqlite3"
    monkeypatch.setenv("VELTRIX_SOLANA_DISCOVERY_DB", str(database))
    monkeypatch.setenv("VELTRIX_SOLANA_DISCOVERY_POLL_SECONDS", "45")

    engine = build_engine(websocket_factory=lambda *args, **kwargs: None)

    assert engine.store.database_path == str(database)
    assert engine.poll_interval_seconds == 45


@pytest.mark.parametrize("interval", ["invalid", "4", "3601", "nan", "inf"])
def test_build_engine_rejects_invalid_poll_interval(tmp_path, monkeypatch, interval):
    monkeypatch.setenv("VELTRIX_SOLANA_DISCOVERY_DB", str(tmp_path / "bad.sqlite3"))
    monkeypatch.setenv("VELTRIX_SOLANA_DISCOVERY_POLL_SECONDS", interval)

    with pytest.raises(ValueError):
        build_engine(websocket_factory=lambda *args, **kwargs: None)
