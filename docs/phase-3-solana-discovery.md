# Phase 03 — Solana Discovery Engine

## Scope

This stage discovers Solana token candidates and durably records observations.
It does not perform market enrichment, token scoring, security analysis, agent
consensus, paper trading, or transaction execution.

## Sources

### Primary: PumpPortal new-token stream

- Endpoint: `wss://pumpportal.fun/api/data`
- Subscription: `subscribeNewToken` only
- No API key or paid trade-stream subscription is used by this adapter.
- One websocket connection is supervised by `LaunchStreamManager`.
- Malformed messages, non-create events, and invalid mint strings are discarded.

PumpPortal documents new-token and migration subscriptions as free. Trade
subscriptions are separately metered and are intentionally out of scope.

### Secondary: DEX Screener latest lists

- `/token-profiles/latest/v1`
- `/token-boosts/latest/v1`

These feeds are candidate leads only. Profiles and boosts can reflect paid
promotion and must not be interpreted as organic demand, liquidity, safety, or
buy signals. The adapter filters strictly to `chainId=solana` and normalizes
candidate mint addresses through the Phase 02 validator.

The polling interval defaults to 30 seconds. Each latest-list endpoint is
documented with a 60-requests-per-minute limit; the default polling rate remains
well below that ceiling.

## Persistence and coverage

`LaunchStore` stores:
- One candidate row per mint.
- First and latest observation timestamps.
- Latest-observation primary source.
- All observed source identities.
- Append-only connection and candidate events.
- Source-specific outage start and recovery timestamps.

Use a persistent SQLite path in deployment. The in-memory mode exists for tests
and short-lived local probes only.

An outage is not automatically treated as resolved by activity from a different
provider. Each secondary source recovers independently after a successful poll;
the primary stream's outage is closed by a subsequent valid event.

## Runtime

`SolanaDiscoveryEngine.run(stop_event)` polls the secondary lists and can
supervise the primary PumpPortal stream in a dedicated daemon thread. Run it as
a long-lived worker process, not inside a web request:

```bash
python -m veltrix.solana_meme.worker
```

Configure the persistent database with `VELTRIX_SOLANA_DISCOVERY_DB` and the
secondary poll interval with `VELTRIX_SOLANA_DISCOVERY_POLL_SECONDS` (default
30 seconds; accepted range 5–3600 seconds). `SIGINT` and `SIGTERM` trigger a
graceful stop.

## Safety boundary

- Discovery is not a buy signal.
- No token enrichment or scoring is performed in this stage.
- No security/risk verdict is produced in this stage.
- No wallet, signer, swap, transaction or live-trading dependency exists.
- A syntactically valid Solana public key is not proof that the address is an
  initialized SPL Token or Token-2022 mint. On-chain mint verification belongs
  to a separately verified validation/data step and must never be inferred here.
