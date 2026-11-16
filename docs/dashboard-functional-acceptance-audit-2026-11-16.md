# Dashboard functional acceptance audit — 2026-11-16

## Scope and baseline

This audit begins the post-deployment functional acceptance stage for the Cloudflare Pages dashboard. It is a source-level and deployment-record audit, not a claim that every live browser interaction has been exercised.

- Repository: `Nexora-Apparel/veltrix-multi-agent-crypto`
- Audited branch baseline: `main`
- Audited commit: `7c657a97533b06518aeac5057f4d9f4e20437a83`
- Production URL: https://veltrix-multi-agent-crypto.pages.dev
- Deployment workflow: [run 43945852263](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43945852263)
- Deployment result: completed / success on the audited commit.
- Pages deployment job verified the homepage, unauthenticated API denial, authenticated health response, connected read-only database, initialized paper ledger, no missing paper tables, and live execution disabled.

## Safety invariants

These are release invariants for every acceptance test and follow-up change:

- Scheduler remains disabled.
- Paper trading remains disabled.
- Live trading/execution remains disabled.
- Phantom signing, transaction submission and broadcast remain disabled.
- Dashboard API remains GET-only and database access remains read-only.
- No Cron trigger, scheduled worker, or automatic pipeline invocation may be added.
- Tests use mocked clients, isolated fixtures, or read-only production requests only.

## Findings

### 1. Dashboard sections and navigation

The canonical Pages source is `veltrix/dashboard_assets/index.html`, copied into `dist/index.html` by `scripts/build-pages.mjs`.

The current page is a single analytics screen. It contains:
- Portfolio metrics and realized equity curve.
- Open positions.
- Trading costs.
- Paper execution decisions.
- Multi-agent consensus activity.
- Latest Solana market snapshots.
- Recent paper fills.
- Remote dashboard access/token controls.

There is no section navigation, chain selector, or distinct route/view for Solana, Base, or BSC. The brand link returns to `/`; the only other top-level action is Refresh.

**Status:** Existing single-page sections are source-identified. Navigation acceptance is blocked by missing chain navigation/views.

### 2. Agent status and data retrieval

The Pages API currently allows only:
- `/api/health`
- `/api/overview`
- `/api/positions`
- `/api/trades`
- `/api/decisions`
- `/api/consensus`
- `/api/markets`
- `/api/equity-curve`
- `/api/costs`

Consensus rows expose persisted supporting-agent names and decision metadata. There is no dedicated live agent-status endpoint, heartbeat/last-seen contract, per-agent provider readiness, or agent diagnostic view. Persisted consensus activity must not be presented as current agent health.

The browser refresh requests all nine endpoints with `Promise.all`. If any request fails, the whole refresh enters the shared error state; successful sections are not independently refreshed.

**Status:** Existing read-only retrieval contract is source-identified. Dedicated agent health and per-section failure isolation are missing.

### 3. Solana, Base and BSC intelligence

The Pages API's current market, risk, consensus and latest-update queries are Solana-specific. The production initializer defines and verifies 20 tables, including Solana discovery/enrichment/market/risk/consensus queues and the paper ledger. No dedicated Base or BSC intelligence tables are included in that production schema.

The current dashboard therefore cannot truthfully render Base/BSC persisted intelligence from its existing API/schema. Repository-level Base research and qualification modules exist, but they are not wired into this Pages API contract. A corresponding BSC dashboard data contract was not found in the audited dashboard/schema paths.

**Status:** Solana market/consensus view exists. Base and BSC dashboard views are not implemented. Do not fabricate empty chain health or sample market rows as live intelligence.

### 4. Database-backed history and diagnostics

Current persisted history endpoints cover paper positions, trades, paper decisions, consensus decisions, market snapshots, equity curve, and modeled costs. Health reports database connectivity, read-only status, available table names, paper schema completeness, and live-execution false.

There is no dedicated diagnostics endpoint for provider connectivity, agent heartbeat, stale pipeline queues, last successful ingestion per chain, or API latency/error summaries.

The API handler uses parameterized values for mint lookups, a strict route allowlist, GET-only enforcement, bearer authentication, and generic 503 errors that do not return database exception details.

**Status:** Existing paper/Solana history and basic health checks are implemented. Comprehensive multi-chain system diagnostics are missing.

### 5. Authentication, failure states and mobile responsiveness

Source-level controls observed:
- Missing/invalid production configuration fails closed.
- Missing or incorrect bearer token returns 401 before database queries.
- Non-GET methods return 405.
- Unknown routes return 404.
- Database errors return generic 503.
- Browser stores the dashboard bearer token in tab-scoped `sessionStorage`, not persistent local storage.
- Responsive CSS includes breakpoints at 1100px and 650px.
- The browser has a refresh-in-progress guard and a shared request-failure state.

Not yet verified by a live browser/device run:
- Actual navigation and layout at common phone widths.
- Token entry, save, clear, reload and expiry behavior on mobile browsers.
- Runtime 401, 404, 405, 503 and offline/network-failure rendering.
- Whether long token/decision/consensus values remain usable in narrow tables.
- Keyboard and screen-reader interaction of the access panel and status updates.

**Status:** Security behavior is partly covered by automated API tests and was checked at deployment for unauthenticated/authenticated health. Full mobile/browser acceptance remains open.

### 6. Execution isolation

The Pages API route allowlist contains only read endpoints. The handler rejects non-GET requests before route processing and uses a read-only Turso credential in the Pages runtime. The deployment workflow explicitly binds `TURSO_READONLY_AUTH_TOKEN` to the Pages runtime as `TURSO_AUTH_TOKEN`, rather than the worker/initializer write token. The production deployment verification reports `database_read_only=true` and `live_execution=false`.

The current dashboard contains no order-entry, signing, transaction-submit, broadcast, scheduler, or paper-trading activation controls.

**Status:** Read-only API and production read-only database boundary were verified by the deployment workflow. Full UI/API negative-path regression tests remain required after dashboard expansion.

## Acceptance matrix

| Area | Current result | Remaining work |
|---|---|---|
| Production Pages deployment | PASS | None for deployment milestone |
| Existing single-page sections | SOURCE REVIEWED | Browser interaction sweep |
| Navigation routes | GAP | Add chain navigation and route/view tests |
| Agent status | GAP | Define live health/heartbeat contract and diagnostics |
| Solana intelligence | PARTIAL | Add risk evidence, freshness and pipeline diagnostics to view |
| Base intelligence | GAP | Identify durable source/schema and add read-only API/view |
| BSC intelligence | GAP | Identify durable source/schema and add read-only API/view |
| Paper/history views | PARTIAL | Validate empty/populated/history edge cases against fixtures |
| System diagnostics | GAP | Add read-only diagnostics contract |
| Auth boundary | DEPLOYMENT PASS / TESTS EXIST | Full browser token lifecycle and expiry tests |
| Failure states | PARTIAL | Exercise 401/404/405/503/network failures in browser |
| Mobile responsiveness | CSS SOURCE REVIEWED | Real-device viewport and interaction test |
| Execution isolation | DEPLOYMENT PASS | Add negative tests for every future action surface |

## Test execution limitations

The repository's Node test suite is `npm test` (`node --test cloudflare-tests/*.test.js`). The existing dashboard API test source was inspected. This audit environment could not clone the repository or reach the live Pages host from its browser/network tools, so it did not independently execute `npm test`, `npm run build:pages`, or a real-device browser session. The successful production deployment workflow remains the evidence for the deployment and its built-in production checks; it is not a substitute for the outstanding UI acceptance tests.

## Next implementation sequence

1. Keep the dashboard read-only and add a chain-aware navigation shell without changing execution controls.
2. Define a truthful chain capability/status contract; never show a chain as healthy merely because its tab renders.
3. Identify and verify durable Base/BSC data stores before adding API queries. If a chain has no persisted data, display an explicit unavailable/not-integrated state.
4. Add per-agent status and system diagnostics from real heartbeat/provider/queue evidence.
5. Isolate browser section failures so one endpoint does not blank all dashboard data.
6. Add tests for route allowlists, auth, methods, malformed/stale data, chain isolation, and execution-negative guarantees.
7. Run CI, build and deployment verification; then complete live mobile/browser acceptance.

## Documentation reconciliation

This audit is the current source-level acceptance record for the dashboard. Earlier sections of `production-acceptance-progress-2026-11-16.md` and `production-acceptance-runbook.md` contain pre-deployment statements and must not be read as the current deployment status. The verified deployment run above supersedes those historical statements for the Pages deployment milestone.
