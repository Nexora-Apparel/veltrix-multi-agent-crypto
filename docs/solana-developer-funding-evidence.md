# Solana Developer Funding Evidence Worker

This worker joins launch candidates to market-enriched mints, obtains a
cryptographically verified Ed25519 developer-root attestation, retrieves a
complete holder inventory, traces each holder's earliest observed native-SOL
funder through Helius Parsed Events Scan, and applies the existing
DeveloperConcentrationAnalyzer.

Provider identities must be explicitly allowlisted in configuration:
- `TRUSTED_HOLDER_INVENTORY_PROVIDERS=solana-rpc-holder-inventory`
- `TRUSTED_DEVELOPER_FUNDING_PROVIDERS=helius-solana-funding-graph`
- `TRUSTED_DEVELOPER_ROOT_PROVIDERS=signed-developer-roots`

The signed root endpoint and public verification key must also be configured.
No new private key or API secret is introduced. Existing `HELIUS_API_KEY` and
`SOLANA_RPC_URL` are reused.

Reports are append-only and include the root, holder, funding provider IDs,
coverage status, linked wallets, supply percentage and rejection reasons.
A common funder does not imply developer linkage: only ancestry that reaches
an independently attested root is counted.

**Certification boundary:** the worker always passes `certified_provider_id=None`.
Even a complete graph report therefore cannot be converted into a trusted
SafetyEvidence claim. Helius native-SOL ancestry is an investigative signal,
not proof of common ownership, and does not establish coordinated token buys
or bundled launch supply. The worker has no agent, broker, wallet, signer,
swap, or transaction-submission dependency.
