# Solana Mint Authority and Token-2022 Evidence

`SolanaMintAuthorityProvider` performs a read-only `getAccountInfo` request at
the `finalized` commitment. It validates the owning program, account
initialization, the 82-byte SPL Mint base layout, both COption authority fields,
supply, decimals, slot provenance, and account-data digest.

## Token programs and extension parsing

- Classic SPL Token mints must be owned by the canonical Token Program and
  contain exactly 82 bytes.
- Token-2022 mints use the same first 82 bytes. If extensions exist, the
  provider validates the mint padding, Mint account-type discriminator, and
  TLV structure beginning at byte 166.
- Known mint extension IDs are named. Unknown IDs are preserved as
  `unknown_<id>` and set `extension_tlv_structure_valid=false`; they are never
  silently discarded.
- Account-only extension types, duplicate TLV types, truncated headers or
  payloads, malformed terminators, non-zero mint padding and incorrect account
  type fail closed.
- The provider validates TLV boundaries but does not yet decode all extension
  payload semantics or their individual authorities.

## Risk collector and Shield integration

The optional `SolanaRiskEvidenceCollector` now accepts this provider and records
mint/freeze authority state, program ID, finalized slot, evidence digest and
extension inventory. When the mint account is verified and no Token-2022
extensions are present, the corresponding authority and extension-presence
claims can be resolved. Active mint or freeze authority is explicitly retained
and vetoed by Shield. Any Token-2022 mint with extensions remains blocked until
the extension-specific policy and authority fields are decoded and reviewed.

The runtime constructs this collector only when the existing
`GOPLUS_ACCESS_TOKEN` and `SOLANA_RPC_URL` settings are both available. It adds
no credential requirement.

This is evidence collection, not a safety verdict. No private key, wallet
signer, transaction, or write method is present.
