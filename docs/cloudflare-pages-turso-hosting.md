# Cloudflare Pages + Turso read-only dashboard

## Runtime split

- GitHub Actions runs the bounded Python discovery/enrichment/market/risk/consensus pipeline as a scheduled one-shot job.
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
2. Run the application schema initializer using the rebased Python adapter and verify all Phase 03–07 tables/indexes.
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
