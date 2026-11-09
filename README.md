# VELTRIX Multi-Agent Crypto

Paper-only research and monitoring system for Solana, Base and BSC. It has no wallet signing, private-key loading, swaps or live order execution. `LIVE_TRADING=true` is rejected.

## Current components
- Scout / Whale / Sniper: initial signal logic. Sniper now requires a typed, passing SafetyReport from the independent validator; raw booleans in market payloads are rejected. Whale-flow and verified launch-safety feeds are not yet connected.
- Shield: enforces at least two distinct BUY agents, a maximum $50 position and a maximum of 10 open positions.
- Exit: the SQLite paper broker persists 2x / 3x partial-profit milestones and stop/trailing-stop state across restarts; this is paper-fill simulation, not exchange execution.
- DexScreener adapter: read-only token-pair snapshots for Solana, Base and BSC.
- Helius Parsed Events connector: read-only Solana wallet swap history for configured tracked wallets, with token-mint matching, bounded pagination, successful-swap filtering and an injected spot-price resolver. The USD notional is an estimate using the current spot price, not the historical execution price.
- Alchemy Transfers By Wallet connector: read-only Base and BNB Chain (BSC) wallet transfer history using the multichain Data API. It emits a BUY only when the same transaction includes an inbound target ERC-20 and an outbound operator-allowlisted USDC/USDT/DAI transfer from the tracked wallet; plain transfers, deposits and airdrops are rejected. Quote notional treats the stablecoin quantity as an approximate USD value (not an exact historical execution valuation); depegs, fees, router behavior and provider labeling can affect it. Pagination is bounded, provider partial errors fail closed, and `alchemy` remains untrusted until explicitly allowlisted.
- GoPlus token-security connector: read-only BSC token-risk endpoint and authenticated Solana beta endpoint. It can only mark LP liquidity locked when the returned LP-holder list is complete and every listed holder is explicitly locked. It deliberately leaves developer concentration and bundled-supply evidence unknown because the documented response does not establish those exact metrics. Base is not enabled because the current documented chain list does not establish support.
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

Add comma-separated contract addresses to `WATCHLIST_SOLANA`, `WATCHLIST_BASE`, and/or `WATCHLIST_BSC`. Configure `HELIUS_API_KEY` and `TRACKED_SMART_WALLETS` for the read-only Solana wallet-history connector. For Base and BNB Chain, configure `ALCHEMY_API_KEY`, `TRACKED_EVM_WALLETS`, and explicit comma-separated quote-token contract addresses in `ALCHEMY_QUOTE_TOKENS_BASE` and `ALCHEMY_QUOTE_TOKENS_BSC`. The quote-token lists are intentionally empty by default; add only verified USDC/USDT/DAI contract addresses and verify each contract address and asset identity on its chain before adding it. The Alchemy Transfers By Wallet endpoint is documented as beta and may change; its `bnb-mainnet` and `base-mainnet` network identifiers are used by the connector. Add `helius,alchemy` to `TRUSTED_WALLET_PROVIDERS` only after reviewing the wallet list and provider behavior. `GOPLUS_ACCESS_TOKEN` is required for the GoPlus Solana beta endpoint; BSC uses the documented chain-ID endpoint. Optionally set `DISCOVERY_ENABLED=true` to poll DexScreener's latest token profiles every cycle; configure `DISCOVERY_CHAINS` and `MAX_DISCOVERED_PER_CHAIN`. The profile endpoint is rate-limited to 60 requests/minute and does not represent all new pools. Keep `LIVE_TRADING=false`.

Start a 24-hour read-only monitoring session:
```bash
python -m veltrix.main --hours 24
```

Market observations are saved to the SQLite database at `DB_PATH`. No positions are opened automatically: the CLI is still observation-only. The new wallet connectors are library-level adapters and are not yet wired into a complete live signal-to-paper-entry pipeline. An actual trusted token-safety provider, comprehensive launch/pool discovery and end-to-end paper-session acceptance must pass before paper entries are enabled.

## Risk and data limitations
DexScreener pair snapshots do not prove liquidity is locked, developer holdings are below 5%, or bundled supply is zero. Missing safety fields must fail closed. The current implementation is not a financial recommendation and must not be used for live execution.
