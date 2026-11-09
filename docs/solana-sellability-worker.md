# Solana Sellability Worker

The sellability worker schedules only candidates with a finalized mint snapshot
whose raw supply and decimals reconcile with the holder inventory. It persists
observations append-only, leases jobs, and retries unexpected failures.

## Exact paper-size mode

The production runtime supplies the shared `MarketSnapshotStore` and configures
the current paper notional at $50. For each mint, the worker:
1. Reads the latest market USD price.
2. Reads finalized raw supply and decimals from the risk evidence report.
3. Uses `calculate_paper_exit_size` with Decimal arithmetic and downward
   rounding to derive the exact raw token amount for a $50 paper position.
4. Requests a sell-side Jupiter V2 quote for that exact amount.
5. Persists the route observation separately from risk evidence.

The allowed size band is 0.0001%–5% of finalized supply. Market price must be no older than 120 seconds and risk evidence no older
than 300 seconds. Missing/stale market price, stale risk evidence, missing mint
metadata, mismatched supply/decimals, or an amount outside the configured band
fails closed and is retried. A diagnostic-only worker
without a market store may still use the configured supply fraction.

## Evidence boundary

The worker does not remove `executable_sellability` from the risk report's
missing claims. A route observation remains
`ROUTE_OBSERVED_NOT_EXECUTION_PROVEN`; it is not a transfer test, transaction
simulation or live-execution certificate.

The worker is integrated into the read-only Solana runtime but remains disabled
unless `SOLANA_SELLABILITY_ENABLED=true`. Quote-only Jupiter V2 requests require
no taker address and never call `/execute`. Tests use synthetic quote providers;
they do not establish provider uptime or live sellability.
