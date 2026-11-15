# Stage 5 production evidence acceptance runbook

Stage 5 is not complete until the six chain-specific read-only acceptance runs (liquidity lock + developer evidence on Solana, Base and BSC) pass against production finalized state and the resulting provider identities are independently reviewed. Unit tests and mocked fixtures do not satisfy this gate.

## 1. Liquidity-lock provider acceptance

Run the GitHub Actions workflow **Liquidity live read-only acceptance** once per chain. The workflow is manual and does not submit transactions.

### Solana secrets

- `SOLANA_LIQUIDITY_POOL_RPC_URL` — production HTTPS RPC endpoint for pool inventory.
- `SOLANA_LIQUIDITY_LOCKER_RPC_URL` — a separate, independently operated production HTTPS RPC endpoint for locker state.
- `SOLANA_RAYDIUM_AMM_V4_EXECUTABLE_HASH` — SHA-256 of the finalized deployed Raydium AMM V4 ProgramData ELF.
- `SOLANA_RAYDIUM_CPMM_EXECUTABLE_HASH` — SHA-256 of the finalized deployed Raydium CPMM ProgramData ELF.
- `SOLANA_RAYDIUM_LOCKING_EXECUTABLE_HASH` — SHA-256 of the finalized deployed Raydium Burn & Earn / Liquidity Locking ProgramData ELF.
- `SOLANA_LIQUIDITY_AMM_REGISTRY_COMPLETE` — set to the string `true` only after the supported Raydium AMM V4 + CPMM scope has been independently reviewed as complete.

Optional repository variable: `SOLANA_LIQUIDITY_ACCEPTANCE_TOKEN`.

### Base secrets

- `BASE_LIQUIDITY_POOL_RPC_URL` — production HTTPS RPC endpoint for pool inventory.
- `BASE_LIQUIDITY_LOCKER_RPC_URL` — a separate, independently operated production HTTPS RPC endpoint for locker state.
- `BASE_LIQUIDITY_AMM_FACTORIES_JSON` — complete, reviewed factory registry JSON.
- `BASE_UNCX_LOCKERS_JSON` — complete, reviewed UNCX locker deployment/ABI/hash registry JSON.
- `BASE_LIQUIDITY_AMM_REGISTRY_COMPLETE` — set to `true` only after the supported factory scope has been independently reviewed as complete.

Optional repository variable: `BASE_LIQUIDITY_ACCEPTANCE_TOKEN`.

### BSC secrets

- `BSC_LIQUIDITY_POOL_RPC_URL` — production HTTPS RPC endpoint for pool inventory.
- `BSC_LIQUIDITY_LOCKER_RPC_URL` — a separate, independently operated production HTTPS RPC endpoint for locker state.
- `BSC_LIQUIDITY_AMM_FACTORIES_JSON` — complete, reviewed factory registry JSON.
- `BSC_UNCX_LOCKERS_JSON` — complete, reviewed UNCX locker deployment/ABI/hash registry JSON.
- `BSC_LIQUIDITY_AMM_REGISTRY_COMPLETE` — set to `true` only after the supported factory scope has been independently reviewed as complete.

Optional repository variable: `BSC_LIQUIDITY_ACCEPTANCE_TOKEN`.

Do not derive or guess program hashes. Pin the deployed executable bytes read from finalized ProgramData. A documented program address or an IDL alone is not a runtime hash. The Solana reader now verifies both the UNCX AMM V4 TokenLock profile and Raydium Burn & Earn CPMM lock-state profile. CLMM, Meteora DLMM, Orca and PumpSwap remain outside the declared pool scope.

## 2. Developer-holdings and funding-graph acceptance

Run **Developer evidence structural acceptance** once per chain, selecting a production token whose holder inventory and full funding history can be enumerated. This workflow is read-only.

### Required secrets

- Solana: `SOLANA_RPC_URL`, `HELIUS_API_KEY` (Helius Developer plan or higher).
- Base: `BASE_RPC_URL`, `ALCHEMY_API_KEY`.
- BSC: `BSC_RPC_URL`, `ETHERSCAN_API_KEY`.
- All chains: `DEVELOPER_ROOT_ATTESTATION_URL`, `DEVELOPER_ROOT_ATTESTATION_PUBLIC_KEY`, `DEVELOPER_ROOT_ATTESTATION_PROVIDER_ID`.

### Required repository variables

- `TRUSTED_DEVELOPER_ROOT_PROVIDERS`
- `TRUSTED_HOLDER_INVENTORY_PROVIDERS`
- `TRUSTED_DEVELOPER_FUNDING_PROVIDERS`
- `TRUSTED_DEVELOPER_PROVIDERS` — must explicitly include `developer-graph-provider`.
- `TRUSTED_LAUNCH_ORIGIN_PROVIDERS` — required for Base and BSC deployment-block provenance.

The workflow reports structural completeness separately from provider trust and capability certification. It exits unsuccessfully if structural coverage or the exact provider trust allowlists are incomplete. A structural pass is not capability certification: independent review of root-identity methodology, source coverage, provider terms/limits and production evidence is still required.

## 3. Release decision

Stage 5 can be marked complete only when all of the following are true:

1. Liquidity live acceptance passes on Solana, Base and BSC.
2. Developer evidence acceptance passes on Solana, Base and BSC.
3. Every production RPC is independent where the reader requires independent endpoints, and all registries cover the declared AMM/locker scope.
4. All executable hashes are measured from deployed finalized code and reviewed.
5. Developer-root identity evidence is signed, token-bound, fresh and independently trusted.
6. Provider readiness reports no unresolved Stage 5 blockers.
7. Capability allowlists are updated only after review of the exact provider IDs and acceptance artifacts.

Never place RPC URLs containing credentials, API keys, private keys or signed payload secrets in source files, issue comments or chat. Store them in GitHub Actions Secrets. Do not enable live trading as part of this stage; all acceptance workflows are read-only.
