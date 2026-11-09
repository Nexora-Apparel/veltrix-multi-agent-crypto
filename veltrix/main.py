import argparse
import time
from .config import settings
from .ledger import PaperBroker
from .monitor import MarketMonitor
from .providers.factory import (build_intelligence_coordinator, build_developer_evidence_orchestrator, build_liquidity_snapshot_providers)
from .providers.intelligence import VerifiedIntelligenceAdapters
from .providers.launch import build_launch_intelligence_service
from .launch_origin import build_launch_origin_resolvers
from .provider_readiness import build_provider_readiness

def main():
    parser = argparse.ArgumentParser(description="VELTRIX multi-chain paper monitor")
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    cfg = settings()
    if args.hours <= 0:
        raise SystemExit("--hours must be positive")
    watchlist = {"solana": cfg.solana_tokens, "base": cfg.base_tokens, "bsc": cfg.bsc_tokens}
    broker = PaperBroker(cfg.db_path, cfg.balance, cfg.max_open, cfg.max_position, cfg.stop_loss)
    intelligence = build_intelligence_coordinator(cfg)
    wallet_intelligence = intelligence if intelligence.providers else None
    launch_service = None
    origin_resolvers = {}
    developer_evidence = None
    liquidity_evidence_providers = {}
    if cfg.launch_intelligence_enabled:
        adapters = VerifiedIntelligenceAdapters.from_settings(cfg)
        launch_service = build_launch_intelligence_service(cfg, adapters)
        origin_resolvers = build_launch_origin_resolvers(cfg)
        developer_evidence = build_developer_evidence_orchestrator(
            cfg, adapters, audit_sink=broker,
        )
        try:
            liquidity_evidence_providers = build_liquidity_snapshot_providers(cfg)
        except (ValueError, TypeError) as exc:
            print(f"Liquidity reader configuration blocked safely: {type(exc).__name__}")
    monitor = MarketMonitor(watchlist, cfg.db_path, discovery_enabled=cfg.discovery_enabled,
        allowed_chains=cfg.discovery_chains, max_discovered_per_chain=cfg.max_discovered_per_chain,
        broker=broker, wallet_intelligence=wallet_intelligence,
        wallet_max_age_ms=cfg.wallet_evidence_max_age_ms,
        launch_intelligence=launch_service,
        launch_origin_resolvers=origin_resolvers,
        trusted_launch_origin_providers=cfg.trusted_launch_origin_providers,
        developer_evidence=developer_evidence,
        liquidity_evidence_providers=liquidity_evidence_providers,
        position_management_chains=(
            ("base", "bsc") if cfg.paper_trading_enabled else ("solana", "base", "bsc")
        ))
    readiness = build_provider_readiness(cfg)
    print(f"Provider readiness: {len(readiness.blocked_claims)}/{len(readiness.claims)} chain/claim checks blocked; fully ready: {readiness.fully_ready}")
    for item in readiness.blocked_claims:
        print(f"  [{item.chain}] {item.claim}: {'; '.join(item.blockers)}")
    count = sum(len(v) for v in watchlist.values())
    print(f"VELTRIX PAPER MONITOR: virtual balance {cfg.balance:.2f} USD; duration {args.hours:g}h; watchlist tokens {count}")
    print(f"Profile discovery enabled: {cfg.discovery_enabled}; chains: {','.join(cfg.discovery_chains)}")
    print("Discovery candidates are untrusted leads only. No new paper or live orders are generated.")
    if cfg.paper_trading_enabled:
        print("Solana paper-position exits are delegated to the Phase 07 consensus paper runtime.")
    print(f"Wallet intelligence connectors configured: {len(intelligence.providers)}; trusted: {len(cfg.trusted_wallet_providers)}")
    print(f"Launch intelligence enabled: {cfg.launch_intelligence_enabled}; indexers: {len(getattr(launch_service, 'indexers', {}))}; origin resolvers: {len(origin_resolvers)}; liquidity readers: {len(liquidity_evidence_providers)}")
    if cfg.launch_intelligence_enabled:
        print("Launch candidates are submitted to bundle and developer-evidence analysis. Missing signed roots, liquidity evidence or certified claim providers fail closed; no paper entries are opened.")
    deadline = time.monotonic() + args.hours * 3600
    try:
        while time.monotonic() < deadline:
            try:
                observations = monitor.poll_once()
                wallet_batches = tuple(monitor.last_wallet_intelligence.values())
                wallet_observations = sum(len(batch.batch.accepted) for batch in wallet_batches)
                wallet_clusters = sum(len(batch.clusters) for batch in wallet_batches)
                wallet_failures = sum(len(batch.provider_failures) for batch in wallet_batches)
                launch_checked = len(monitor.last_launch_results)
                developer_checked = len(monitor.last_developer_results)
                liquidity_checked = len(monitor.last_liquidity_results)
                liquidity_verified = sum(
                    1 for item in monitor.last_liquidity_results.values()
                    if item.get("status") == "verified"
                )
                developer_analyzed = sum(
                    1 for item in monitor.last_developer_results.values()
                    if item.get("status") == "analyzed"
                )
                launch_certified = sum(
                    1 for item in monitor.last_launch_results.values()
                    if item.get("status") == "certified"
                )
                print(
                    f"Market observations: {len(observations)}; "
                    f"verified wallet observations: {wallet_observations}; "
                    f"multi-wallet clusters: {wallet_clusters}; "
                    f"provider failures: {wallet_failures}; "
                    f"launch candidates checked: {launch_checked}; "
                    f"liquidity inventory checks: {liquidity_checked}; "
                    f"verified liquidity snapshots: {liquidity_verified}; "
                    f"developer evidence checks: {developer_checked}; "
                    f"complete developer analyses: {developer_analyzed}; "
                    f"composite safety passes: {launch_certified}"
                )
            except Exception as exc:
                print(f"Provider cycle failed safely: {type(exc).__name__}: {exc}")
            time.sleep(min(cfg.poll_seconds, max(0, deadline-time.monotonic())))
    except KeyboardInterrupt:
        print("Session interrupted; inspect the SQLite ledger. Performance is not certified.")
    report = broker.session_report()
    print("PAPER SESSION REPORT")
    print(f"Starting balance: ${report['starting_balance']:.2f}")
    print(f"Cash: ${report['cash']:.2f}")
    print(f"Realized PnL: ${report['realized_pnl']:.2f}")
    print(f"Closed positions: {report['closed_positions']}; win rate: {report['win_rate_pct']:.1f}%")
    print(f"Open positions remaining: {report['open_positions']}")
    print(f"Best closed position: {report['best_trade']}")
    print(f"Worst closed position: {report['worst_trade']}")
    print("No live orders were sent. This is not a certified strategy-performance result.")

if __name__ == "__main__":
    main()
