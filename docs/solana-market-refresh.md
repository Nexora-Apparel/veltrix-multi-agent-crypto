# Solana Market Snapshot Refresh

`MarketRefreshWorker` keeps discovered Solana mints on a durable refresh
schedule after initial enrichment. It uses the existing read-only
`SolanaMarketData` adapter and append-only `MarketSnapshotStore`.

## Behavior

- Seeds one refresh job per persisted launch candidate.
- Claims bounded due batches under SQLite `BEGIN IMMEDIATE` and leases jobs.
- Refreshes on a configurable cadence (default 60 seconds).
- Validates returned snapshot type, mint identity, and retrieval freshness.
- Appends new provider observations; duplicate observations are counted as
  unchanged rather than rewriting historical rows.
- Retries provider failures with capped exponential backoff.
- Recovers expired processing leases after worker interruption.
- Exposes active, processing, retry, succeeded, and degraded counts.

## Boundaries

This is a read-only market-data maintenance worker. It does not generate token
scores, risk verdicts, BUY signals, positions, or swap transactions. A refreshed
market snapshot is not evidence of token safety. Provider timestamps and
coverage limits remain visible in the stored snapshot history.
