# Solana agent context provider

`SolanaAgentContextProvider` supplies typed, timestamped inputs to the
existing Scout, Whale and Sniper agents. It does not generate BUY decisions.

## Market context

- Uses the latest persisted Solana market snapshot for 5-minute momentum.
- Computes a volume-spike flag only when five prior snapshots have valid,
  positive hourly-volume observations.
- The latest hourly volume must be at least 2x the median of those five prior
  observations. Insufficient history means `volume_spike=false`; no baseline
  is fabricated.
- Market evidence must be within the configured 120-second freshness window.

## Wallet-flow context

- Uses the existing `CrossChainIntelligenceCoordinator` and Helius wallet
  observation adapter.
- Wallets are eligible only when explicitly listed in
  `TRACKED_SMART_WALLETS`; the Helius provider identity must be included in
  `TRUSTED_WALLET_PROVIDERS`.
- The factory returns no provider unless `HELIUS_API_KEY`, at least two
  tracked wallet addresses and the Helius trust entry are configured.
- Provider failures or rejected wallet observations fail closed. The Whale
  agent receives only normalized wallet addresses from qualifying clusters.
- Helius wallet notional uses the existing spot-price resolver. It is context
  for wallet-flow consensus, not historical execution-price truth and not
  position sizing.

## Provenance

The provider returns `SolanaAgentContext` with mint, provider ID, evidence
digest, observation time, volume-spike state, momentum and distinct wallet
identities. The paper decision runner separately requires the provider ID in
its explicit trusted-agent-context allowlist.

## Safety boundary

This component has no signing or trading interface. Market momentum and
wallet-flow observations are agent inputs, not token-safety certificates.
The runner remains disabled until a production Solana composite-safety
certificate can pass the default capability validator.
