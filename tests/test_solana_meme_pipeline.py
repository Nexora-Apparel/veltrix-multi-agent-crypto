from dataclasses import dataclass

from veltrix.solana_meme.pipeline import SolanaMemePipeline


@dataclass
class FakeEnrichmentResult:
    discovered: int = 1
    processed: int = 1
    enriched: int = 1
    failed: int = 0
    deferred: int = 0


@dataclass
class FakeRiskResult:
    discovered: int = 1
    processed: int = 1
    stored: int = 1
    failed: int = 0
    deferred: int = 1


@dataclass
class FakeRefreshResult:
    discovered: int = 0
    processed: int = 0
    refreshed: int = 0
    unchanged: int = 0
    failed: int = 0
    deferred: int = 1


class FakeWorker:
    def __init__(self, database_path, result):
        self.database_path = database_path
        self.result = result
        self.calls = []

    def run_once(self, *, limit):
        self.calls.append(limit)
        return self.result


def test_pipeline_runs_enrichment_before_refresh_and_returns_both_results():
    enrichment = FakeWorker("shared.sqlite3", FakeEnrichmentResult())
    refresh = FakeWorker("shared.sqlite3", FakeRefreshResult())
    pipeline = SolanaMemePipeline(enrichment, refresh)

    result = pipeline.run_once(limit=12)

    assert result.enrichment.enriched == 1
    assert result.refresh.deferred == 1
    assert enrichment.calls == [12]
    assert refresh.calls == [12]


def test_pipeline_rejects_workers_using_different_databases():
    try:
        SolanaMemePipeline(
            FakeWorker("launch.sqlite3", FakeEnrichmentResult()),
            FakeWorker("market.sqlite3", FakeRefreshResult()),
        )
        assert False, "expected mismatched databases to be rejected"
    except ValueError as exc:
        assert "share one SQLite database" in str(exc)


def test_pipeline_rejects_invalid_batch_size():
    pipeline = SolanaMemePipeline(
        FakeWorker("shared.sqlite3", FakeEnrichmentResult()),
        FakeWorker("shared.sqlite3", FakeRefreshResult()),
    )
    for invalid in (0, -1, 501, True):
        try:
            pipeline.run_once(limit=invalid)
            assert False, "expected invalid batch size to be rejected"
        except ValueError:
            pass



def test_pipeline_collects_risk_after_enrichment_and_before_refresh():
    order = []

    class OrderedWorker(FakeWorker):
        def run_once(self, *, limit):
            order.append(self.result.__class__.__name__)
            return super().run_once(limit=limit)

    enrichment = OrderedWorker("shared.sqlite3", FakeEnrichmentResult())
    risk = OrderedWorker("shared.sqlite3", FakeRiskResult())
    refresh = OrderedWorker("shared.sqlite3", FakeRefreshResult())
    pipeline = SolanaMemePipeline(enrichment, refresh, risk, risk_batch_size=3)

    result = pipeline.run_once(limit=12)

    assert result.risk.stored == 1
    assert risk.calls == [3]
    assert order == ["FakeEnrichmentResult", "FakeRiskResult", "FakeRefreshResult"]


def test_pipeline_rejects_risk_worker_on_separate_database():
    try:
        SolanaMemePipeline(
            FakeWorker("shared.sqlite3", FakeEnrichmentResult()),
            FakeWorker("shared.sqlite3", FakeRefreshResult()),
            FakeWorker("risk.sqlite3", FakeRiskResult()),
        )
        assert False, "expected mismatched risk database to be rejected"
    except ValueError as exc:
        assert "share one SQLite database" in str(exc)


@dataclass
class FakeDeveloperResult:
    discovered: int = 1
    processed: int = 1
    stored: int = 1
    incomplete: int = 0
    failed: int = 0
    deferred: int = 1


def test_pipeline_runs_developer_ancestry_after_risk_and_before_refresh():
    order = []

    class OrderedWorker(FakeWorker):
        def run_once(self, *, limit):
            order.append(self.result.__class__.__name__)
            return super().run_once(limit=limit)

    enrichment = OrderedWorker("shared.sqlite3", FakeEnrichmentResult())
    risk = OrderedWorker("shared.sqlite3", FakeRiskResult())
    developer = OrderedWorker("shared.sqlite3", FakeDeveloperResult())
    refresh = OrderedWorker("shared.sqlite3", FakeRefreshResult())
    pipeline = SolanaMemePipeline(
        enrichment, refresh, risk, developer,
        risk_batch_size=4, developer_batch_size=2,
    )
    result = pipeline.run_once(limit=12)

    assert result.developer.stored == 1
    assert developer.calls == [2]
    assert order == [
        "FakeEnrichmentResult", "FakeRiskResult",
        "FakeDeveloperResult", "FakeRefreshResult",
    ]


def test_pipeline_rejects_developer_worker_on_separate_database():
    try:
        SolanaMemePipeline(
            FakeWorker("shared.sqlite3", FakeEnrichmentResult()),
            FakeWorker("shared.sqlite3", FakeRefreshResult()),
            None,
            FakeWorker("developer.sqlite3", FakeDeveloperResult()),
        )
        assert False, "expected mismatched developer database to be rejected"
    except ValueError as exc:
        assert "share one SQLite database" in str(exc)
