# Phase 07 — Consensus-Gated Paper Trading

## Scope

Phase 07 connects the Phase 06 Solana consensus ledger to the existing SQLite
`PaperBroker`. It creates virtual positions only. It does not connect to an
exchange, sign transactions, hold private keys, or submit swaps.

## Entry gate

A decision is eligible for a paper entry only when all of the following remain
true at execution time:

1. The latest decision for that mint is `CONSENSUS_APPROVED`.
2. `paper_review_eligible` is exactly true and `execution_authorized` is
   exactly false.
3. At least two distinct specialist agents support the same mint and Shield has
   exactly one `APPROVE` vote.
4. The decision is fresh and its market snapshot is still the latest snapshot.
5. The market snapshot is fresh, has a finite positive price, and has no
   unresolved quality flags.
6. The latest risk evidence is fresh, matches the decision's evidence hash,
   state, missing-claim list and risk flags, and contains no unresolved claims,
   provider errors, or medium/high/critical flags.
7. The broker's position and cash limits permit the virtual entry.

Only the latest decision per mint is considered. An older approval cannot
override a newer abstention, rejection, or changed market snapshot.

## Execution model

- Default starting virtual balance: USD 500.
- Default maximum position notional: USD 50.
- Default maximum concurrent positions: 10.
- Default maximum portfolio exposure: 50% of starting virtual balance.
- Default entry/exit fee assumption: 100 basis points per side.
- Default entry/exit slippage assumption: 50 basis points per side.
- Default initial stop-loss: 6%.
- The existing broker applies one-time 2x and 3x partial exits and a 6%
  trailing stop after the 3x milestone.
- Entry and exit fills, fees, slippage costs and decision IDs are persisted.
- Decision ID consumption and opening the virtual position are atomic in SQLite.
- Duplicate decisions cannot create duplicate positions.
- Stale or invalid market prices do not trigger paper exits.

The fee and slippage settings are explicit simulation assumptions, not measured
execution costs. Phase 08 will expose these assumptions and their effect on
reported P&L. No profitability claim is made from synthetic tests.

## Database

All Phase 03–07 stores and the existing PaperBroker must share the same
persistent SQLite database. Path selection is `VELTRIX_SOLANA_DISCOVERY_DB`,
then the legacy `DB_PATH`, then `data/solana_discovery.sqlite3`. If both
environment variables are set, they must resolve to the same file; the paper
runtime refuses to create a second ledger silently. Set both to the existing
paper-ledger path to preserve the current account and history. Do not use
`:memory:` for this runtime.

## Run

Start the existing Phase 03 discovery, Phase 04 market refresh and Phase 05
risk-evidence workers separately. Then start the paper worker:

```bash
python -m veltrix.solana_meme.paper_runtime
```

Keep `LIVE_TRADING=false`; the runtime refuses to start if live trading is
enabled. Stop with SIGINT or SIGTERM. The runtime logs each cycle and stops
cleanly.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `VELTRIX_PAPER_POLL_SECONDS` | 15 | Worker polling interval; 5–3600 seconds |
| `VELTRIX_PAPER_DECISION_BATCH_SIZE` | 25 | Maximum latest-per-mint decisions per cycle; 1–250 |
| `VELTRIX_PAPER_FEE_BPS` | 100 | Simulated fee on each side |
| `VELTRIX_PAPER_SLIPPAGE_BPS` | 50 | Simulated slippage on each side |
| `VELTRIX_PAPER_MAX_PORTFOLIO_EXPOSURE_PCT` | 0.5 | Maximum open cost basis as a fraction of starting balance |
| `VELTRIX_PAPER_MAX_DECISION_AGE_MS` | 120000 | Maximum approval age |
| `VELTRIX_PAPER_MAX_MARKET_AGE_MS` | 120000 | Maximum market-snapshot age |
| `VELTRIX_PAPER_MAX_RISK_AGE_MS` | 300000 | Maximum security-evidence age |
| `PAPER_BALANCE_USD` | 500 | Initial virtual balance on a new database |
| `MAX_POSITION_USD` | 50 | Per-position notional cap |
| `MAX_OPEN_POSITIONS` | 10 | Concurrent position cap |
| `STOP_LOSS_PCT` | 0.06 | Initial and trailing stop fraction |

The broker creates the starting account only once. Changing
`PAPER_BALANCE_USD` does not reset an existing ledger.

## Phase boundary

Phase 07 ends at a durable, restart-safe paper execution and performance ledger.
Dashboard and analytics are Phase 08. Live execution remains disabled in Phase
09 and is not enabled by this worker.
