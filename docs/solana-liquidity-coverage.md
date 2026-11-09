# Solana liquidity-lock coverage registry

## Official UNCX program inventory

UNCX's official developer page lists six locker program / LP-storage-account
pairs: Raydium AMM V4, Raydium AMM V4 Smart Lockers, Raydium CP Swap, Raydium
CLMM, Meteora DLMM and PumpSwap. The candidate registry in
`veltrix/liquidity_evidence.py` now records all six program IDs and their
documented storage accounts.

Source: [UNCX Solana locker contract registry](https://docs.uncx.network/guides/for-developers/liquidity-lockers/solana-lockers/contracts/).

## Evidence level by program

| Program family | Official program ID/storage account listed | Public source + executable hash pinned in this project | Current adapter status |
|---|---:|---:|---|
| Raydium AMM V4 locker | Yes | Yes | Existing verified Anchor account/PDA/custody reader |
| Raydium AMM V4 smart locker | Yes | No | Candidate only |
| Raydium CP Swap locker | Yes | No | Candidate only; public repository currently exposes only a README, not the implementation/IDL |
| Raydium CLMM locker | Yes | No | Candidate only |
| Meteora DLMM locker | Yes | No | Candidate only |
| PumpSwap locker | Yes | No | Candidate only |

The public UNCX Raydium AMM V4 repository includes Rust source, an Anchor IDL,
the deployed program binary and a verifiable executable SHA-256. The CPMM
repository currently exposes only a README in its public tree. The other
locker program IDs are documented, but this project has not independently
pinned their deployed executable hashes or account layouts.

Source: [UNCX Raydium AMM V4 locker source](https://github.com/uncx-network/raydium-amm-lp-locker)
and [UNCX Raydium CP-Swap locker repository](https://github.com/uncx-network/raydium-cp-swap-lp-locker).

## Why this is not yet certifying

The existing read-only pool reader scans Raydium AMM V4 and CPMM pool state.
It verifies the published UNCX V4 locker program and its custody account. It
does not yet decode all five other UNCX locker program families. A CPMM pool
may be discovered while its associated UNCX locker remains unsupported.

A token can have liquidity split across multiple AMMs. A positive lock result
for one pool cannot establish that all active liquidity is locked. Therefore:

- The complete active-AMM registry must be independently enumerated.
- Every supported locker executable must be pinned to source and a verified
  deployed hash.
- Every account layout, PDA derivation, custody authority, LP mint, amount,
  unlock schedule and finalized account owner must be validated.
- Every discovered pool must reconcile against the full pool inventory.
- Any unsupported venue, unknown locker, missing pool page or incomplete
  indexer watermark must keep the composite liquidity claim false.

The capability catalogue now marks
`solana-raydium-v4-cpmm-finalized-pool-inventory` as **partial**, not
certifying. The default composite validator will not accept it as proof of
Solana liquidity lock. This is intentional; the registry additions are
discovery metadata, not a safety approval.

## Next implementation sequence

1. Add independent source/IDL and executable-hash verification for each
   remaining UNCX locker program.
2. Implement separate account decoders for each locker family; do not reuse the
   AMM V4 TokenLock layout across programs.
3. Expand pool discovery to CLMM, Meteora DLMM and PumpSwap and prove complete
   coverage across all active pools.
4. Add negative fixtures for unknown program versions, upgrade changes,
   unsupported pools, incomplete scans, unrecognized lock records and custody
   mismatches.
5. Only after a reproducible full-coverage acceptance suite, reassess the
   capability catalogue. Do not mark it complete based only on documentation
   or a configured boolean.


## Runtime evidence retention

The optional `SolanaLiquidityEvidenceWorker` runs from the read-only pipeline
when both independent HTTPS RPC endpoints, pinned Raydium AMM V4 and CPMM
executable hashes, and the explicitly configured reader scope are present.
It discovers only market-enriched candidates and processes a bounded batch.

For each successful provider response, the worker persists both:
- The analyzer's pass/block report, including per-pool locked percentages and
  exact reasons for rejection.
- The complete typed source inventory: pool records, LP supplies, locker
  positions, deployment identities, custody references and finalized block
  references.

The store is append-only and deduplicates identical report/inventory payloads.
Provider failures are retried with bounded exponential backoff and do not
create fabricated reports. An incomplete inventory can be stored as a blocked
observation for diagnosis, but it cannot become a positive safety claim.

Persisting raw inventory improves auditability; it does not expand the reader's
supported AMM/locker coverage and does not change the partial capability status.
The default composite validator continues to reject this provider as a
certifying Solana liquidity source.
