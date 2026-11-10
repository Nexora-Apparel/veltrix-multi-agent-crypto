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
  conditional upsert inside a transaction. Release marks the row unowned rather
  than deleting it, preserving the counter across clean hand-offs. Renew/release and every job state transition (start, finish, recovery)
  require the current owner and fencing token. The lease row is checked inside
  the same transaction as each state mutation; expired or superseded workers
  are rejected.
- `orchestration_audit`: database-enforced append-only lifecycle events without
  payload bodies, credentials, provider response bodies or exception messages.
  Event types are constrained and arbitrary exception names collapse to a
  generic `HANDLER_ERROR` code.
- Reusing an idempotency key with a different job type or payload digest fails.
- Retries are deferred; maximum attempts are capped at five. A later scheduler
  invocation must submit the same idempotency key and payload.
- A stale RUNNING attempt is recovered only after the caller has acquired the
  global lease. It becomes RETRYABLE or FAILED depending on remaining attempts.

## Runtime limits

`run_once` accepts a deadline of 1–240 seconds and a work budget of 1–10,000
items. The lease must outlive the configured deadline by at least 15 seconds.
Handlers must call `context.checkpoint()` at bounded work boundaries and
call `context.assert_fence()` immediately before each externally visible side
effect. Both operations verify the current lease; the context tracks consumed
items in place. Any downstream database mutation must independently include
the owner and fencing token in its own transaction. A checkpoint cannot stop a
handler that ignores the contract or is blocked inside external I/O.

The deadline is cooperative, not an OS-level kill switch. It cannot interrupt a
Python handler blocked inside an external call. Provider HTTP/WebSocket calls
must separately configure connect/read timeouts shorter than the remaining
worker budget. Avoid unbounded streaming and use bounded pages/batches.

## Deployment requirements

- Use a scheduler to invoke one-shot jobs; retries are picked up on later runs.
- Ensure only the manual remote-conformance workflow receives the dedicated
  `TURSO_CONFORMANCE_DATABASE_URL` and `TURSO_CONFORMANCE_AUTH_TOKEN` secrets.
  Never use generic production `TURSO_DATABASE_URL` or `TURSO_AUTH_TOKEN`
  secrets for destructive conformance tests.
- The workflow and test code both enforce the exact disposable host
  `veltrix-h0-conformance.turso.io`, reject URL credentials and extras, and
  never print secrets or include them in artifacts.
- The local SQLite backend remains the default and rollback path. No production
  data is migrated by this phase.

## Explicit non-goals

No wallet keys, signing, transaction construction/submission, live trading,
unbounded worker, automatic infrastructure provisioning, or production DB
migration. This is application-level coordination, not proof of Linux
process/mount isolation or serverless-host equivalence to the Phase 09.2 host
gate.
