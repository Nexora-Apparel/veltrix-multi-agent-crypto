# Stage 2 — Solana Candidate Validation

**Status:** implementation and acceptance in progress  
**Depends on:** Stage 1 architecture and runtime foundation  
**Scope:** validate Solana candidate identity and provenance before persistence or downstream use.

## Objective

Provide one fail-closed candidate contract shared by discovery adapters, stream ingestion and persistent storage. A candidate is a syntactically valid Solana mint observation with an explicit chain identity, non-empty source identity and positive integer observation timestamp.

A valid candidate is only a discovery lead. Validation does not certify that the mint account exists, is initialized as a token mint, has safe authorities, has liquidity, or is suitable for trading.

## Contract

- Canonical asset key: `(solana, mint)`; Solana base58 is case-sensitive.
- Mint must parse as a Solana `Pubkey` and round-trip to the exact canonical base58 string.
- Input must declare `chainId` or `chain`; if both are present, both must identify Solana.
- Supported mint aliases are `tokenAddress`, `token_address`, and `mint`. If multiple aliases are supplied, every one must be valid and identify the same mint.
- Source identity is required, whitespace-normalized, and retained as provenance. Source identity does **not** imply source trust.
- Observation time is a positive integer in milliseconds. Booleans, strings, zero and negative values are rejected.
- Candidate source provenance is normalized, de-duplicated, sorted and always includes the primary source.
- Duplicate mint observations merge provenance and retain the latest observation time and corresponding primary source.

## Enforcement points

1. Adapter normalization rejects malformed provider payloads.
2. The public candidate constructor enforces the same invariants; callers cannot bypass the normalizer through ordinary construction.
3. Deduplication reconstructs and validates each candidate before merging.
4. `LaunchStore.record_candidate` reconstructs the candidate again at the persistence boundary. Frozen dataclasses are not treated as proof of integrity because `object.__new__` and `object.__setattr__` can bypass or mutate them.
5. Primary stream and secondary feed ingestion record a bounded `candidate_rejected` audit event and continue processing valid candidates. Rejected candidate payloads and unbounded provider text are not copied into the audit record.

## Acceptance matrix

| Test area | Required result |
|---|---|
| Canonical Solana mint | Valid canonical base58 accepted; malformed/noncanonical rejected |
| Chain identity | Missing, wrong, malformed or conflicting chain metadata rejected |
| Mint aliases | Matching aliases accepted; conflicting aliases rejected |
| Source provenance | Missing/invalid source rejected; whitespace and duplicate provenance normalized |
| Observation time | Positive integer accepted; boolean/string/zero/negative rejected |
| Direct construction | Same invariants as normalization |
| Deduplication | Stable identity, latest timestamp, complete source union |
| Persistence boundary | Forged/mutated candidate rejected before any database write |
| Secondary ingestion | Invalid row audited and skipped; valid rows still persisted |
| Primary stream | Invalid row audited and skipped without falsely marking the feed offline |
| Historical data | Existing schema and records remain readable; no destructive migration |

## Explicit non-goals

- Provider allowlisting or proof of provider ownership.
- On-chain mint-account existence, Tokenkeg/Token-2022 owner, mint initialization, freeze/mint authority, or metadata verification.
- Pair/pool discovery completeness, market-data enrichment, liquidity, holder or developer analysis.
- Candidate ranking, trading decisions, paper entries, Telegram delivery, scheduling or execution.

Those concerns belong to later stages and must not be smuggled into candidate validation as unverified assumptions.

## Operational preservation

The existing SQLite database path, `solana_launch_candidates` table, event/outage tables, PumpPortal adapter, DexScreener adapter, and read-only ingestion behavior remain in place. This stage adds no database migration and no network capability. Existing historical candidate rows are not rewritten.

## Stage 2 exit criteria

- [x] Canonical mint, explicit chain, source and timestamp validation.
- [x] Conflicting address aliases rejected.
- [x] Direct constructor and deduplication enforce the contract.
- [x] Persistence revalidates rather than trusting object type.
- [x] Primary and secondary ingestion audit invalid candidates without dropping valid peers.
- [x] Full repository CI and targeted adversarial tests pass on the exact PR head (792 passed, 5 skipped; compileall passed).
- [ ] Post-merge CI passes on the integrated main commit.
- [x] Review confirms no discovery expansion, market/risk logic, database migration, credentials or release activation was introduced.

Stage 2 remains open until post-merge CI is independently verified.
