# Solana Launch-Window Shared-Funder Evidence

The Helius launch index scans a mint's bounded launch window for Pump.fun buy
instructions, verifies buyer token transfers, resolves finalized total supply,
and requests first-funder ancestry for each buyer through the existing Helius
funding graph adapter. The analyzer counts token amounts only for distinct
wallets sharing a funder.

The worker requires explicit provider allowlists:
- `TRUSTED_BUNDLE_PROVIDERS=helius-solana-launch-index`
- `TRUSTED_FUNDING_PROVIDERS=helius-solana-funding-graph`
- `TRUSTED_LAUNCH_ORIGIN_PROVIDERS=helius-launch-origin`

It reuses `HELIUS_API_KEY` and `SOLANA_RPC_URL`. No new secret is required.
The Helius scan is bounded by configured page and scanned-transaction limits.
If launch origin, scan completion, finalized supply, or any buyer's funding
ancestry is unresolved, the bundled-supply percentage is withheld.

A shared first funder can be an exchange or other common service. This report
is a launch-cohort heuristic, not proof of common ownership, insider control,
or developer linkage. It is not registered as certified safety evidence and
cannot approve a paper or live entry. The provider supports Pump.fun launch
buys only; other Solana launchpads are not covered by this adapter.
