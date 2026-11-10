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

Deploy with Wrangler from the repository root after installing Wrangler:

```sh
npx wrangler secret put VELTRIX_API_ORIGIN
npx wrangler secret put VELTRIX_DASHBOARD_TOKEN
npx wrangler deploy
```

Bind a custom domain in Cloudflare after the deployment is verified. Enable
HTTPS-only mode and strict TLS to the origin. Restrict origin ingress to
Cloudflare where the hosting provider supports it. Do not expose the origin
token in Pages build variables or frontend code.

## API proxy controls

The Worker rejects non-GET methods, unknown paths, non-HTTPS origins, and
upstream redirects. It forwards query strings only to the fixed configured
origin, disables caching, and does not expose arbitrary proxy destinations.
The Python origin continues to reject unauthenticated requests and all
write methods. The edge Worker does not replace Cloudflare Access.

## Remaining deployment gates

This repository change prepares the deployable edge artifact but does not
provision a Cloudflare account, create DNS records, deploy an origin host,
configure Access identity policies, or create encrypted secrets. Those require
account-level access and an actual HTTPS origin. No deployment is claimed
until an authenticated smoke test confirms dashboard assets, API health,
candle reads, and denial of write/unauthenticated requests.
