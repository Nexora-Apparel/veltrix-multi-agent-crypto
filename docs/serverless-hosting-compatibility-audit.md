# VELTRIX Serverless Hosting Compatibility Audit

**Audit baseline:** `f64a05ff205db25428c91353a89e263b005da632`  
**Repository:** `Nexora-Apparel/veltrix-multi-agent-crypto`  
**Decision status:** Architecture selected for a no-card, $0 deployment prototype. Not deployed.  
**Safety status:** PAPER-only. Live execution, signer invocation, wallet keys and transaction submission remain disabled.

## 1. Non-negotiable constraints

- $0 recurring cost.
- No bank card, payment method, paid trial, or automatic paid renewal.
- Free-tier hard limits must stop work rather than create a bill.
- Preserve the existing paper ledger and audit history.
- Do not store secrets, wallet keys, or provider credentials in Git.
- No claim of uninterrupted real-time discovery from scheduled serverless jobs.
- Do not treat serverless permission checks as proof of Phase 09.2 Linux UID/mount-namespace isolation.

## 2. Repository facts confirmed at the audit baseline

The repository is public. GitHub's current documentation says standard GitHub-hosted runners are free and unlimited for public repositories. The standard `ubuntu-latest` runner is a fresh Linux VM with 4 CPUs, 16 GB RAM and 14 GB SSD. GitHub-hosted jobs have a six-hour execution ceiling.

The existing application is a Python application with these dependencies:
- `requests`, `python-dotenv`, `cryptography`, `eth-hash[pycryptodome]`, `solders`, and `websocket-client`.
- `solders` is already known to be unavailable as a compatible wheel in the user's Android Termux environment, but Linux GitHub runners can install the project's normal requirements.

The current runtime topology is continuous-process oriented:
- `veltrix.solana_meme.worker`: long-running discovery supervisor. It polls DEX Screener and starts a PumpPortal WebSocket supervisor.
- `veltrix.solana_meme.intelligence_worker`: long-running enrichment and market-refresh loop.
- `veltrix.solana_meme.risk_runtime`: long-running risk-evidence loop.
- `veltrix.solana_meme.consensus_runtime`: long-running consensus loop.
- `veltrix.solana_meme.paper_runtime`: long-running paper-trading loop.
- `veltrix.dashboard_server`: persistent Python HTTP server over a read-only SQLite connection.

The workers share one persistent SQLite database, defaulting to `data/solana_discovery.sqlite3`. Database path selection is centralized in `veltrix/solana_meme/database.py`, but most stores and workers still call `sqlite3.connect` directly.

The application relies on SQLite features and semantics, including:
- `sqlite3.Row` named-column row access.
- explicit commit/rollback and context-managed connections.
- `BEGIN IMMEDIATE` claims/leases and atomic paper-decision consumption.
- SQLite DDL, indexes, UPSERT/INSERT OR IGNORE, JSON payload columns and query-only read connections.
- one shared database across launch, market, risk, consensus, paper and dashboard stores.

The current dashboard's Python repository opens a local file path with `Path.is_file()` and SQLite `mode=ro`; it cannot point at a remote database URL unchanged.

## 3. Selected hosting architecture

### Compute: GitHub Actions

Use standard `ubuntu-24.04` or `ubuntu-latest` hosted runners for bounded Linux jobs. Because this repository is public, standard hosted runner usage is documented as free and unlimited. Use no larger runners.

Workflows will:
1. Start on a scheduled or manual trigger.
2. Check out a pinned repository commit.
3. Install pinned Python dependencies.
4. Connect to the remote database using repository Actions secrets.
5. Execute one bounded orchestration cycle.
6. Persist all durable state remotely.
7. Publish run metadata and exit.

No workflow may rely on its local filesystem surviving the job. Temporary files are disposable. A workflow timeout, runner loss or retry must not duplicate paper entries or corrupt the ledger.

### Durable database: Turso Cloud Free

Turso is preferred over Cloudflare D1 for the first migration prototype because its official Python quickstart documents `turso_serverless`, a remote HTTP database driver that implements the Python DB-API interface and requires no persistent connection or native extension. This is materially closer to the existing SQLite-based Python application than a D1-specific API or a PostgreSQL rewrite.

The official Turso pricing page currently advertises a Free plan with:
- $0/month and no credit card required.
- 100 databases.
- 5 GB storage.
- 500 million monthly rows read.
- 10 million monthly rows written.
- 3 GB monthly sync allowance.
- one day of point-in-time restore.

**Billing guard:** remain on the Free plan, do not enable overages, do not add a payment method, and do not upgrade. If the service changes these conditions or requires a card, stop and re-evaluate rather than proceeding.

**Compatibility is not assumed.** Before migration, a driver-conformance spike must prove support for the exact features used by this repository: parameter binding, `sqlite3.Row`-equivalent mapping, explicit transactions, `BEGIN IMMEDIATE`, rollback, context managers, `lastrowid`/row counts where used, and the SQL constructs in the existing stores. The driver is DB-API compatible, but that alone does not prove full sqlite3 behavioural equivalence.

### Dashboard: Cloudflare Pages + Workers

- Pages serves the existing static dashboard assets.
- A Worker exposes the dashboard's read-only API.
- The Worker queries Turso using the JavaScript serverless database driver over HTTP.
- The API returns the same response contract consumed by `dashboard.js`.
- Dashboard credentials are never embedded in the page or query string.
- Ingestion/write routes are separate from dashboard read routes and require a distinct machine credential.

Cloudflare Workers Free is suitable only for the small API/query layer, not the Python analysis pipeline. Current official documentation describes a free-plan CPU allowance of 10 ms per invocation and a daily request limit. Keep Worker logic limited to authentication, bounded SQL queries and JSON serialization.

## 4. Why Cloudflare D1 is not the first database choice

D1 is a credible free serverless SQL database, but the current Python code directly depends on sqlite3 connection objects, row factories, local file paths and transaction semantics. D1 is accessed through a Worker binding/API rather than as a remote Python sqlite3 DB-API connection. Choosing D1 first would require a larger service/repository rewrite and would place more SQL/business logic into JavaScript.

Turso's documented Python remote DB-API driver provides a narrower compatibility experiment. D1 remains a fallback if Turso's driver fails the required conformance tests or its free-plan terms change.

## 5. Required application changes

### A. Database boundary

Introduce one connection factory and explicit database identity abstraction. All production stores must use that boundary rather than calling `sqlite3.connect` directly.

Modes:
- `local`: existing SQLite file, unchanged for development and tests.
- `turso`: remote Turso database via `turso_serverless`, selected only when the required URL/token are present.

The local SQLite path remains the default. Never silently fall back from a failed remote connection to a new local database.

Keep row conversion and exception handling inside the adapter. Preserve transaction boundaries and the atomicity of paper fills and decision-ID consumption.

### B. Bounded orchestration

Add a one-cycle orchestration entry point that calls existing bounded worker methods in dependency order:
1. discovery poll;
2. candidate enrichment and market refresh;
3. risk evidence;
4. consensus;
5. paper trading.

Each stage needs explicit per-cycle limits, timeout budgets, structured counts and failure status. A failed stage must not cause downstream stages to treat missing or stale data as approval.

The existing long-running entry points remain available for local Linux development. The cloud workflow must not start all five infinite loops.

PumpPortal's WebSocket connection must be treated as optional, bounded capture rather than a permanent connection. Scheduled jobs can poll supported feeds and run a short capture window, but they cannot guarantee continuous event delivery. Persist source timestamps, deduplicate candidates and report coverage gaps.

### C. Dashboard API

Keep the existing static UI where possible. Implement a Cloudflare Worker API that preserves the current dashboard endpoint contract:
- `/api/health`
- `/api/overview`
- `/api/positions`
- `/api/trades`
- `/api/decisions`
- `/api/consensus`
- `/api/markets`
- `/api/equity-curve`
- `/api/costs`

The Worker must be read-only against the trading tables. Do not expose a generic SQL endpoint. Enforce bounded limits and prepared statements.

### D. Durable state and recovery

- Use a single canonical Turso database for the current shared ledger unless measured row volume requires a later partitioning design.
- Add a durable run table with run ID, stage, start/end time, status, counts, error class and source watermark.
- Add a global lease/claim strategy so two scheduled workflows cannot process the same cycle concurrently.
- Use idempotency keys for candidate ingestion, evidence refresh, consensus decisions and paper decisions.
- Never depend on GitHub Actions artifacts or commits as the live database.
- Preserve the current SQLite file untouched as the rollback source.


### H0 driver source inspection update

The official `turso_serverless` Python source was inspected after the initial audit. It exposes a DB-API connection/cursor implementation with a configurable `row_factory`, `commit()`, `rollback()`, context-manager methods, `execute()`, `executemany()`, cursor `rowcount` and `lastrowid`, and explicit transaction support including `BEGIN IMMEDIATE`. Its package metadata currently classifies it as Beta (version 0.1.0 in the inspected source). This materially improves the compatibility outlook but is not a substitute for the package's live-database conformance suite or our application-specific tests. The official driver README states that its conformance tests run against a live Turso database and skip when the database URL/token are absent.

Consequently, the next implementation task is a disposable-database H0 conformance spike, not a production data migration. The test must specifically exercise the repository's actual `sqlite3.Row` access patterns through the driver's row factory, multi-statement transactions, rollback, `BEGIN IMMEDIATE` lease claims, duplicate/idempotency constraints, and the existing store initialization SQL.

## 6. Migration safety protocol

1. Stop local writers before taking the source backup.
2. If SQLite WAL mode is active, create a consistent SQLite backup using SQLite's backup API or a checkpointed copy; do not copy only the main database file while WAL/SHM files are active.
3. Record source schema version, table counts, row counts, primary-key ranges and a SHA-256 hash of the backup.
4. Import into a newly created Turso database; never overwrite the original SQLite file.
5. Compare schema, table counts, critical ledger balances, positions, trades, decision IDs and event counts.
6. Run read-only dashboard queries against the migrated database.
7. Run paper-cycle tests against a disposable test database.
8. Keep the source backup and a documented rollback procedure until the remote system passes repeated acceptance runs.

No user database has been uploaded or migrated as part of this audit.

## 7. Security model for the serverless deployment

The serverless deployment is a separate security profile; it does not satisfy the current Phase 09.2 host verifier.

Required controls:
- GitHub Actions secrets only; never commit Turso tokens, provider keys or dashboard credentials.
- Separate credentials for ingestion/write jobs and dashboard read access, if the provider supports scoped tokens. If scope cannot be restricted adequately, put a narrow Worker/API gateway in front of the database and do not expose the write credential to the browser.
- No provider secrets in pull-request workflows from untrusted forks.
- Pin third-party GitHub Actions to reviewed commit SHAs before production use.
- Restrict workflow permissions to read-only repository contents unless a workflow explicitly needs more.
- Use concurrency controls and durable database leases.
- Keep live execution false; do not deploy a signer, private keys or transaction broadcast code.
- Add tests proving dashboard requests cannot mutate records and ingestion credentials cannot invoke any transaction-execution path.

The existing systemd UID/mount-namespace checks remain a separate, unfulfilled host-isolation release gate. A successful serverless deployment must not be described as passing that gate.

## 8. Deployment cadence and reliability limits

GitHub scheduled workflows:
- run only from the repository's default branch;
- have a documented minimum schedule interval of five minutes;
- may be delayed during load;
- can be disabled in public repositories after 60 days without repository activity;
- are not a real-time scheduler or continuous process manager.

The initial target should be a conservative 5–15 minute bounded cycle, subject to provider rate limits and observed job duration. Add manual dispatch, missed-cycle detection, a heartbeat and an alert/report when a cycle is stale. Do not promise second-by-second discovery or guaranteed exact start times.

## 9. Implementation sequence and acceptance gates

### H0 — Audit and driver conformance
- Map every direct SQLite connection and transaction.
- Identify all SQLite-only SQL and row-factory assumptions.
- Prototype `turso_serverless` against a disposable database.
- Test transaction, rollback, row access, row counts and concurrent claims.
- No production data migration.

**Exit:** conformance report passes; no unresolved transaction incompatibility.

### H1 — Database adapter
- Implement local and Turso adapters.
- Convert stores to use the adapter.
- Preserve local SQLite as default.
- Add unit and integration tests for both modes.

**Exit:** full existing suite passes on local SQLite; remote adapter contract tests pass.

### H2 — One-shot cloud orchestration
- Add bounded discovery/intelligence/risk/consensus/paper cycle.
- Add run ledger, global lease, time budgets and idempotent retries.
- Keep WebSocket capture bounded and optional.

**Exit:** repeat, interrupted and concurrent-run tests prove no duplicate paper entries or corrupted state.

### H3 — Remote dashboard API
- Implement Worker read-only API against Turso.
- Reuse static assets and preserve endpoint response contracts.
- Add authentication and authorization tests.

**Exit:** dashboard loads all required views and every write method is rejected.

### H4 — CI and free-tier deployment
- Add workflow_dispatch first; scheduled operation only after manual runs pass.
- Add Turso secrets through GitHub settings.
- Add Cloudflare Pages/Worker deployment configuration.
- Confirm provider dashboards show Free plan and overages disabled before production data is migrated.

**Exit:** repeatable deployment and health checks with no card, no payment method, no paid plan and no overage setting.

### H5 — Data migration and shadow operation
- Back up and migrate the existing SQLite ledger.
- Compare counts and critical balances.
- Run cloud cycles in shadow/paper mode.
- Keep original SQLite database as rollback source.

**Exit:** migration reconciliation passes and at least seven days of operational evidence is collected before considering the cloud deployment the primary paper environment.

## 10. Official references

- GitHub Actions billing: https://docs.github.com/en/actions/concepts/billing-and-usage
- GitHub hosted runner selection: https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job
- GitHub Actions limits: https://docs.github.com/en/actions/reference/limits
- GitHub scheduled workflow events: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- Turso Free plan: https://turso.tech/pricing
- Turso Python SDK quickstart and remote DB-API driver: https://github.com/tursodatabase/turso-docs/blob/main/sdk/python/quickstart.mdx
- Cloudflare Workers limits: https://developers.cloudflare.com/workers/platform/limits/
- Cloudflare D1 limits (fallback only): https://developers.cloudflare.com/d1/platform/limits/

---

**Audit conclusion:** The strongest first implementation path is GitHub Actions (bounded Linux compute) + Turso Free (remote SQLite-compatible durable database) + Cloudflare Pages/Workers (static dashboard and narrow read-only API). Turso is a better initial database fit than D1 because the existing Python system is deeply coupled to sqlite3. This remains a prototype decision until the remote DB-API conformance spike proves the transaction and row-access behaviour required by the current paper ledger.
