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
    monitor = MarketMonitor(watchlist, cfg.db_path)
    count = sum(len(v) for v in watchlist.values())
    print(f"VELTRIX PAPER MONITOR: virtual balance {cfg.balance:.2f} USD; duration {args.hours:g}h; watchlist tokens {count}")
    if not count:
        print("No token addresses configured. Add comma-separated WATCHLIST_SOLANA, WATCHLIST_BASE and/or WATCHLIST_BSC values to .env.")
    print("Execution remains disabled until independent whale-flow and token-safety sources are integrated.")
    deadline = time.monotonic() + args.hours * 3600
    try:
        while time.monotonic() < deadline:
            if count:
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
