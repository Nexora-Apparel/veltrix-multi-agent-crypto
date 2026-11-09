# Solana claim-separated risk evidence

`SolanaRiskEvidenceCollector` reuses the repository's GoPlus Solana security
connector and native finalized holder inventory provider. It can also accept
the optional `SolanaMintAuthorityProvider` to collect finalized mint-account
state from Solana JSON-RPC.

## Evidence collected

- GoPlus observation identity, evidence digest, timestamp and LP-lock status.
- Holder snapshot identity, finalized block reference and provider provenance.
- Holder count and total supply.
- Top-1, top-5 and top-10 concentration percentages only when the upstream
  inventory is explicitly complete and the sum of owner balances exactly
  reconciles to total supply.
- Optional finalized mint account: token program, mint authority, freeze
  authority, supply, decimals, finalized slot, evidence digest and Token-2022
  extension type inventory.
- A reconciliation flag requiring finalized mint supply and decimals to match
  the holder inventory before Shield can approve a paper entry.

A failed, stale, mismatched or incomplete source remains unknown. Percentages are
never calculated from a partial holder list. Active mint/freeze authorities are
recorded explicitly; the Shield blocks either authority while the strict policy
is in force.

## Token-2022 review boundary

The mint provider validates the base Mint layout and extension TLV structure.
It preserves unknown extension IDs and refuses to treat them as reviewed.
Known extensions are listed, but their individual payload semantics and
authority fields are not yet fully decoded. Therefore any Token-2022 mint with
extensions retains `token_2022_extension_review` as a missing claim and is
blocked by Shield.

## Explicitly not certified

This report does not establish developer-wallet linkage, bundled supply,
creator provenance or whether the token can be sold under current execution
conditions. Those claims remain in `missing_claims` until independently
sourced and validated.

The report's `risk_state` is an evidence-readiness label, not a buy/sell
recommendation or a safety certification. No single provider response or
holder snapshot can authorise an entry.
