import argparse
import time
from .config import settings
from .ledger import PaperBroker
from .monitor import MarketMonitor
from .providers.factory import build_intelligence_coordinator

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
    monitor = MarketMonitor(watchlist, cfg.db_path, discovery_enabled=cfg.discovery_enabled,
        allowed_chains=cfg.discovery_chains, max_discovered_per_chain=cfg.max_discovered_per_chain,
        broker=broker, wallet_intelligence=wallet_intelligence,
        wallet_max_age_ms=cfg.wallet_evidence_max_age_ms)
    count = sum(len(v) for v in watchlist.values())
    print(f"VELTRIX PAPER MONITOR: virtual balance {cfg.balance:.2f} USD; duration {args.hours:g}h; watchlist tokens {count}")
    print(f"Profile discovery enabled: {cfg.discovery_enabled}; chains: {','.join(cfg.discovery_chains)}")
    print("Discovery candidates are untrusted leads only. No paper or live orders are generated.")
    print(f"Wallet intelligence connectors configured: {len(intelligence.providers)}; trusted: {len(cfg.trusted_wallet_providers)}")
    deadline = time.monotonic() + args.hours * 3600
    try:
        while time.monotonic() < deadline:
            try:
                observations = monitor.poll_once()
                wallet_batches = tuple(monitor.last_wallet_intelligence.values())
                wallet_observations = sum(len(batch.batch.accepted) for batch in wallet_batches)
                wallet_clusters = sum(len(batch.clusters) for batch in wallet_batches)
                wallet_failures = sum(len(batch.provider_failures) for batch in wallet_batches)
                print(
                    f"Market observations: {len(observations)}; "
                    f"verified wallet observations: {wallet_observations}; "
                    f"multi-wallet clusters: {wallet_clusters}; "
                    f"provider failures: {wallet_failures}"
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
