# Solana paper decision runner — next integration boundary

## Current state

The read-only runtime collects and persists launch, market, risk, developer,
bundle and sell-route evidence. It does not currently instantiate Core or a
PaperBroker. Core can now require a matching sell-route observation and exact
raw exit size, but a single orchestration component must join the persisted
evidence before a candidate is evaluated.

## Runner contract

The proposed `SolanaPaperDecisionRunner` is the only bridge from persisted
Solana evidence to the existing paper-only Core. It must:

1. Load one canonical mint candidate and the latest market, risk and
   sellability observations from the shared SQLite database.
2. Reconstruct typed evidence objects from stored payloads. Never pass raw
   dictionaries as trusted evidence.
3. Require finalized mint supply and decimals to reconcile with the holder
   inventory, and preserve all unknown mandatory token-risk claims as vetoes.
4. Require the latest market snapshot to be no older than 120 seconds, risk
   evidence no older than 300 seconds, and the Jupiter route to remain within
   its quote expiry and Shield freshness limits.
5. Use the same market USD price that was used to calculate the sell quote.
   Recalculate the raw sell amount from the configured paper notional and
   finalized decimals/supply; reject any mismatch.
6. Call Core only after the evidence packet is complete. Core still requires at
   least two distinct BUY agents, the composite safety report, Shield approval,
   available paper cash and position/concurrency limits.
7. Persist a deterministic decision audit for both approved and rejected
   candidates, including source evidence IDs, observed timestamps, raw quote
   amount, route-only state and all veto reasons.

## Failure behaviour

- Missing, malformed, stale, future-dated or mismatched evidence means HOLD/BLOCK.
- A quote-only observation never clears `executable_sellability` and never
  changes its state to execution-proven.
- No evidence is inferred from a market chart, token symbol or provider's
  promotional metadata.
- Duplicate candidate cycles must be idempotent and must not open duplicate
  positions for the same mint.
- Worker crashes or unavailable providers must leave candidates retryable,
  not approved.

## Explicit exclusions

The runner has no wallet, private key, signer, Jupiter `/execute`, RPC write
method or transaction submission capability. It will use the existing
$500 virtual balance, $50 maximum position, ten-position cap and 6% initial
stop profile. A 24-hour paper run remains an operational smoke test, not
evidence of profitability or live sellability.

## Acceptance tests required before enabling

- Missing/stale risk, market or sell-route data never opens a position.
- Any unknown token-risk claim vetoes entry.
- An `executable_sellability` unknown remains recorded while a paper-only
  route observation can be used.
- A quote for a different raw amount, token price or notional is rejected.
- Duplicate processing cannot create duplicate positions or duplicate BUYs.
- Insufficient paper cash and max-position limits are enforced by the broker.
- The decision audit records the exact evidence IDs and route-only state.
- No network call in the runner can sign or submit a transaction.
