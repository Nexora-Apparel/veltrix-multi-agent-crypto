# VELTRIX Trading Terminal — Product and Data Contract

Status: implementation specification. This document does not claim the terminal is deployed or that OHLCV candle history is already available.

## Product direction

Build a mobile-first Solana trading terminal inspired by the supplied Grok Bot screenshot and the information density of Pump.fun, with VELTRIX's multi-agent intelligence and paper-trading controls. Reuse the existing dashboard, API authentication, paper ledger, portfolio calculations, market snapshots, and historical records. Do not replace or reset the ledger.

The supplied screenshot is the visual reference for the home/portfolio experience:
- dark, high-contrast interface with restrained neon magenta/green accents and subtle atmospheric background
- compact top status/identity area, bot/agent status and wallet allocation
- prominent virtual balance and daily/session PnL
- large equity chart with selectable ranges (1H, 4H, 12H, ALL)
- compact trade count, win-rate and best-close metrics
- recent closed positions with token identity, hold duration, timestamp and realized PnL
- persistent bottom navigation on mobile: Home, Trades, Agents, Wallet, Search

VELTRIX additions:
- a dedicated token terminal with real candlesticks, OHLC values, volume, timeframe selection, token metadata and liquidity/volume/market-cap context
- token discovery lists for new launches, trending, volume/liquidity leaders, watchlist and risk-filtered candidates
- multi-agent decision panel showing Scout, Whale, Sniper and Shield evidence, consensus and reasons
- paper order ticket with SOL-denominated sizing, estimated fee/slippage, exposure checks and explicit simulation state
- positions, order/fill history, trade journal, risk centre and performance analytics
- desktop layout expands into a multi-column terminal; mobile retains a bottom navigation bar and stacked cards

## Non-negotiable data integrity

1. Never fabricate prices, OHLCV candles, volume, liquidity, holder counts, wallet allocations, PnL, win rate or trade history.
2. Every live market value carries source, observed-at timestamp and freshness status.
3. If candle history or a live stream is unavailable, show an explicit unavailable/stale state; do not draw a synthetic chart and label it live.
4. Distinguish market price data from simulated execution prices and modeled fees/slippage.
5. The home equity chart is portfolio performance, not a token price chart. Token terminal charts are separate OHLCV series.
6. The chart should update from real market observations using a supported provider stream where available, with bounded polling fallback. Persist normalized candles with provider/source provenance and interval.
7. Show the last update time and connection/freshness state. Freeze or mark stale when the feed stops updating.
8. Paper mode is always visible. All Buy/Sell actions operate on the internal simulated SOL ledger only. No wallet signer, private-key import, swap submission, or transaction broadcast.

## Required screen map

### Home
- agent/account header, simulation badge, virtual SOL and USD balance, PnL and session return
- interactive equity curve with 1H / 4H / 12H / ALL ranges
- trade count, win rate, best close and drawdown
- recent closes list with token logo/symbol, holding duration, close time, realized USD and percentage PnL
- concise market/agent health and alert indicators

### Search / Discover
- searchable token directory, launch feed, trending and watchlist
- sort/filter by age, liquidity, volume, market cap/FDV and verified risk state
- candidate rows/cards show source and data age; unknown fields remain unknown
- opening a token loads its terminal view

### Token terminal
- token identity, mint copy action, pair/pool, source and data age
- actual candlestick OHLCV chart, volume histogram, 1m/5m/15m/1h/4h/1d intervals only where provider coverage exists
- current price, market cap/FDV, liquidity, volume, price changes and pair age
- buy/sell paper ticket with SOL sizing, balance, estimated fill, modeled costs and post-trade exposure
- AI consensus, risk evidence, holder/concentration and liquidity warnings where supported
- recent transactions/trades only if a trustworthy source supplies them

### Trades
- open positions, pending simulation intents, recent fills, closed trades and full trade journal
- entry/exit price, size, fees, slippage, holding time, realized/unrealized PnL and exit reason

### Agents
- Scout, Whale, Sniper and Shield state, latest evidence, timestamps, score/disposition and explanations
- distinguish an agent being idle from an agent being unavailable or failing

### Wallet
- virtual SOL balance, token holdings, total equity, available balance and exposure
- simulated account activity and ledger integrity
- never present a real wallet address or imply funds are on-chain

## Data/API work required

The current dashboard API is read-only and the existing market store contains market snapshots. Those snapshots can support current-price and portfolio marks, but are not by themselves a historical OHLCV candle feed. Before claiming a real moving candlestick chart, add and validate:
- normalized candle schema keyed by chain, mint/pair, interval, open timestamp and provider
- provider adapter(s) that return timestamped OHLCV and, where possible, live trade/price updates
- deduplication, out-of-order correction, gap detection and retention rules
- authenticated read endpoints for discovery, token detail, candles, agent activity, portfolio and trades
- bounded refresh/stream delivery with explicit freshness metadata
- authenticated paper-order endpoints only after idempotency, account checks, transaction-scoped fencing, risk limits and audit logging are verified

The existing read-only dashboard endpoints remain read-only. Do not add order mutation to them. Paper-order commands belong in a separately scoped authenticated API service and must use the existing broker/ledger with the pipeline's fencing guarantees.

## Visual and interaction requirements

- Mobile-first at 360px width; no horizontal page overflow.
- Bottom nav is persistent on mobile; desktop uses a left rail/top navigation.
- Chart supports crosshair, zoom/pan, OHLC tooltip, volume and range/interval controls.
- Lists have loading, empty, error, stale and populated states.
- Token rows navigate to a persistent selected-token route.
- All prices show sensible precision for micro-priced tokens without rounding to zero.
- Green/red PnL styling must be paired with explicit signs/text, not color alone.
- Respect reduced-motion preferences; chart updates should animate smoothly but not imply price movement between observations.
- Accessibility: keyboard-operable controls, readable contrast, labels and semantic status announcements.

## Delivery sequence

1. Keep the successful remote Turso conformance gate recorded; do not merge the integration PR solely because the workflow passed.
2. Finish Cloudflare Workers API/auth and Pages hosting foundation.
3. Implement normalized OHLCV persistence/provider adapter and read API; verify real provider coverage before UI claims.
4. Build the responsive terminal UI against documented API contracts, preserving the current analytics/ledger views.
5. Add fenced, idempotent paper-order commands and connect the Buy/Sell ticket.
6. Add domain/DNS and HTTPS, then scheduler/monitoring/recovery.
7. Run full end-to-end tests with simulated SOL, real market observations and zero wallet execution.
