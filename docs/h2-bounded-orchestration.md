# H2 bounded one-shot orchestration

## Contract

H2 adds durable coordination, not a permanently running worker. A scheduled
invocation may execute at most one job attempt, under one global worker lease.
A later scheduler invocation performs a retry after its persisted
`next_attempt_at_ms`. No daemon, thread, timer, in-process queue, or unbounded
retry loop is created.

## Records and invariants

- `orchestration_jobs`: unique idempotency key, payload digest, job type,
  attempt counter, bounded maximum attempts, lifecycle timestamps, retry time,
  terminal status and sanitized error code.
- `orchestration_worker_lease`: one named global lease, owner ID, expiry and
  monotonically increasing fencing token. Acquisition is one atomic
  conditional upsert inside a transaction. Renew/release require both owner
  and fencing token; expired owners cannot renew or release a successor's lease.
- `orchestration_audit`: append-only lifecycle events without payload bodies,
  credentials, provider response bodies or exception messages.
- Reusing an idempotency key with a different job type or payload digest fails.
- Retries are deferred; maximum attempts are capped at five. A later scheduler
  invocation must submit the same idempotency key and payload.
- A stale RUNNING attempt is recovered only after the caller has acquired the
  global lease. It becomes RETRYABLE or FAILED depending on remaining attempts.

## Runtime limits

`run_once` accepts a deadline of 1–240 seconds and a work budget of 1–10,000
items. The lease must outlive the configured deadline by at least 15 seconds.
Handlers must call `context.checkpoint()` at bounded work boundaries and
reassign nothing: the context tracks consumed items in place.

The deadline is cooperative, not an OS-level kill switch. It cannot interrupt a
Python handler blocked inside an external call. Provider HTTP/WebSocket calls
must separately configure connect/read timeouts shorter than the remaining
worker budget. Avoid unbounded streaming and use bounded pages/batches.

## Deployment requirements

- Use a scheduler to invoke one-shot jobs; retries are picked up on later runs.
- Ensure only trusted workflows receive the Turso secrets.
- Keep `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in GitHub Actions secrets;
  never print them or include them in artifacts.
- H2 conformance must use the disposable Frankfurt database only. Never point
  this test at a production ledger.
- The local SQLite backend remains the default and rollback path. No production
  data is migrated by this phase.

## Explicit non-goals

No wallet keys, signing, transaction construction/submission, live trading,
unbounded worker, automatic infrastructure provisioning, or production DB
migration. This is application-level coordination, not proof of Linux
process/mount isolation or serverless-host equivalence to the Phase 09.2 host
gate.
