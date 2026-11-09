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
  `unknown_<id>` and set `extension_parse_complete=false`; they are never
  silently discarded.
- Account-only extension types, duplicate TLV types, truncated headers or
  payloads, malformed terminators, non-zero mint padding and incorrect account
  type fail closed.
- The snapshot records active mint and freeze authorities as addresses or
  explicit `None`. An active authority is evidence, not an automatic safety
  approval.

## Scope boundary

This is an evidence collector, not a safety verdict. It does not yet decode
authority fields inside each Token-2022 extension, establish whether an
authority is controlled by the creator, or decide whether an extension is
acceptable. The existing risk evidence collector and Shield remain closed
until this data is integrated with separate provenance, policy, and
sellability checks.

The RPC URL is never written to evidence or exception messages. No private key,
wallet signer, transaction, or write method is present.
