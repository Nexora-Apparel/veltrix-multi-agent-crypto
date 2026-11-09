from veltrix.monitor import MarketMonitor
from veltrix.providers.base import MarketEvent
from veltrix.discovery import TokenCandidate

class FakeProvider:
    def fetch_token_pairs(self, chain, addresses):
        return [MarketEvent(chain, addresses[0], "pair", "TST", 1.0, 5000, 2000, 1.5, 1000, None, "fake")]

class FakeDiscovery:
    def discover(self, chains):
        return [TokenCandidate("base", "discovered-token", "fake-feed", 1000)]

def test_monitor_persists_market_observations(tmp_path):
    db = str(tmp_path / "monitor.db")
    monitor = MarketMonitor({"base": ["token-address"]}, db, FakeProvider())
    events = monitor.poll_once()
    assert len(events) == 1
    import sqlite3
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM events WHERE event='MARKET_OBSERVATION'").fetchone()[0]
    assert count == 1

def test_monitor_discovers_and_fetches_candidate(tmp_path):
    db = str(tmp_path / "discovery.db")
    monitor = MarketMonitor({}, db, FakeProvider(), FakeDiscovery(), True, ("base",), 5)
    events = monitor.poll_once()
    assert len(events) == 1 and events[0].token_address == "discovered-token"
    import sqlite3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='DISCOVERY_CANDIDATE'").fetchone()[0] == 1

def test_monitor_builds_scout_context_only_after_minimum_history(tmp_path):
    from veltrix.providers.base import MarketEvent
    db = str(tmp_path / "signals.db")
    monitor = MarketMonitor({"base": ["token-address"]}, db, FakeProvider())
    import time
    now_ms = int(time.time() * 1000)
    for i in range(5):
        monitor.ledger.record_market_event(MarketEvent(
            "base", "token-address", f"old-{i}", "TST", 1.0, 5000,
            1000, 0.5, now_ms - (5 - i) * 60_000, None, "fake"
        ))
    monitor.provider = type("SpikeProvider", (), {
        "fetch_token_pairs": lambda self, chain, addresses: [
            MarketEvent(chain, addresses[0], "new", "TST", 1.1, 6000,
                        3000, 2.0, int(time.time() * 1000), None, "fake")
        ]
    })()
    monitor.poll_once()
    assert len(monitor.signal_contexts) == 1
    context = monitor.signal_contexts[0]
    assert context["volume_spike"] is True
    assert context["volume_multiple"] == 3.0
    assert context["signal_quality"] == "heuristic"


def test_monitor_does_not_emit_volume_signal_without_history(tmp_path):
    monitor = MarketMonitor({"base": ["token-address"]}, str(tmp_path / "empty.db"), FakeProvider())
    monitor.poll_once()
    assert monitor.signal_contexts == []


def test_monitor_collects_verified_wallet_clusters_without_opening_positions(tmp_path):
    import time
    from veltrix.providers.coordinator import CrossChainIntelligenceCoordinator
    from veltrix.providers.intelligence import SmartWalletObservation, VerifiedIntelligenceAdapters

    class WalletProvider:
        provider_id = "alchemy"
        supported_chains = frozenset({"base", "bsc"})
        def fetch_wallet_observations(self, chain, token_addresses, since_ms):
            now = int(time.time() * 1000)
            return tuple(SmartWalletObservation(
                provider="alchemy", chain=chain, token_address="token-address",
                wallet_address=wallet, side="BUY", notional_usd=25,
                transaction_id=tx, observed_at_ms=now - 1_000,
            ) for wallet, tx in [("0xwallet-a", "tx-a"), ("0xwallet-b", "tx-b")])

    adapters = VerifiedIntelligenceAdapters(trusted_wallet_providers=["alchemy"])
    coordinator = CrossChainIntelligenceCoordinator(
        [WalletProvider()], adapters, trusted_wallets=["0xwallet-a", "0xwallet-b"]
    )
    monitor = MarketMonitor(
        {"base": ["token-address"]}, str(tmp_path / "wallet-monitor.db"),
        FakeProvider(), broker=None, wallet_intelligence=coordinator,
    )
    monitor.poll_once()
    result = monitor.last_wallet_intelligence["base"]
    assert len(result.batch.accepted) == 2
    assert len(result.clusters) == 1
    assert monitor.ledger.snapshot()["open_positions"] == []


def test_discovered_launch_is_resolved_and_submitted_to_composite_gate(tmp_path):
    from types import SimpleNamespace

    class OriginResolver:
        provider_id = "base-rpc-launch-origin"
        def resolve(self, token, window):
            assert token == "discovered-token"
            assert window == 120_000
            return SimpleNamespace(
                complete=True, chain="base", launch_at_ms=1_700_000_000_000,
                launch_block=100, window_end_block=120,
                provider=self.provider_id, evidence_id="origin-evidence",
            )

    class LaunchService:
        launch_window_ms = 120_000
        def __init__(self):
            self.calls = []
        def evaluate(self, **kwargs):
            self.calls.append(kwargs)
            return (
                SimpleNamespace(coverage_complete=False, bundled_supply_pct=None),
                SimpleNamespace(passed=False, composite_verified=False,
                                reasons=("liquidity evidence missing",)),
            )

    service = LaunchService()

    class DeveloperEvidence:
        def __init__(self):
            self.calls = []
        def evaluate(self, **kwargs):
            self.calls.append(kwargs)
            report = SimpleNamespace(
                coverage_complete=False, dev_hold_pct=None, evidence_id=None,
                root_provider=None,
            )
            safety = SimpleNamespace(passed=False)
            return SimpleNamespace(
                report=report, safety_report=safety,
                audit={"reasons": ["signed root provider not configured"]},
            )

    developer_evidence = DeveloperEvidence()
    monitor = MarketMonitor(
        {}, str(tmp_path / "launch-runtime.db"), FakeProvider(), FakeDiscovery(),
        True, ("base",), 5, launch_intelligence=service,
        launch_origin_resolvers={"base": OriginResolver()},
        trusted_launch_origin_providers=["base-rpc-launch-origin"],
        developer_evidence=developer_evidence,
    )
    monitor.poll_once()
    assert len(service.calls) == 1
    assert len(developer_evidence.calls) == 1
    developer_call = developer_evidence.calls[0]
    assert developer_call["chain"] == "base"
    assert developer_call["token_address"] == "discovered-token"
    assert developer_call["root_attestation"] is None
    assert developer_call["liquidity_evidence"] is None
    assert developer_call["launch_bundle"] is not None
    assert monitor.last_developer_results[("base", "discovered-token")]["status"] == "blocked"
    call = service.calls[0]
    assert call["chain"] == "base"
    assert call["indexer_kwargs"] == {"launch_block": 100, "window_end_block": 120}
    assert call["liquidity_evidence"] is None
    assert call["developer_evidence"] is None
    result = monitor.last_launch_results[("base", "discovered-token")]
    assert result["status"] == "blocked"
    assert result["origin_evidence_id"] == "origin-evidence"
    import sqlite3
    with sqlite3.connect(str(tmp_path / "launch-runtime.db")) as conn:
        row = conn.execute(
            "SELECT payload FROM events WHERE event='LAUNCH_INTELLIGENCE'"
        ).fetchone()
    assert row is not None
    assert "origin-evidence" in row[0]


def test_discovered_launch_origin_requires_explicit_provider_trust(tmp_path):
    class UntrustedResolver:
        provider_id = "unknown-origin"
        def resolve(self, token, window):
            raise AssertionError("untrusted resolver must not be called")

    class LaunchService:
        launch_window_ms = 120_000

    monitor = MarketMonitor(
        {}, str(tmp_path / "untrusted-origin.db"), FakeProvider(), FakeDiscovery(),
        True, ("base",), 5, launch_intelligence=LaunchService(),
        launch_origin_resolvers={"base": UntrustedResolver()},
        trusted_launch_origin_providers=(),
    )
    monitor.poll_once()
    result = monitor.last_launch_results[("base", "discovered-token")]
    assert result["status"] == "blocked"
    assert "untrusted" in result["reason"]


def test_monitor_submits_only_verified_all_pool_liquidity_evidence(tmp_path):
    import time
    from types import SimpleNamespace
    from veltrix.liquidity_evidence import (
        LiquidityInventoryBatch, LockerDeployment, LockerPositionSnapshot,
        PoolLiquiditySnapshot,
    )

    now = int(time.time() * 1000)
    token = "discovered-token"
    locker = "0xc4E637D37113192F4F1F060DaEbD7758De7F4131"
    pool = "0x2222222222222222222222222222222222222222"
    lp = "0x3333333333333333333333333333333333333333"
    digest = "a" * 64
    deployment = LockerDeployment(
        chain="base", provider="locker-registry", address=locker,
        abi_profile="uncx-v2.1-locks-nonce", expected_code_hash=digest,
        observed_code_hash=digest, source_ref="https://registry.example/locker",
        independence_group="locker-operator", chain_id=8453,
    )
    batch = LiquidityInventoryBatch(
        chain="base", token_address=token, provider="pool-indexer",
        independence_group="independent-indexer", observed_at_ms=now - 100,
        finalized_ref="block:100", pool_set_id="all-token-pools-v1",
        inventory_complete=True,
        pools=(PoolLiquiditySnapshot(
            chain="base", token_address=token, pool_address=pool,
            lp_token_address=lp, total_lp_supply=100,
            observed_at_ms=now - 100, finalized_ref="block:100",
            inventory_provider="pool-indexer", inventory_complete=True,
            pool_set_id="all-token-pools-v1", locker_custody_amount=100,
        ),),
        positions=(LockerPositionSnapshot(
            chain="base", pool_address=pool, lp_token_address=lp,
            locker_address=locker, position_id="lock-100", amount=100,
            unlock_at_ms=now + 86_400_000, observed_at_ms=now - 100,
            owner="0x5555555555555555555555555555555555555555",
            evidence_ref="tx:lock-100",
        ),),
        deployments=(deployment,),
    )

    class LiquidityProvider:
        def fetch_inventory(self, token_address):
            assert token_address == token
            return batch

    class OriginResolver:
        provider_id = "base-rpc-launch-origin"
        def resolve(self, address, window):
            return SimpleNamespace(
                complete=True, chain="base", launch_at_ms=now - 120_000,
                launch_block=100, window_end_block=120,
                provider=self.provider_id, evidence_id="origin",
            )

    class LaunchService:
        launch_window_ms = 120_000
        def __init__(self):
            self.call = None
        def evaluate(self, **kwargs):
            self.call = kwargs
            return (
                SimpleNamespace(coverage_complete=False, bundled_supply_pct=None),
                SimpleNamespace(passed=False, composite_verified=False,
                                reasons=("developer evidence missing",)),
            )

    service = LaunchService()
    monitor = MarketMonitor(
        {}, str(tmp_path / "liquidity-runtime.db"), FakeProvider(), FakeDiscovery(),
        True, ("base",), 5, launch_intelligence=service,
        launch_origin_resolvers={"base": OriginResolver()},
        trusted_launch_origin_providers=["base-rpc-launch-origin"],
        liquidity_evidence_providers={"base": LiquidityProvider()},
    )
    monitor.poll_once()
    evidence = service.call["liquidity_evidence"]
    assert evidence is not None
    assert evidence.claim == "liquidity_lock"
    assert evidence.liquidity_inventory_complete is True
    assert evidence.liquidity_locked_lp_pct == 100.0
    assert evidence.independence_group == "independent-indexer"
    assert monitor.last_liquidity_results[("base", token)]["status"] == "verified"
