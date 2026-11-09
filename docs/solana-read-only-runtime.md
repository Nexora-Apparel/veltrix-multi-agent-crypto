# Solana Read-Only Runtime

Run the integrated launch-discovery and market-data pipeline with:

```bash
python -m veltrix.solana_meme.runtime --database veltrix_solana_meme.sqlite3
```

Optional controls:

- `--poll-seconds`: pipeline polling interval, 5–300 seconds (default 15).
- `--batch-size`: bounded enrichment/refresh batch, 1–500 (default 25).
- `--duration-seconds`: finite run duration; 0 runs until interrupted.

The runtime starts one supervised PumpPortal new-token stream in a background
thread, persists launches, performs initial DexScreener enrichment, then runs
scheduled market refreshes. Both workers share one SQLite database. Shutdown
sets the shared stop event and attempts a bounded stream-thread join.

The runtime is deliberately isolated from Core, the paper broker, wallet
intelligence, private keys, signers, Jupiter, and all live execution. It only
collects launch and market observations. It does not claim exhaustive launch
coverage: PumpPortal stream interruptions remain recorded coverage gaps, and
there is no historical launch backfill in this runtime.
