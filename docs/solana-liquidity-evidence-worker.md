# Solana liquidity evidence worker

The optional `SolanaLiquidityEvidenceWorker` connects the finalized Raydium
V4/CPMM inventory reader to append-only evidence persistence.

## Processing

1. Discovers mints already present in the launch and market stores.
2. Claims a bounded batch through a SQLite lease queue.
3. Requests a finalized pool/locker inventory from the configured reader.
4. Runs `LiquidityEvidenceAnalyzer` against the returned typed inventory.
5. Persists the complete report, including failed or incomplete coverage.
6. Schedules the next refresh or retries provider failures with bounded
   exponential backoff.

The evidence store is append-only and deduplicates identical report payloads.
The worker and launch/market stores must share one persistent SQLite database.

## Runtime activation

The runtime builds this worker only when the existing Solana liquidity reader
factory can construct a reader. That requires two independent HTTPS RPC
endpoints, pinned Raydium AMM V4 and CPMM executable hashes, and the explicit
supported-AMM registry setting. No private key or signer is used.

## Certification boundary

The current reader inventories Raydium AMM V4 and CPMM only. CPMM positions
remain unverified until a supported locker is independently pinned. The
reader does not claim complete coverage of Raydium CLMM, Meteora DLMM, Orca,
PumpSwap or every active Solana AMM.

Consequently, a stored report can be incomplete or fail the liquidity analyzer.
The worker never changes provider capability status and never creates a
composite safety certificate. The default capability catalogue continues to
mark the current Solana liquidity adapter as partial. A report is evidence
for review, not trade permission.
