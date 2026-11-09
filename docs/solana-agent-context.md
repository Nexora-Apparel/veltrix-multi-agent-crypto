# Solana agent context providers

The paper runner consumes one typed `SolanaAgentContext`. Two read-only
providers can supply it; neither provider decides whether to BUY.

## Market-only context

`SolanaMarketAgentContextProvider` derives Scout inputs from persisted
DexScreener observations:

- Uses the latest snapshot's 5-minute price change.
- Compares hourly volume only across the same selected pair/pool.
- Requires five distinct prior provider observations.
- The latest hourly volume must be at least 2x the median of the five prior
  observations.
- Baseline observations must be earlier than the latest snapshot and no older
  than 15 minutes. The latest snapshot must be no older than 120 seconds.
- Pool changes, missing history, stale history or invalid rows result in
  `volume_spike=false`; unrelated pools are never mixed.

The factory uses this provider by default. It can operate from persisted market
data without adding API credentials or wallet permissions.

## Optional Helius wallet-flow context

`SolanaAgentContextProvider` composes the market-only context with the
existing `CrossChainIntelligenceCoordinator` and Helius wallet adapter.

- Wallets are eligible only when explicitly listed in
  `TRACKED_SMART_WALLETS`; Helius must be included in
  `TRUSTED_WALLET_PROVIDERS`.
- The factory selects this combined provider only when `HELIUS_API_KEY`, at
  least two tracked wallet addresses and the Helius trust entry are configured.
- Provider failures or rejected wallet observations fail closed. The Whale
  agent receives only normalized wallet addresses from qualifying clusters.
- Helius wallet notional uses the existing spot-price resolver. It is context
  for wallet-flow consensus, not historical execution-price truth or position
  sizing.

## Provenance and activation

Both providers return a typed context with mint, provider ID, evidence digest,
observation time, volume-spike state, momentum and wallet identities. The
paper decision runner requires the selected provider ID in its explicit
trusted-agent-context allowlist.

The context providers have no signing or trading interface. They are agent
inputs, not token-safety certificates. The runner remains disabled until a
production Solana composite-safety certificate can pass the default capability
validator.
