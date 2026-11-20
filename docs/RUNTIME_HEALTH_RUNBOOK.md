# VELTRIX bounded pipeline operations runbook

## Purpose

This runbook describes the selected no-VPS operating model. VELTRIX performs bounded, operator-initiated pipeline runs on GitHub-hosted Actions runners, persists shared state in Turso, and serves its dashboard/API through Cloudflare Pages and Functions.

**A permanently running Python worker, Docker host, paid VPS, or free VPS is not required.** A run continues on GitHub's hosted runner if the operator's phone is switched off or loses internet after the run has started. The run ends when the bounded job completes, times out, fails, or is cancelled.

## Selected hosting architecture

| Component | Responsibility | Runtime behavior |
|---|---|---|
| Veltrix dashboard | Operator control and read-only monitoring | Cloudflare Pages |
| Dashboard/API functions | Authenticated read-only API; future secure run-control endpoint | Cloudflare Pages Functions |
| Pipeline execution | Bounded Python discovery, enrichment, market refresh, risk and consensus | GitHub-hosted Actions runner |
| Shared persistence | Run records, leases/fencing, market/evidence state and telemetry | Turso |
| Operator device | Activate, inspect and stop runs | Browser/Android; not a compute host |

Do not provision a VPS or search for a free VPS as a prerequisite. Dockerfile/Compose worker packaging, if retained, is optional for local development or a separately approved deployment experiment only. It must not be a required CI, release, or production acceptance dependency.

## Current activation boundary

- `.github/workflows/solana-pipeline-schedule.yml` is manual `workflow_dispatch` only.
- It runs on `ubuntu-24.04`, has a 10-minute job timeout and a concurrency group, and calls `python scripts/run_solana_pipeline.py`.
- The script calls `OneShotSolanaPipeline.run_once()`; the pipeline executes bounded stages under the existing global orchestration lease/fencing contract.
- `SCHEDULER_ENABLED=false`, `PAPER_TRADING_ENABLED=false`, and `LIVE_TRADING=false` are explicit workflow settings.
- Until the dashboard-to-GitHub Actions control endpoint is implemented and verified, the workflow can be manually dispatched from GitHub Actions. Do not claim that dashboard activation/deactivation is already connected.
- Automatic cron/scheduled execution must remain disabled. Do not bypass this with Cloudflare Cron or another scheduler.

## Intended operator flow

1. Operator opens the Veltrix dashboard while online and activates one bounded run.
2. The future dashboard control endpoint authenticates the operator and requests a GitHub Actions workflow dispatch using a server-side credential. The credential must never be sent to the browser or Android app.
3. GitHub starts the bounded job on its hosted runner. The runner uses the production Turso credentials and approved read-only provider credentials injected through GitHub Actions secrets.
4. The operator may close the dashboard, disconnect the phone or turn the phone off. The GitHub-hosted job continues independently.
5. Each stage records its real outcome and run correlation in Turso. The job exits at completion or its configured timeout.
6. When the operator reconnects, the dashboard reads the persisted result and displays the last run and stage outcomes.
7. Deactivation prevents another run from being requested and may cancel an in-progress GitHub run. It must not delete historical records or fabricate a successful stop event.

### Manual fallback before dashboard control is delivered

Use the repository's GitHub Actions page to run the manual workflow. This is a temporary operator path, not the intended final dashboard experience. Do not enable a recurring schedule.

## Bounded execution and reliability requirements

- Preserve the one-shot pipeline's fixed stage order and hard runtime/item limits.
- Preserve the global lease, fencing-token assertions, idempotency key and durable run/attempt identity.
- Use GitHub run ID plus run attempt as the external invocation identity; never use a date/hour-only key that permanently deduplicates a later operator retry.
- Overlapping dispatches must not execute concurrent mutations. GitHub concurrency and the Turso global lease are independent protection layers.
- Retry only through a new bounded invocation after the prior attempt has a durable RETRYABLE/FAILED outcome. Never silently loop forever inside one Actions job.
- Keep provider calls bounded, with explicit timeouts, retry/backoff and sanitized failure codes.
- The existing market-refresh one-shot path performs an independent DexScreener health probe even when the refresh queue is empty. A newly constructed bounded process can probe once per run; it does not need a persistent 60-second timer.
- A successful HTTP probe is endpoint-connectivity evidence only. It does not prove that token data is available, complete, current, safe or tradable.
- If an Actions job is cancelled or times out, the next invocation must recover through the existing fenced orchestration state. Never reset queues or erase history to make a retry pass.

## Telemetry semantics

The dashboard must not interpret a completed one-shot run as a continuously running process.

- A run-level record represents one actual bounded pipeline invocation, with run ID, attempt, start/finish time and terminal outcome.
- Stage-level records represent actual stage completion or failure within that invocation.
- `WORKER_HEARTBEAT` in this operating model means a completed bounded stage/cycle. It is not proof of a live process after the Actions job exits.
- `AGENT_HEARTBEAT` is written only when that agent actually completes an evaluation/vote. Empty queues do not produce synthetic agent heartbeats.
- `PROVIDER_PROBE` is independent from market-batch observations and carries its actual observation time and sanitized outcome.
- A stopped/idle system after a completed run is normal. The dashboard should show last-run age/outcome and schedule-disabled state, not label the absence of a permanently running process as a worker failure.
- A currently executing run may be shown only when its state is obtained from authoritative GitHub Actions run data; otherwise show the last persisted run and clearly mark execution state as unknown.
- Preserve append-only history. Do not backdate, seed, delete or rewrite telemetry to manufacture freshness.

**Implementation note:** PR #226 added worker-cycle telemetry and optional perpetual process entry points. The one-shot orchestrator currently calls stage methods directly, so run/stage-scoped telemetry must be wired into that orchestrator and covered by tests before the dashboard can claim complete bounded-run telemetry. Until then, do not infer full stage coverage from the optional worker entry points.

## Required credentials and secret handling

Inject credentials through GitHub Actions repository/environment secrets or Cloudflare encrypted bindings, as appropriate. Never put secrets in source, workflow logs, dashboard JavaScript, mobile assets, Docker build arguments, or chat.

- `TURSO_DATABASE_URL`: production `libsql://*.turso.io` URL.
- `TURSO_AUTH_TOKEN`: least-privilege token for the bounded pipeline's required writes.
- `TURSO_READONLY_AUTH_TOKEN`: read-only token for diagnostic inspection jobs.
- `SOLANA_RPC_URL` or `HELIUS_API_KEY`: configured Solana read access.
- `GOPLUS_ACCESS_TOKEN`: optional security-provider access where configured.
- `HELIUS_API_KEY`, `TRUSTED_WALLET_PROVIDERS`, and `VELTRIX_TRUSTED_SOLANA_WALLETS`: only when explicitly enabling the trusted wallet evidence path.

Do not paste secret values into chat. Verify presence and permissions without exposing values. Reuse the existing Turso account/database where available; do not create a new database or migrate production data without an explicit, separately verified migration plan.

## Dashboard-to-workflow control security requirements

The future control endpoint must:
- require the dashboard's authenticated operator session and CSRF/origin protections;
- accept only a fixed allowlisted workflow and approved input fields;
- keep the GitHub App/private token or fine-grained credential in Cloudflare encrypted server-side bindings;
- scope the credential to dispatch/read the required workflow only;
- reject arbitrary workflow names, refs, shell commands, secret values and unbounded parameters;
- return a GitHub run ID only after GitHub confirms dispatch acceptance;
- expose read/cancel operations only for this repository's authorized pipeline runs;
- never expose repository secrets or privileged GitHub tokens to the browser;
- retain an audit record of operator action, workflow ref and GitHub run identity.

The dashboard control is not complete until authenticated dispatch, status polling and cancellation are tested end-to-end.

## Acceptance gates before operational activation

1. Exact-head Python CI and Cloudflare Pages CI pass.
2. Disposable Turso conformance passes against a dedicated disposable database; production database is not used for conformance writes.
3. Production Turso schema initialization is guarded and independently reviewed; no production migration occurs until source data and rollback are reconciled.
4. Manual dispatch produces one bounded run with a durable run ID, attempt, lease/fencing record and terminal outcome.
5. Empty queues still produce truthful run/stage telemetry and the independent provider probe.
6. Injected provider failure produces sanitized FAILED/DEGRADED evidence; a later bounded retry recovers without duplicate mutations.
7. Concurrent dispatch attempts are serialized/fenced and cannot create duplicate work.
8. Dashboard output reconciles with the same Turso snapshot and authoritative GitHub run state where available.
9. Live Solana provider smoke tests run explicitly and are not counted as passed when skipped.
10. Base and BSC collection acceptance are separate; never mark them healthy based on Solana-only evidence.
11. Physical Android testing remains a separate hardware acceptance item.

## Stop, cancellation and rollback

- To stop future processing, disable the dashboard dispatch control and keep automatic scheduling disabled.
- To stop an active run, request cancellation through the authorized control endpoint once implemented, or use GitHub Actions cancellation as the manual fallback.
- A cancelled job may leave an interrupted durable attempt; the next authorized run must use the existing recovery/fencing logic.
- Roll back application code through a reviewed commit/revert. Do not delete Turso events, reset queues, clear leases manually, fabricate heartbeats or weaken gates.
- Keep `SCHEDULER_ENABLED=false`, `PAPER_TRADING_ENABLED=false`, and `LIVE_TRADING=false` throughout this stage.

## Optional local worker packaging

`Dockerfile.worker` and `compose.worker.yaml` may be retained for local development or future separately approved use. They are not part of the selected production hosting path and must not be required to build, test, deploy or operate the no-VPS pipeline.
