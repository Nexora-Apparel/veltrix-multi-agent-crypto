# Solana claim-separated risk evidence

`SolanaRiskEvidenceCollector` reuses the repository's existing GoPlus Solana
security connector and native finalized holder inventory provider.

## Evidence collected

- GoPlus observation identity, evidence digest, timestamp and LP-lock status.
- Holder snapshot identity, finalized block reference and provider provenance.
- Holder count and total supply.
- Top-1, top-5 and top-10 concentration percentages only when the upstream
  inventory is explicitly complete and the sum of owner balances exactly
  reconciles to total supply.

A failed, stale, mismatched or incomplete source remains unknown. Percentages are
never calculated from a partial holder list.

## Explicitly not certified

This report does not establish developer-wallet linkage, bundled supply,
mint/freeze authority state, Token-2022 extension safety, creator provenance or
whether the token can be sold under current execution conditions. Those claims
remain in `missing_claims` until independently sourced and validated.

The report's `risk_state` is an evidence-readiness label, not a buy/sell
recommendation or a safety certification. No single GoPlus response or holder
snapshot can authorise an entry.
