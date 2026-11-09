# VELTRIX Multi-Agent Crypto

Paper-only research and monitoring system for Solana, Base and BSC. It has no wallet signing, private-key loading, swaps or live order execution. `LIVE_TRADING=true` is rejected.

## Current components
- Scout / Whale / Sniper: initial signal logic. Sniper now requires a typed, passing SafetyReport from the independent validator; raw booleans in market payloads are rejected. Whale-flow and verified launch-safety feeds are not yet connected.
- Shield: enforces at least two distinct BUY agents, a maximum $50 position and a maximum of 10 open positions.
- Exit: initial 2x / 3x / stop decision rules; stateful partial-fill orchestration remains to be completed.
- DexScreener adapter: read-only token-pair snapshots for Solana, Base and BSC.
- TokenSafetyValidator: fail-closed evidence contract requiring a trusted provider, matching token address, fresh evidence, confirmed liquidity lock, developer holdings below 5%, and zero bundled supply. No provider is trusted by default.
- SQLite paper ledger: persists market observations, paper fills, positions and realized PnL.
- GitHub Actions: Python 3.11 compile and pytest checks.

## Setup
Python 3.11+
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add comma-separated contract addresses to `WATCHLIST_SOLANA`, `WATCHLIST_BASE`, and/or `WATCHLIST_BSC`. These are explicit token watchlists, not automatic new-launch discovery. Keep `LIVE_TRADING=false`.

Start a 24-hour read-only monitoring session:
```bash
python -m veltrix.main --hours 24
```

Market observations are saved to the SQLite database at `DB_PATH`. No positions are opened automatically: independent smart-wallet activity, an actual trusted token-safety provider, dynamic discovery, stateful exit execution and end-to-end paper-session reporting must pass acceptance tests first.

## Risk and data limitations
DexScreener pair snapshots do not prove liquidity is locked, developer holdings are below 5%, or bundled supply is zero. Missing safety fields must fail closed. The current implementation is not a financial recommendation and must not be used for live execution.
