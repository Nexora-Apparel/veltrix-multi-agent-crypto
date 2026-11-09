# Phase 08 — Read-Only Dashboard and Analytics

## Scope

Phase 08 exposes a read-only view of the shared Phase 03–07 SQLite database.
It does not create positions, change decisions, manage exits, or submit orders.

## Dashboard

Start with:

```bash
python -m veltrix.solana_meme.dashboard
```

Default local address: `http://127.0.0.1:8765`.

The page refreshes every 15 seconds and displays:
- Virtual starting balance, cash, equity and portfolio exposure.
- Realized and unrealized P&L.
- Open positions, entry/mark prices, market value and mark freshness.
- Win rate and closed sell fills.
- Fees and simulated slippage costs.
- Daily realized P&L and cumulative realized P&L.
- Recent consensus-to-paper outcomes and the 24-hour consensus funnel.
- Latest market/risk timestamps and stale open-position count.

The dashboard uses the same SQLite database as the discovery, market,
risk-evidence, consensus and paper-trading workers. It opens that database with
SQLite `mode=ro` and `PRAGMA query_only=ON`. It does not initialize missing
tables or create a new paper account.

## Read-only API

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness response only |
| `GET /api/overview` | Account, P&L, exposure, costs, freshness and consensus funnel |
| `GET /api/positions` | Open positions with last observed mark and freshness |
| `GET /api/decisions?limit=50` | Recent consensus-to-paper execution outcomes |
| `GET /api/performance?days=30` | Daily realized P&L, cumulative P&L, wins/losses and costs |

All API responses are JSON. Write methods return HTTP 405. No endpoint can open,
close, or modify a paper position.

## Access control

The server binds to loopback by default. For remote binding:
1. Set `VELTRIX_DASHBOARD_HOST=0.0.0.0` only behind a trusted network or TLS reverse proxy.
2. Set `VELTRIX_DASHBOARD_TOKEN` to a random secret of at least 32 characters.
3. Enter that token in the dashboard access field; it is held in the page's
   memory and sent as a Bearer token for API requests.

The token is never embedded into the page or a URL. `/healthz` exposes only
liveness and read-only mode, not portfolio data.

## Freshness and reporting

Open positions are marked with the latest persisted Phase 04 market observation.
The dashboard labels each mark as fresh, stale, missing, or invalid. Stale marks
remain visible but are explicitly counted; they must not be interpreted as a
current executable quote.

Realized P&L comes from persisted SELL fills. Unrealized P&L uses the last
persisted mark less cost basis. Fees and slippage are shown separately as
simulation costs; they are already reflected in fill prices and realized P&L.
The daily chart is realized P&L by UTC date, not a backtested equity curve.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VELTRIX_DASHBOARD_HOST` | `127.0.0.1` | Bind address |
| `VELTRIX_DASHBOARD_PORT` | `8765` | HTTP port |
| `VELTRIX_DASHBOARD_TOKEN` | empty | Required for non-loopback binding; minimum 32 characters |
| `VELTRIX_DASHBOARD_MARKET_MAX_AGE_MS` | `120000` | Fresh-mark threshold |

## Phase boundary

Phase 08 is analytics and presentation only. Phase 09 live execution remains
disabled and is not part of this dashboard.
