from types import SimpleNamespace

from veltrix.launch_bundle_acceptance import evaluate_launch_bundle_acceptance
from veltrix.launch_origin import LaunchOrigin
from veltrix.providers.launch import LaunchIndexBatch
from veltrix.launch_bundles import LaunchBuyEvidence


NOW = 2_000_000
TOKEN = "0x" + "aa" * 20
LAUNCH = NOW - 120_000
END = NOW


class OriginResolver:
    def __init__(self, provider="base-rpc-launch-origin"):
        self.provider = provider
    def resolve(self, token, window, *, now_ms):
        assert token == TOKEN
        assert window == 120_000
        assert now_ms == NOW
        return LaunchOrigin(
            "base", token.lower(), LAUNCH, 100, 200, END,
            self.provider, "origin-evidence", True,
        )


class Indexer:
    def __init__(self, batch):
        self.batch = batch
    def index_launch(self, token, launch, end, **kwargs):
        assert token == TOKEN
        assert launch == LAUNCH
        assert end == END
        assert kwargs == {"launch_block": 100, "window_end_block": 200}
        return self.batch


def settings(**overrides):
    values = {
        "launch_window_ms": 120_000,
        "safety_evidence_max_age_ms": 300_000,
        "trusted_launch_origin_providers": ("base-rpc-launch-origin",),
        "trusted_bundle_providers": ("base-rpc-launch",),
        "trusted_funding_providers": ("base-quote-funder",),
        "launch_bundle_evidence_capability_certified": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def batch(*, coverage=True, observations=(), rejected=()):
    return LaunchIndexBatch(
        provider="base-rpc-launch", chain="base", token_address=TOKEN.lower(),
        launch_at_ms=LAUNCH, window_end_ms=END, total_supply=1000,
        supply_source="base-rpc-launch", supply_block_ref="0x64",
        supply_observed_at_ms=NOW, observations=tuple(observations),
        coverage_complete=coverage, scanned_through_ms=END, pages_scanned=1,
        rejected=tuple(rejected), provenance=("logs:100-200",),
    )


def evaluate(data, cfg=None, origin=None):
    return evaluate_launch_bundle_acceptance(
        chain="base", token_address=TOKEN, settings=cfg or settings(),
        indexers={"base": Indexer(data)},
        origin_resolvers={"base": origin or OriginResolver()}, now_ms=NOW,
    )


def test_launch_bundle_acceptance_passes_complete_zero_cluster_scan():
    result = evaluate(batch())
    assert result["reader_acceptance"] == "PASS"
    assert result["bundle"]["bundled_supply_pct"] == 0
    assert result["launch_index"]["coverage_complete"] is True
    assert result["capability_certified"] is False
    assert result["live_execution_enabled"] is False


def test_launch_bundle_acceptance_certifies_only_after_explicit_flag():
    result = evaluate(
        batch(),
        cfg=settings(launch_bundle_evidence_capability_certified=True),
    )
    assert result["reader_acceptance"] == "PASS"
    assert result["capability_certified"] is True


def test_launch_bundle_acceptance_rejects_untrusted_launch_origin():
    try:
        evaluate(batch(), cfg=settings(trusted_launch_origin_providers=()))
    except ValueError as exc:
        assert "not explicitly trusted" in str(exc)
    else:
        raise AssertionError("untrusted launch-origin provider must fail")


def test_launch_bundle_acceptance_rejects_incomplete_scan():
    result = evaluate(batch(coverage=False))
    assert result["reader_acceptance"] == "BLOCKED"
    assert result["bundle"]["bundled_supply_pct"] is None


def test_launch_bundle_acceptance_rejects_untrusted_funding_provenance():
    observation = LaunchBuyEvidence(
        provider="base-rpc-launch", chain="base", token_address=TOKEN.lower(),
        wallet_address="0x" + "11" * 20, funder_address="0x" + "22" * 20,
        transaction_id="0xtx", token_amount=10, observed_at_ms=LAUNCH + 1,
        funder_provider="untrusted-funder", funder_evidence_id="funding-proof",
    )
    result = evaluate(batch(observations=(observation,)))
    assert result["reader_acceptance"] == "BLOCKED"
    assert result["bundle"]["bundled_supply_pct"] is None
