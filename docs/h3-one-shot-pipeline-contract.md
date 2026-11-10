# H3 one-shot Solana analysis pipeline contract

## Objective

Compose the existing bounded Solana workers into one scheduled, finite pipeline.
A scheduler invocation runs one orchestration attempt and exits. It must not start
a daemon, background thread, websocket supervisor, hidden retry loop, or
permanent process.

This document is a contract, not a claim that the pipeline is implemented.

## Fixed stage order

1. **Discovery** — one bounded poll of configured read-only discovery sources.
   The ephemeral runner does not start the long-lived PumpPortal stream
   supervisor; stream processing requires a separately designed bounded ingress
   mechanism.
2. **Initial enrichment** — process at most the configured batch of newly
   discovered mints and persist timestamped market observations.
3. **Market refresh** — refresh due observations only; preserve source,
   retrieval-time, quality and staleness metadata.
4. **Risk evidence** — collect and persist the current read-only security
   evidence for eligible candidates.
5. **Consensus** — compute and persist a decision only from the persisted,
   timestamped market and risk evidence.
6. **Paper review/execution** — process exits and evaluate paper entries from
   the latest fully revalidated consensus decision. This stage is disabled by
   default and may only call the existing paper broker.

No stage may construct, sign, submit or broadcast a Solana transaction.

## Fencing contract

Every stage must accept the active RunContext. Checking the fence only before
and after a stage is insufficient.

- Check the deadline and lease before every bounded unit of work.
- Each worker claim, completion, failure, retry scheduling and evidence/ledger
  mutation must validate owner ID, fencing token and lease expiry inside the
  same database transaction as that mutation.
- Provider reads may happen outside a write transaction, but the worker must
  revalidate the fence before committing the returned evidence.
- A worker that loses its lease must stop processing and must not mark its job
  failed or successful. The next lease owner performs bounded interrupted-run
  recovery.
- External side effects require their own idempotency and fencing contract.
  The paper broker is local database state; no external trading side effect is
  permitted.

Existing worker run_once methods do not yet accept RunContext. Do not compose
them into a production pipeline until their write paths implement this
transaction-level contract.

## Bounds and idempotency

- Default maximum batch: 25 candidates per stage; each stage may impose a
  lower provider-specific limit.
- One orchestration attempt per scheduler invocation.
- Stable idempotency key per scheduled analysis window and pipeline version.
- Identical idempotency key plus identical payload is a no-op; a changed payload
  under the same key is rejected.
- Runtime must remain below the orchestration deadline; provider connect/read
  timeouts must be shorter than the remaining budget.
- Retries are deferred to later scheduler invocations; no in-process retry loop.
- The aggregate result records per-stage counts, deferrals and sanitized
  failures, never credentials, provider response bodies or exception messages.

## Paper-only gate

Paper processing is off unless an explicit configuration flag enables it.
When enabled, the only permitted execution target is the existing paper ledger.
LIVE_TRADING remains false; signer and transaction-submission boundaries remain
absent. A missing or stale evidence stage must result in no paper entry.

## Required implementation and validation gates

1. Complete H1.1 shared database adapter integration and retain the H1.2
   dedicated-disposable-database guard.
2. Pass live Turso conformance on the exact disposable host before claiming
   backend compatibility.
3. Add RunContext-aware worker APIs and transaction-level fencing to each
   worker's durable write paths.
4. Add deterministic stage-order, partial-failure, retry, idempotency, lease-loss
   and deadline tests.
5. Verify a full SQLite integration run, then the same pipeline against the
   dedicated disposable Turso DB.
6. Compare website/dashboard state, persisted ledger and scheduled-run report
   from the same run ID before enabling any scheduled production cadence.

## Current non-completions

- This contract is not an implementation or deployment.
- H1.1 and H2 remain draft pull requests.
- Dedicated Turso credentials and remote conformance are not verified.
- Worker-level transaction fencing and the composed one-shot pipeline are not
  implemented.
- No production scheduler, Cloudflare API/dashboard, domain, DNS or HTTPS route
  is configured by this phase.
