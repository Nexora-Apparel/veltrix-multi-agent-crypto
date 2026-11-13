# Phase 03 — Solana Discovery Engine

## Scope

This stage discovers Solana token candidates and durably records observations.
It does not perform market enrichment, token scoring, security analysis, agent
consensus, paper trading, or transaction execution.

## Sources

### Primary: PumpPortal new-token stream

- Endpoint: `wss://pumpportal.fun/api/data`
- Subscription: `subscribeNewToken` only
- The adapter subscribes only to the free new-token feed and never subscribes to paid trade streams. If the provider requires a key for websocket access, supply the complete endpoint through `VELTRIX_PUMPPORTAL_WS_ENDPOINT`; keep credentials out of source control.
- One websocket connection is supervised by `LaunchStreamManager`.
- Malformed messages, non-create events, and invalid mint strings are discarded.
- Provider connection/subscription errors report exception classes and a scheme/host only. URL userinfo, path, query and fragments are never copied into logs or outage records.

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
supervise the primary PumpPortal stream in a dedicated daemon thread. Before starting the long-running worker, run the one-shot provider connectivity check:

```bash
python -m veltrix.solana_meme.smoke
```

It checks the PumpPortal websocket handshake/subscription and verifies that at
least one DEX Screener discovery endpoint responds. It does not trade or leave a
socket open.

Run the continuous engine as
a long-lived worker process, not inside a web request:

```bash
python -m veltrix.solana_meme.worker
```

Configure the persistent database with `VELTRIX_SOLANA_DISCOVERY_DB` and the
secondary poll interval with `VELTRIX_SOLANA_DISCOVERY_POLL_SECONDS` (default
30 seconds; accepted range 5–3600 seconds). Optionally set
`VELTRIX_PUMPPORTAL_WS_ENDPOINT` to a provider-issued websocket URL if
connection authentication is required. `SIGINT` and `SIGTERM` trigger a
graceful stop.

## Safety boundary

- Discovery is not a buy signal.
- No token enrichment or scoring is performed in this stage.
- No security/risk verdict is produced in this stage.
- No wallet, signer, swap, transaction or live-trading dependency exists.
- A syntactically valid Solana public key is not proof that the address is an
  initialized SPL Token or Token-2022 mint. On-chain mint verification belongs
  to a separately verified validation/data step and must never be inferred here.


## Stage 3 exit criteria

- [x] Primary PumpPortal adapter subscribes only to `subscribeNewToken`; no paid trade stream is requested.
- [x] Secondary DEX Screener latest profile and boost feeds are polled independently with bounded request timeouts and a conservative interval.
- [x] Both sources produce canonical candidates through the Stage 2 validation contract.
- [x] Duplicate mints merge source provenance; persistent storage retains first/latest observation and source-specific coverage.
- [x] Primary reconnect uses bounded exponential backoff; secondary partial failures do not discard successful endpoint results.
- [x] Invalid candidate rows are audited and skipped without falsely classifying an otherwise healthy source as a feed outage.
- [x] PumpPortal endpoint path, query, fragment and userinfo are redacted from connection errors; raw provider exception text is not propagated.
- [x] Full repository CI and adversarial tests pass on the exact Stage 3 PR head (795 passed, 5 skipped; compileall passed).
- [x] Live, read-only PumpPortal handshake/subscription and DEX Screener endpoint smoke passed in the Stage 3 PR workflow; both DEX endpoints returned healthy and 51 normalized Solana candidates were observed.
- [ ] Post-merge CI and a read-only production-equivalent worker construction test pass.
- [x] Review confirms no schema reset/migration, credential changes, Telegram activation, schedule, production deployment, wallet or execution capability was introduced.

The discovery worker remains an explicitly invoked, read-only process. This stage does not enable automatic scheduling or production deployment.
