# Phase 04 — Solana Token Intelligence

## Scope

Phase 04 turns each discovered mint into a timestamped, read-only market
intelligence record. It does not score tokens, make safety decisions, call the
AI agents, create positions, or submit transactions.

## Market source and batching

The primary market source is the existing DEX Screener provider using:

`GET /tokens/v1/solana/{tokenAddresses}`

The API accepts up to 30 comma-separated token addresses per request and
documents a 300-requests-per-minute limit for this endpoint. Both enrichment
workers use `fetch_many` so a batch of up to 30 candidates can share one API
request.

The provider response time is recorded locally as `response_received_at_ms`.
DEX Screener does not expose an authoritative per-pair source timestamp in
these pair records; this field must not be interpreted as the exact time of the
underlying on-chain trade or reserve update.

## Stored intelligence

For the selected pool, the snapshot records:

- Token name and symbol.
- USD price, pool liquidity and 5-minute / 1-hour / 24-hour volume.
- 5-minute / 1-hour / 24-hour price change.
- 5-minute / 1-hour / 24-hour buy and sell counts when supplied.
- FDV and market cap when supplied.
- DEX, quote-token symbol and pair address.
- Pair creation time, number of eligible pools, local retrieval time and source
  response receipt time.
- Explicit data-quality flags for missing or zero fields.

The selected pool is the single eligible pool with the highest reported USD
liquidity. Liquidity and volume are not summed across pools, because fragmented
or correlated pools must not be mistaken for one executable market.

## Data quality and limitations

- Only exact Solana chain and mint matches are eligible.
- Prices must be finite and positive.
- Liquidity and volume must be finite and non-negative.
- Invalid individual pairs are discarded without discarding valid pairs in the
  same provider response.
- Missing fields remain missing and are surfaced as quality flags.
- The response receipt timestamp is not proof that the provider's underlying
  market data is equally recent.
- A displayed liquidity figure is not proof of executable depth or sellability.

## Persistence and workers

`MarketSnapshotStore` is append-only. It deduplicates the same mint/pair/source
response timestamp while retaining later observations.

`CandidateEnrichmentWorker`:
1. Imports unseen launch candidates into a durable job table.
2. Claims bounded jobs with SQLite leases.
3. Fetches market snapshots in batches.
4. Persists snapshots and marks successful jobs completed.
5. Retries failures with capped exponential backoff and marks exhausted jobs
   dead for review.

`MarketRefreshWorker`:
1. Schedules candidates that already have an initial snapshot.
2. Refreshes on a configurable cadence (default 60 seconds).
3. Appends changed observations and counts duplicates as unchanged.
4. Recovers expired leases and retries provider failures.

Both workers require the same persistent SQLite file as the Phase 03 launch
store. In-memory or split databases are rejected.

The supervised runtime is started with:

```bash
python -m veltrix.solana_meme.intelligence_worker
```

Configuration:
- `VELTRIX_SOLANA_DISCOVERY_DB`: same SQLite path used by Phase 03.
- `VELTRIX_MARKET_INTELLIGENCE_POLL_SECONDS`: worker loop interval (default 15).
- `VELTRIX_MARKET_REFRESH_INTERVAL_MS`: snapshot refresh cadence (default 60000).
- `VELTRIX_MARKET_BATCH_SIZE`: candidates per batch (default 25, maximum 30).

The default batch and loop settings remain far below the documented DEX
Screener token-pairs request ceiling.

## Safety boundary

This stage reports market facts and data-quality gaps only. It does not inspect
mint/freeze authorities, holder concentration, creator wallets, honeypot or
sellability conditions, liquidity locks, malicious metadata, or contract risk.
Those checks belong to Phase 05. No market snapshot is a buy signal.
