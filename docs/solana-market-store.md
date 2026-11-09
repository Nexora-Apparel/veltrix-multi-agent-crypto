# Solana market snapshot persistence

`MarketSnapshotStore` writes normalized market observations to an append-only
SQLite table. The unique key `(mint, pair_address, provider_observed_at_ms)`
prevents duplicate ingestion of the same provider observation while retaining
later observations as history.

The store exposes:
- `record(snapshot)`: returns false when the observation already exists.
- `latest(mint)`: most recently retrieved snapshot.
- `history(mint, limit)`: newest-first timestamped history.
- `count(mint=None)`: total snapshot count.

Use the same persistent SQLite database path as the launch store when a single
local evidence database is desired. Configure persistent storage in deployment;
ephemeral filesystems are not suitable for operational history.

Historical snapshots are evidence for later liquidity/volatility analysis. They
are not proof of executable depth, sellability or token safety.
