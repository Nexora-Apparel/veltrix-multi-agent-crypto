# VELTRIX production acceptance runbook

**Scope:** Turso production schema, Cloudflare Pages dashboard and the manually invoked Solana intelligence pipeline.

**Current release state:** PR #195 is open, draft and unmerged. This runbook is an execution plan, not evidence that production has been initialized or deployed. Do not dispatch production workflows until the hosting PR is integrated into `main`, the target database and credentials have been independently checked, and the operator explicitly authorizes that production action.

## 1. Non-negotiable safety state

The following controls must remain in force throughout hosting acceptance:

- `TELEGRAM_ENABLED=false`
- `SCHEDULER_ENABLED=false`
- `LIVE_TRADING=false`
- `PAPER_TRADING_ENABLED=false`
- No signing, transaction submission, broadcast or live-execution path.
- No GitHub cron trigger and no Cloudflare Cron replacement.
- The Solana pipeline remains `workflow_dispatch` only.

A successful schema initialization or dashboard health check does not authorize trading, paper execution, Telegram notifications or scheduled operation.

## 2. Gate 0 — repository integration

- [ ] Confirm PR #195 is the reviewed hosting PR and its head has not changed unexpectedly.
- [ ] Review the final diff and all required checks on the exact final head.
- [ ] Integrate the PR into `main` only through the repository's explicitly authorized merge process.
- [ ] Confirm `.github/workflows/turso-production-init.yml` is now present on `main`. GitHub cannot manually dispatch a `workflow_dispatch` workflow that exists only on a feature branch.
- [ ] Confirm the production initializer, Pages deployment and manual pipeline workflows still declare the safety environment variables above.

**Stop condition:** If the workflow is absent from `main`, do not retry its Actions URL. If the PR head changes, rerun the relevant checks against the new exact head.

## 3. Gate 1 — credential inventory

Configure GitHub Actions secrets by role. Never paste secret values into chat, logs, commits or screenshots.

| Secret | Role | Required for |
|---|---|---|
| `TURSO_DATABASE_URL` | Production `libsql://*.turso.io` endpoint | Initializer, Pages, pipeline |
| `TURSO_AUTH_TOKEN` | Production read/write token | Initializer and pipeline |
| `TURSO_READONLY_AUTH_TOKEN` | Separate read-only token for the same production database | Pages deployment |
| `DASHBOARD_API_TOKEN` | Dashboard bearer credential, minimum 32 characters | Pages deployment |
| `CLOUDFLARE_API_TOKEN` | Cloudflare Pages API access with required Pages permissions | Pages deployment |
| `CLOUDFLARE_ACCOUNT_ID` | Target Cloudflare account | Pages deployment |
| `SOLANA_RPC_URL` or `HELIUS_API_KEY` | Solana data provider credential | Manual pipeline |
| `GOPLUS_ACCESS_TOKEN` | Optional GoPlus evidence adapter | Optional pipeline enrichment |

Credential checks:

- [ ] Confirm `TURSO_DATABASE_URL` is the intended production endpoint, not the H0 disposable conformance host.
- [ ] Confirm `TURSO_AUTH_TOKEN` is the saved production read/write token. Do not overwrite or delete a working token merely to rename it.
- [ ] Confirm `TURSO_READONLY_AUTH_TOKEN` is a distinct read-only token for that same production database.
- [ ] Confirm the dashboard bearer token is random and at least 32 characters.
- [ ] Confirm the Cloudflare token is scoped to the intended account and Pages operations.
- [ ] Confirm Solana provider credentials only if the manual pipeline is to be run.
- [ ] Confirm no conformance token or endpoint has been copied into production secrets.

The assistant cannot inspect masked GitHub secret values. A configured secret name is not proof that its value, permissions or database scope are correct.

## 4. Gate 2 — database preflight and data preservation

- [ ] Confirm the production Turso database exists and its hostname matches the intended production endpoint.
- [ ] Confirm the database is separate from the disposable H0 conformance database.
- [ ] Determine whether any local SQLite ledger or paper-state records must be retained.
- [ ] If records must be retained, export and back up the exact source SQLite database before initializing production. Record source row counts and a checksum.
- [ ] Test restoration of that backup in a disposable environment.
- [ ] Plan a separate export/import and reconciliation procedure for any retained data. Reconcile row counts, primary/unique keys, timestamps, append-only event history and paper state before cutover.
- [ ] Keep the original SQLite file and a verified backup until migration acceptance is signed off.

**Important:** `scripts/initialize_turso_database.py` creates the production schema and an initial paper account row if absent. It does not migrate a local SQLite ledger, import historical records, call market providers or enable paper trading. Do not treat `SCHEMA_READY` as data-migration acceptance.

## 5. Gate 3 — production schema initialization

Only after Gates 0–2 are satisfied:

1. Open GitHub Actions on the default branch.
2. Select **Initialize production Turso schema**.
3. Dispatch it once against the intended default-branch revision.
4. Inspect the workflow result and retain its logs.

Acceptance evidence:

- [ ] Workflow completes successfully.
- [ ] Output status is `SCHEMA_READY`.
- [ ] Output database host matches the approved production host.
- [ ] `tables_verified` reports 20 expected tables.
- [ ] `indexes_verified` reports 6 required named indexes.
- [ ] `paper_ledger_initialized` is true.
- [ ] `paper_trading_enabled` is false.
- [ ] `live_execution_enabled` is false.
- [ ] `data_migration_performed` is false.

The initializer is additive schema setup, not a general migration or backup tool. Stop if the host, counts or safety flags differ from these expectations.

## 6. Gate 4 — Cloudflare Pages deployment

Prerequisites:

- Production schema initialization has passed.
- The Pages secrets listed in Gate 1 are configured.
- The read-only token belongs to the same production database as the worker token.

Then dispatch **Deploy VELTRIX Cloudflare Pages** from `main`, or allow its approved `main` path-triggered deployment after the reviewed change is integrated.

The workflow must:

- Verify all required deployment secrets are present.
- Build the Pages static bundle.
- Run the read-only token probe, including a harmless no-row UPDATE inside a rolled-back transaction that must be rejected.
- Bind the read-only token as the Pages `TURSO_AUTH_TOKEN` secret; the production worker token must not be sent to the browser runtime.
- Deploy the static assets and Pages Functions.
- Verify the public authentication boundary and authenticated health endpoint.

Acceptance evidence:

- [ ] Deployment workflow completes successfully.
- [ ] Dashboard root serves the VELTRIX page.
- [ ] Unauthenticated `/api/health` returns HTTP 401.
- [ ] Authenticated `/api/health` returns HTTP 200.
- [ ] Health payload reports `ok=true`.
- [ ] Health payload reports `database=connected` and `database_read_only=true`.
- [ ] Health payload reports `paper_ledger_initialized=true`.
- [ ] Health payload reports `live_execution=false`.
- [ ] Health payload reports an empty `missing_paper_tables` list.
- [ ] No production database token appears in browser assets, client responses or deployment logs.

Do not accept a successful static build alone as a successful production deployment.

## 7. Gate 5 — optional bounded pipeline acceptance

This gate is separate from database initialization and dashboard deployment. Run it only after production persistence and provider credentials are accepted.

- [ ] Confirm the workflow is still `workflow_dispatch` only.
- [ ] Confirm Telegram, scheduler, live trading and paper trading are false in the workflow environment.
- [ ] Confirm provider allowlists and trust identifiers have been independently vetted; do not populate them merely to make health checks pass.
- [ ] Dispatch one bounded pipeline attempt manually.
- [ ] Confirm it writes only the intended discovery, enrichment, market, risk, consensus and orchestration records.
- [ ] Confirm no order, paper position, Telegram message, signature or broadcast is produced.
- [ ] Record provider failures and rate limits without repeatedly dispatching the job.

A pipeline run is not required to prove that the schema and read-only dashboard are healthy.

## 8. Recovery and rollback

- **Initializer failure:** Stop. Do not repeatedly dispatch against production. Preserve logs and inspect the first failing validation. The initializer does not migrate the local SQLite ledger.
- **Read-only token probe failure:** Do not deploy Pages. Reissue or correct a dedicated read-only token, verify its database scope, then rerun the probe.
- **Pages health failure:** Keep the dashboard unaccepted. Preserve the previous known-good Pages deployment and use the Cloudflare deployment controls to restore it; do not weaken API authentication or substitute a read/write token.
- **Data mismatch:** Stop cutover. Retain the source SQLite file and tested backup. Reconcile using a separately tested migration/restore procedure.
- **Unexpected execution or notification:** Treat it as a release incident. Disable the affected workflow or credential immediately and preserve logs for investigation.

This PR does not provide a destructive schema rollback or a local-SQLite-to-Turso migration command. Do not improvise a production database restore or delete tables to reverse a failed acceptance attempt.

## 9. Final release record

Record these separately; do not collapse them into one generic “deployed” status:

| Acceptance item | Required evidence |
|---|---|
| Repository integration | PR merge commit and exact `main` SHA |
| Credential readiness | Secret names present and roles independently verified; values remain private |
| Schema readiness | Initializer run URL, commit SHA and `SCHEMA_READY` output |
| Data migration | Separate migration report, or explicit `not performed` |
| Pages deployment | Deployment run URL and production URL |
| API security | 401 unauthenticated / 200 authenticated, read-only health payload |
| Pipeline | Separate manual run URL, or `not run` |
| Safety controls | Telegram/scheduler/paper/live execution remain disabled |

**Current status at authoring:** all production gates are pending. PR #195 is not merged; no production schema initialization, data migration, Cloudflare deployment or production pipeline execution is claimed by this document.
