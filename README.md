# VELTRIX Multi-Agent Crypto

Paper-only research and monitoring system for Solana, Base and BSC. It has no wallet signing, private-key loading, swaps or live order execution. `LIVE_TRADING=true` is rejected.

## Current components
- Scout / Whale / Sniper: initial signal logic. Sniper now requires a typed, passing SafetyReport from the independent validator; raw booleans in market payloads are rejected. Whale-flow and verified launch-safety feeds are not yet connected.
- Shield: enforces at least two distinct BUY agents, a maximum $50 position and a maximum of 10 open positions.
- Exit: initial 2x / 3x / stop decision rules; stateful partial-fill orchestration remains to be completed.
- DexScreener adapter: read-only token-pair snapshots for Solana, Base and BSC.
- Optional candidate discovery: consumes DexScreener's latest token-profile feed, filters to configured chains, deduplicates and caps candidate counts. This is profile/promotional discovery, not an exhaustive new-pair feed or organic trend ranking.
- WhaleFlowAggregator: clusters buys only from operator-allowlisted wallets, within a freshness window and minimum-notional threshold. A real transaction/indexer feed and vetted wallet list are still required.
- TokenSafetyValidator: fail-closed evidence contract requiring a trusted provider, matching token address, fresh evidence, confirmed liquidity lock, developer holdings below 5%, and zero bundled supply. No provider is trusted by default.
- VerifiedIntelligenceAdapters: defines `SmartWalletProvider` and `TokenSafetyProvider` interfaces plus fail-closed collection adapters. It normalizes smart-wallet BUY observations only from operator-allowlisted providers; rejects unsupported chains, stale/future timestamps, malformed notionals, missing transaction IDs, duplicate transactions and provider-identity mismatches. Safety observations pass through the trusted-provider and freshness validator. Whale and Sniper decisions expose agent-specific rejection reasons. This is a provenance/freshness gate, not proof that a provider's claims are true; provider due diligence and actual integrations remain required.
- SQLite paper ledger: persists market observations, paper fills, positions, one-time 2x/3x exit milestones and peak prices. Fresh observed prices trigger the initial 6% stop, one-time partial profit-taking at 2x and 3x, then a 6% trailing stop on the remainder. Snapshot supports unrealized PnL/equity; session report summarizes realized PnL, closed-position win rate, best/worst trade and open positions.
- GitHub Actions: Python 3.11 compile and pytest checks.

## Setup
Python 3.11+
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add comma-separated contract addresses to `WATCHLIST_SOLANA`, `WATCHLIST_BASE`, and/or `WATCHLIST_BSC`. Optionally set `DISCOVERY_ENABLED=true` to poll DexScreener's latest token profiles every cycle; configure `DISCOVERY_CHAINS` and `MAX_DISCOVERED_PER_CHAIN`. The profile endpoint is rate-limited to 60 requests/minute and does not represent all new pools. Keep `LIVE_TRADING=false`.

Start a 24-hour read-only monitoring session:
```bash
python -m veltrix.main --hours 24
```

Market observations are saved to the SQLite database at `DB_PATH`. No positions are opened automatically: a real smart-wallet transaction/indexer feed, an actual trusted token-safety provider, comprehensive launch/pool discovery, stateful exit execution and end-to-end paper-session reporting must pass acceptance tests first.

## Risk and data limitations
DexScreener pair snapshots do not prove liquidity is locked, developer holdings are below 5%, or bundled supply is zero. Missing safety fields must fail closed. The current implementation is not a financial recommendation and must not be used for live execution.
