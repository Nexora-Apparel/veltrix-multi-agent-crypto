# Cloudflare dashboard/API edge deployment

## Topology

- Cloudflare Workers Static Assets serves the existing dashboard files from
  `veltrix/dashboard_assets`.
- The Worker proxies only the explicit read-only `GET /api/*` allowlist to
  the Python dashboard origin over HTTPS.
- The Worker injects `VELTRIX_DASHBOARD_TOKEN` server-side. The API token is
  never shipped to browser JavaScript or stored in sessionStorage.
- The Python dashboard remains the authorization and read-only SQL boundary.
  The pipeline supervisor remains a separate persistent process and uses the
  shared Turso/SQLite database.
- Cloudflare Access must protect the entire dashboard hostname and API paths.
  Configure an allow policy for the intended user identity, then test both
  authenticated and unauthenticated requests. Do not rely on an unguessable
  URL or on the Worker route alone as user authentication.

## Worker bindings

Set these as encrypted Worker secrets/variables; never commit actual values:

- `VELTRIX_API_ORIGIN`: HTTPS origin of the deployed Python dashboard server,
  e.g. `https://api-origin.example.net` (no path).
- `VELTRIX_DASHBOARD_TOKEN`: the same strong token configured on the Python
  dashboard server; at least 32 random characters.

For the Python dashboard's Turso connection, set
`TURSO_DASHBOARD_AUTH_TOKEN` in `/etc/veltrix/dashboard.env` to a dedicated
Turso read-only token when the database provider supports read-only tokens.
The pipeline keeps its separate `TURSO_AUTH_TOKEN`. If a separate read-only
token is unavailable, the dashboard's remote DB-API wrapper still rejects
every statement except a single `SELECT`; do not expose a write endpoint.

The Wrangler configuration sets `workers_dev=false`: the Worker is not
published on a public `workers.dev` hostname. Attach it only to the intended
custom domain after Cloudflare Access is configured and tested.

Deploy with Wrangler from the repository root after installing Wrangler:

```sh
npx wrangler secret put VELTRIX_API_ORIGIN
npx wrangler secret put VELTRIX_DASHBOARD_TOKEN
npx wrangler deploy
```

The API origin can be terminated with the supplied `deployment/Caddyfile`.
Set `VELTRIX_API_DOMAIN` to a dedicated hostname, point its DNS record at the
API host, install Caddy, and load the Caddyfile. Caddy exposes only `/api/*`
and proxies it to the Python server bound on loopback port 8765; every other
path returns 404. The bearer header is forwarded unchanged to the Python
authentication boundary. Verify Caddy has issued a valid certificate before
setting `VELTRIX_API_ORIGIN=https://<api-hostname>` in Worker secrets.

Bind the dashboard custom domain in Cloudflare after the Worker deployment is
verified. Enable HTTPS-only mode and strict TLS to the origin. Restrict origin
ingress to Cloudflare where the hosting provider supports it. Do not expose
the origin token in Pages build variables or frontend code.

## API proxy controls

The Worker rejects non-GET methods, unknown paths, non-HTTPS origins, and
upstream redirects. It forwards query strings only to the fixed configured
origin, disables caching, and does not expose arbitrary proxy destinations.
The Python origin continues to reject unauthenticated requests and all
write methods. The Turso serverless HTTP driver rejects SQLite
`PRAGMA query_only`, so remote dashboard connections use an application-level
SELECT-only connection wrapper; local SQLite connections retain OS-level
read-only URI mode plus `query_only`. The edge Worker does not replace Cloudflare Access.

## Remaining deployment gates

This repository change prepares the deployable edge artifact but does not
provision a Cloudflare account, create DNS records, deploy an origin host,
configure Access identity policies, or create encrypted secrets. Those require
account-level access and an actual HTTPS origin. No deployment is claimed
until an authenticated smoke test confirms dashboard assets, API health,
candle reads, and denial of write/unauthenticated requests.


## Zero-VPS pipeline scheduling option

For a deployment without an always-on VM, the repository includes
`.github/workflows/solana-scheduled-pipeline.yml`. GitHub Actions invokes
`python -m veltrix.solana_meme.pipeline_once` every five minutes. The runner
is ephemeral; all orchestration fences, idempotency keys, health records,
candles, and paper-ledger state remain in the shared Turso database.

Configure these repository secrets before enabling the schedule:

- `VELTRIX_SOLANA_DISCOVERY_DB`: production Turso `libsql://` URL. Do not
  use the disposable conformance database for production paper-ledger state.
- `TURSO_AUTH_TOKEN`: production database auth token.

The optional repository variable `SOL_PAPER_TRADING_ENABLED` defaults to
false. Set it to true only after the market-data and paper-ledger acceptance
gates are satisfied. `LIVE_TRADING` is pinned false and the runtime rejects
live execution. Workflow concurrency prevents overlapping scheduled jobs;
the database fencing lease remains the authoritative cross-run lock.

GitHub scheduled events are best-effort, not a real-time scheduler. A missed
or delayed invocation is not backfilled as a sequence of missed cycles. The
UTC-bucket idempotency key prevents duplicate execution in one five-minute
slot. Use the systemd supervisor on a supported always-on host when tighter
scheduling guarantees are required.

## Deployment boundary

The Worker and dashboard assets are deployable from this repository, but the
Python API origin is a separate service. It must be reachable over HTTPS,
authenticate the Worker-injected bearer token, and use the same production
Turso database as the pipeline. A local loopback API is not reachable from
Cloudflare. The repository intentionally does not substitute the disposable
conformance DB or claim an origin, DNS zone, Access policy, or production
secret exists.

After the API origin and secrets are provisioned, verify in this order:
1. Worker static assets return successfully over HTTPS.
2. Cloudflare Access blocks an unauthenticated browser session.
3. `GET /api/health` reports the production database connected and a
   non-stale worker heartbeat.
4. `GET /api/candles` returns provider-sourced OHLCV with fresh/stale status.
5. `GET /api/sol-paper-wallet` returns integer-lamport balances and P&L.
6. Browser/API write methods and unlisted routes return 405/404.
7. Restart the pipeline runner and verify ledger, candles, and job state persist.
