# Phase 08 — Read-Only Dashboard and Analytics

## Scope

Phase 08 exposes a read-only dashboard over the shared Phase 03–07 SQLite
database. It does not create positions, change decisions, manage exits, or
submit orders. Live execution remains disabled.

## Start

Run the canonical dashboard server:

```bash
python -m veltrix.dashboard_server
```

Default local address: `http://127.0.0.1:8765`.

The interface refreshes every 15 seconds and displays:
- Virtual starting balance, cash, equity, total return and portfolio exposure.
- Realized and unrealized P&L.
- Open positions, entry/mark prices, market value and mark freshness.
- Sell-fill win rate and recent buy/sell fills.
- Fees and simulated slippage costs, separated by entry and exit.
- Realized equity curve, based on starting balance plus settled sell-fill P&L.
- Recent consensus decisions and paper-execution outcomes.
- Latest market snapshots, liquidity, volume and data-quality flags.
- Pipeline record counts and latest market/risk/consensus/paper timestamps.

The dashboard uses the same persistent SQLite database as the discovery,
market, risk-evidence, consensus and paper-trading workers. Connections use
SQLite `mode=ro` and `PRAGMA query_only=ON`. The dashboard does not create
tables, initialize a paper account, or write to the ledger.

## Read-only API

Every endpoint is GET-only and returns JSON, except the HTML and static assets.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Database connectivity, read-only state and missing tables |
| `GET /api/overview` | Account, P&L, exposure, costs, pipeline counts and freshness |
| `GET /api/positions` | Open positions with last persisted mark and freshness |
| `GET /api/trades` | Recent buy/sell fills and modeled costs |
| `GET /api/decisions` | Recent consensus-to-paper outcomes |
| `GET /api/consensus` | Multi-agent consensus history |
| `GET /api/markets` | Latest Solana market snapshots |
| `GET /api/equity-curve` | Realized equity curve from settled sell fills |
| `GET /api/costs` | Fee and slippage totals, including entry/exit breakdown |
| `GET /` | Dashboard interface |
| `GET /assets/dashboard.css` | Dashboard stylesheet |
| `GET /assets/dashboard.js` | Dashboard client |

POST, PUT and DELETE requests return HTTP 405. No dashboard route can open,
close, or modify a paper position.

## Access control

The server binds to loopback by default. Loopback access is unauthenticated.
For remote binding:
1. Set `VELTRIX_DASHBOARD_HOST=0.0.0.0` only behind a trusted network or TLS
   reverse proxy.
2. Set `VELTRIX_DASHBOARD_TOKEN` to a randomly generated secret of at least
   32 characters.
3. Open the dashboard over HTTPS and enter the token in the remote access
   panel. The browser stores it in tab-scoped session storage and sends it as
   an `Authorization: Bearer` header.

Remote binding fails closed if the token is missing or shorter than 32
characters. The token is not embedded in the HTML or a URL. Authentication is
required for the dashboard page, static assets and API routes. Use HTTPS for
any remote deployment; the built-in HTTP server does not terminate TLS.

## Freshness and reporting

Open positions are marked from the latest persisted Phase 04 market observation.
Stale or missing marks remain visible but are not represented as current
executable quotes. The overview reports the count of stale or unmarked positions.

Realized P&L comes from persisted SELL fills. Unrealized P&L uses the last
persisted mark less cost basis. Fees and slippage are shown separately as
simulation costs; they are modeled assumptions and are already reflected in
fill prices and realized P&L. The realized equity curve is not an independently
audited broker equity curve and does not include unrealized position value.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VELTRIX_DASHBOARD_HOST` | `127.0.0.1` | Bind address |
| `VELTRIX_DASHBOARD_PORT` | `8765` | HTTP port; use `0` for an ephemeral test port |
| `VELTRIX_DASHBOARD_TOKEN` | empty | Required for remote binding; minimum 32 characters |
| `VELTRIX_DASHBOARD_MARKET_MAX_AGE_MS` | `120000` | Fresh-mark threshold |

## Phase boundary

Phase 08 is analytics and presentation only. Phase 09 live execution remains
disabled and is not part of this dashboard.
