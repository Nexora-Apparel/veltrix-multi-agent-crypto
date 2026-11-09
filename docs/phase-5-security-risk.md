# Phase 05 — Solana Security and Risk Evidence

## Scope

Phase 05 collects and classifies security evidence for candidates that already
have Phase 04 market snapshots. It is a deterministic evidence and risk-flag
engine, not an AI consensus engine and not a trading authorization service.

The report intentionally has no `is_safe` field and never emits a SAFE verdict.

## Evidence sources

### Finalized mint-account inspection

`SolanaMintAuthorityProvider` reads the mint account at finalized commitment
and records:
- Token program identity (classic SPL Token or Token-2022).
- Mint authority and freeze authority.
- Raw supply and decimals.
- Token-2022 extension TLV inventory.
- Finalized slot, local observation time and account-data SHA-256.

Malformed mint layouts, unsupported token programs, invalid TLV structures,
stale/future timestamps and invalid authority keys fail closed.

Unknown Token-2022 extension IDs are preserved as unknown evidence. A
structurally valid TLV stream is not rejected merely because the local
extension catalogue does not recognize an ID; the extension review remains
incomplete and a manual-review risk flag is emitted.

### Complete holder inventory

`SolanaHolderInventoryProvider` reads both supported SPL token programs.
Concentration percentages are emitted only when:
- The provider declares complete coverage.
- Holder owner rows are unique and valid.
- The sum of observed owner balances exactly equals the provider's total supply.
- The mint-account supply and decimals reconcile with the holder snapshot.

Otherwise, top-holder percentages remain null. The engine does not infer
concentration from a truncated top-20 account list.

### Supplemental GoPlus observations

GoPlus may contribute reported liquidity-lock, developer-hold, bundled-supply
and creator-hold percentages. These are explicitly labeled supplemental
observations. They do not certify:
- Complete liquidity-pool inventory or all-pool lock coverage.
- Developer-wallet identity or funding provenance.
- Complete bundled-supply analysis.
- Creator-wallet provenance.
- Executable sellability.

The corresponding claims remain in `missing_claims` even when GoPlus reports a
positive liquidity-lock result.

## Deterministic risk flags

Flags include active mint/freeze authorities, reviewed Token-2022 extensions,
unknown extensions, high owner concentration, incomplete holder coverage,
mint/holder supply mismatches, and supplemental GoPlus observations.

Concentration flags are triage indicators. Pool vaults, burn addresses,
custodians and exchange wallets still require classification before interpreting
a large balance as developer control.

No single flag, provider, or market snapshot is sufficient to certify safety.

## Evidence states

- `REQUIRES_MORE_EVIDENCE`: required claims are missing or provider errors exist.
- `EVIDENCE_COLLECTED_REVIEW_REQUIRED`: all configured evidence was collected,
  but human/independent review is still required.

Neither state means that the token is safe to buy.

## Persistence and worker

Reports are append-only in `solana_risk_evidence_reports`, deduplicated by
canonical report hash. The scheduled worker only processes candidates that
already have Phase 04 market snapshots, uses SQLite leases, and retries
transient collection failures.

Start the worker with:

```bash
python -m veltrix.solana_meme.risk_runtime
```

Configuration:
- `VELTRIX_SOLANA_DISCOVERY_DB`: same persistent SQLite database used by
  Phases 03 and 04.
- `SOLANA_RPC_URL` or `HELIUS_API_KEY`: required for finalized mint and holder
  evidence.
- `GOPLUS_ACCESS_TOKEN`: optional supplemental GoPlus evidence.
- `VELTRIX_RISK_POLL_SECONDS`: polling interval (default 30).
- `VELTRIX_RISK_REFRESH_INTERVAL_MS`: refresh cadence (default 300000).
- `VELTRIX_RISK_BATCH_SIZE`: candidates per cycle (default 5, maximum 25).

## Explicit exclusions

Phase 05 does not implement AI consensus, autonomous buy/sell decisions,
paper-trading positions, dashboard presentation, wallet signing or transaction
execution. Those remain later roadmap stages.
