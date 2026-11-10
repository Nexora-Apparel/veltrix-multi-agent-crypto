from types import SimpleNamespace

from veltrix.solana_meme import pipeline_once


def test_ephemeral_entrypoint_returns_success_only_for_succeeded_cycle(monkeypatch):
    monkeypatch.setattr(
        pipeline_once, "build_runtime",
        lambda: SimpleNamespace(
            run_once=lambda: SimpleNamespace(
                job={"status": "SUCCEEDED", "id": "job-1", "executed": True}
            )
        ),
    )
    assert pipeline_once.main() == 0


def test_ephemeral_entrypoint_fails_on_degraded_cycle(monkeypatch):
    monkeypatch.setattr(
        pipeline_once, "build_runtime",
        lambda: SimpleNamespace(
            run_once=lambda: SimpleNamespace(
                job={"status": "DEGRADED", "id": "job-2", "executed": False}
            )
        ),
    )
    assert pipeline_once.main() == 1
