import argparse
import time
from .config import settings
from .ledger import PaperBroker
from .monitor import MarketMonitor

def main():
    parser = argparse.ArgumentParser(description="VELTRIX multi-chain paper monitor")
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    cfg = settings()
    if args.hours <= 0:
        raise SystemExit("--hours must be positive")
    watchlist = {"solana": cfg.solana_tokens, "base": cfg.base_tokens, "bsc": cfg.bsc_tokens}
    broker = PaperBroker(cfg.db_path, cfg.balance, cfg.max_open, cfg.max_position)
    monitor = MarketMonitor(watchlist, cfg.db_path, discovery_enabled=cfg.discovery_enabled,
        allowed_chains=cfg.discovery_chains, max_discovered_per_chain=cfg.max_discovered_per_chain)
    count = sum(len(v) for v in watchlist.values())
    print(f"VELTRIX PAPER MONITOR: virtual balance {cfg.balance:.2f} USD; duration {args.hours:g}h; watchlist tokens {count}")
    print(f"Profile discovery enabled: {cfg.discovery_enabled}; chains: {','.join(cfg.discovery_chains)}")
    print("Discovery candidates are untrusted leads only. No paper or live orders are generated.")
    deadline = time.monotonic() + args.hours * 3600
    try:
        while time.monotonic() < deadline:
            try:
                observations = monitor.poll_once()
                print(f"Market observations: {len(observations)}")
            except Exception as exc:
                print(f"Provider cycle failed safely: {type(exc).__name__}: {exc}")
            time.sleep(min(cfg.poll_seconds, max(0, deadline-time.monotonic())))
    except KeyboardInterrupt:
        print("Session interrupted; inspect the SQLite ledger. Performance is not certified.")
    print(f"Session ended. Realized PnL: {broker.totals()['realized_pnl']:.2f} USD; no live orders were sent.")

if __name__ == "__main__":
    main()
