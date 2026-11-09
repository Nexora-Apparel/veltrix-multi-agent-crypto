# Solana market snapshot normalization

`SolanaMarketData` reuses the repository's existing read-only DexScreener
provider but constrains the active path to a validated Solana mint.

## Selection and provenance

- Fetches pair snapshots for one canonical mint on the Solana chain only.
- Rejects mismatched chain/token records, missing pair addresses, invalid prices,
  stale observations and future-dated observations.
- Selects the single pool with the greatest reported liquidity; ties use hourly
  volume and pair address for deterministic selection.
- Retains pair count, provider observation time, local retrieval time and pair
  creation time when supplied.

Liquidity and volume refer to the selected pool only. They are not summed across
pools, and they do not prove executable depth or sellability.

## Quality flags

Configured minimum liquidity, zero liquidity, zero hourly volume, missing pair
creation time and future pair creation time are exposed as quality flags. They
are not converted into a safety score. A market snapshot is not an authority,
holder-distribution, contract, creator-wallet or sellability audit.

## Next integration boundary

The next step is to persist timestamped snapshots beside launch candidates and
join them with independent Helius/GoPlus risk evidence. No snapshot can directly
trigger a paper entry or live transaction.
