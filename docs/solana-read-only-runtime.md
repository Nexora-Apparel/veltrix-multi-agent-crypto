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
thread, persists launches, performs initial DexScreener enrichment, optionally
collects GoPlus + finalized holder + finalized mint-account risk evidence, and
runs scheduled market refreshes. Risk collection is enabled only when the
existing `GOPLUS_ACCESS_TOKEN` and `SOLANA_RPC_URL` settings are configured.
No new secret is introduced.

All workers share one SQLite database. Shutdown sets the shared stop event and
attempts a bounded stream-thread join.

The runtime is deliberately isolated from Core and the paper broker. It has no
wallet, private key, signer, Jupiter, transaction submission, or live execution
path. It only collects launch, market, and risk observations. It does not claim
exhaustive launch coverage: PumpPortal stream interruptions remain recorded
coverage gaps, and there is no historical launch backfill in this runtime.
