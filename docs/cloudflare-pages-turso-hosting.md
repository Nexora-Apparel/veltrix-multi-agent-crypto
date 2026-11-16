# Cloudflare Pages + Turso read-only dashboard

## Runtime split

- GitHub Actions runs the bounded Python discovery/enrichment/market/risk/consensus pipeline through a manual `workflow_dispatch` one-shot job; automatic scheduling is disabled by release policy.
- Turso is the shared remote persistence target after the reviewed H1.1/H2/H3 adapter integration is rebased onto current `main`, revalidated, and accepted.
- Cloudflare Pages serves the existing static dashboard assets from `veltrix/dashboard_assets`.
- Cloudflare Pages Functions implements the dashboard's existing read-only `/api/*` contract. It uses the web-only `@libsql/client/web` entry point to query Turso; the database token never reaches the browser.
- No signer, private key, transaction construction, transaction submission, broadcast or live-trading endpoint is added.

## Local checks

Install the pinned Node dependencies:

```sh
npm ci
npm test
npm run pages:dev
```

The Pages dev server requires local secrets in an untracked `.dev.vars` file:

```dotenv
TURSO_DATABASE_URL=libsql://your-database.turso.io
TURSO_AUTH_TOKEN=your-read-only-database-token
DASHBOARD_API_TOKEN=at-least-32-random-characters
```

Never commit `.dev.vars` or production credentials.

## Required production bindings

Configure these as Cloudflare Pages production secrets, not plain vars:

- `TURSO_DATABASE_URL`: the separately provisioned production Turso endpoint.
- `TURSO_AUTH_TOKEN`: a **read-only** token scoped to that production database.
- `DASHBOARD_API_TOKEN`: a random dashboard access secret of at least 32 characters.

Set `VELTRIX_DASHBOARD_MARKET_MAX_AGE_MS` only as a non-secret variable if the default 120,000 ms is unsuitable.

Preview deployments must not receive production credentials. Use separate preview credentials and a non-production database or leave the preview secrets unset so the API fails closed.

## Database acceptance before cutover

1. Create a separate production database; never reuse the disposable H0 conformance database.
2. Run the application schema initializer using the rebased Python adapter and verify all expected application tables and the required named indexes.
3. Validate the read-only token against the actual database and confirm that the Pages API can perform SELECT queries only.
4. Migrate data from the selected SQLite source only after export/import row counts, uniqueness constraints, timestamps, append-only ledgers and paper state reconcile.
5. Retain the original SQLite file and a tested restore copy until production cutover is accepted.

The Pages Function is deliberately read-only. It does not initialize schema, create tables, migrate data or perform paper mutations.

## Deployment

Cloudflare account/project binding and secrets are operator-provisioned. Once configured:

```sh
npm run pages:deploy
```

The deployment is not accepted until the production URL serves the dashboard, authenticated API health reports the correct database and read-only state, unauthenticated calls return 401, and the end-to-end persistence/recovery checks pass.

## Automated deployment and production scheduling

The `.github/workflows/cloudflare-pages-deploy.yml` workflow creates the Pages project if needed, uploads the production-only secrets, deploys the static bundle plus Pages Function, then verifies the live dashboard and authenticated read-only database health.

Before deployment, the workflow performs a no-row UPDATE inside a rolled-back transaction and requires Turso to reject it. This verifies that the configured dashboard token is read-only; a write-capable token blocks deployment. The repository must have these GitHub Actions secrets configured before dispatching deployment:

| Secret | Purpose |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token with Pages Read and Pages Write |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account identifier |
| `TURSO_DATABASE_URL` | Separate production Turso endpoint |
| `TURSO_AUTH_TOKEN` | Production read/write token for the GitHub Actions pipeline |
| `TURSO_READONLY_AUTH_TOKEN` | Separate read-only token for the dashboard API |
| `DASHBOARD_API_TOKEN` | Random dashboard bearer token, at least 32 characters |
| `SOLANA_RPC_URL` or `HELIUS_API_KEY` | Required by the Phase 05 risk-evidence collector |

Optional `GOPLUS_ACCESS_TOKEN` can be added for the optional GoPlus evidence adapter. Provider allowlists remain empty until each provider has been separately vetted; do not add trust IDs merely to make a health check pass.

The Cloudflare deployment workflow copies the three dashboard secrets into the Pages **production** environment. Preview deployments receive no production credentials and fail closed.

### Production initialization and scheduled execution

1. Add the GitHub Actions secrets above without posting their values in chat or committing them to the repository.
2. Run `Initialize production Turso schema` manually from Actions. The script refuses the dedicated disposable conformance database, creates the application tables, creates an empty paper ledger account if absent, and verifies the expected tables and required named indexes. It does not run providers or activate paper trading.
3. Run `Deploy VELTRIX Cloudflare Pages` manually. It creates the Pages project, sets production-only read-only bindings, deploys, and verifies the authenticated health response.
4. The Solana pipeline is exposed as a **manual workflow_dispatch only**. Automatic schedule triggers are deliberately blocked by the existing Stage 9.1 release policy and regression test. A two-hour UTC cadence is the proposed post-release setting, not an active schedule. Do not bypass the lock by substituting Cloudflare Cron or another external scheduler.
5. The manual pipeline runs discovery, enrichment, market refresh, risk evidence and consensus only. Paper trading, Telegram, signing, transaction submission and live execution remain disabled.

The manual one-shot pipeline requires a **read/write** production Turso token because it persists candidate, market, risk, consensus and orchestration records. The Cloudflare dashboard uses a distinct **read-only** token. Never reuse the disposable conformance token or database for production.

### Existing SQLite data

The repository does not contain a tracked SQLite database file. The initializer creates a new empty production schema and paper account; it does not import an untracked local database. If a local SQLite ledger contains records that must be retained, provide that exact database file for a separately tested export/import and reconciliation before production cutover. Keep the original file and a verified backup until migration acceptance.
