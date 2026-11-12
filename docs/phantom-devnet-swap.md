# Phantom Devnet direct CPMM page

This static page is the wallet-owner boundary for a single-pool, unsigned Solana Devnet swap test. It is not a live trading interface.

## Acquisition path

- The browser dynamically loads the pinned `@raydium-io/raydium-sdk-v2@0.2.64-alpha` package.
- It resolves the exact Devnet CPMM pool `5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo` through canonical Devnet RPC.
- It builds one direct CPMM v0 transaction. The auto-routing Trade API is not used for transaction acquisition.
- The only supported pair is WSOL to the official Devnet USDC mint.
- Input is fixed at exactly 0.001 SOL (1,000,000 lamports) for this isolated test.
- The page retains a 0.05 SOL balance reserve.
- Price impact is calculated from the live pool reserves and must not exceed 2%.
- The CPMM instruction minimum output is set to a 1% slippage floor and checked against the exact reviewed transaction bytes.

## Browser semantic audit

Before displaying the review summary, the page independently:

1. Checks the canonical Devnet genesis hash.
2. Resolves the transaction's v0 address lookup table from finalized Devnet state, checks ALT ownership/lifecycle/index bounds, and reconstructs all loaded account keys.
3. Reconstructs effective signer and writable privileges.
4. Checks the exact five-instruction direct-CPMM setup/swap/cleanup shape, temporary WSOL account creation/initialization/close semantics, and USDC ATA creation semantics.
5. Reads the live CPMM pool/config/vault/mint/observation/authority accounts and checks their owners, identities and token pairings.
6. Simulates the exact unsigned transaction, inspects parsed inner CPIs, and proves the exact 1,000,000-lamport input transfer and actual USDC output transfer.
7. Simulates an adversarial clone with minimum output set one raw unit above the observed output; the CPMM program must reject it.

CI builds a fresh transaction through the pinned SDK and invokes this browser-side verifier against those exact serialized bytes and current canonical Devnet account state. The evidence artifact includes the transaction hash, ALT state, privilege map, actual output, and adversarial rejection result.

## Final pre-sign freshness gate and user approval

The page now exposes one explicit user-triggered Phantom approval action after the browser audit. It does not sign or submit on page load, on quote creation, or without the wallet owner's Phantom approval. The approval path:

- Require the prepared review to be no older than 15 seconds.
- Rebind the connected Phantom public key and canonical Devnet genesis.
- Recheck the current SOL balance against input, estimated network fee and the 0.05 SOL reserve.
- Verify the transaction's original recent blockhash is still valid.
- Re-read the pinned CPMM pool through the pinned SDK and recompute output, minimum output and price impact. Any quote drift invalidates the prepared transaction and requires a new build.
- Re-run the exact-byte semantic audit, finalized ALT resolution, live account checks, simulation and one-unit-higher adversarial rejection.
- Recheck the serialized bytes after the final audit.

This gate does not refresh or mutate a reviewed transaction. If any freshness condition changes, it rejects the old transaction and requires the user to build a new one. The unconditional signing guard remains first in `execute()`, so this gate is currently unreachable from the UI.

## Signing remains disabled

- The review UI deliberately leaves the Phantom action disabled.
- The execution function throws a fail-closed error before any signing or sending code.
- Do not remove this guard based only on a CI pass. First complete the Phantom Android in-app-browser smoke test and independent release review.
- No seed phrase or private key is requested, accepted, derived, exported or stored.
- No Mainnet cluster, mint, RPC endpoint or execution path is supported by this page.

## Android in-app-browser smoke test

After the page is hosted over HTTPS and opened inside Phantom on Android:

1. Confirm the browser loads the pinned SDK module from `esm.sh`.
2. Connect the intended Phantom Devnet wallet and confirm the page reports Devnet.
3. Request the fixed 0.001 SOL quote.
4. Confirm the single-pool CPMM review details appear and the live semantic audit reports success.
5. Confirm the Phantom action remains disabled and no Phantom signing prompt appears.
6. Close the page. Do not sign, submit or broadcast a transaction during this smoke test.

If the SDK import, pool identity, ALT resolution, account checks, simulation or adversarial threshold test fails, the page must fail closed. There is no automatic router or multi-hop fallback.

## Evidence boundary

A quote or successful simulation is not a swap. A swap is counted as successful only after a user-approved transaction has a real signature, Devnet confirmation, and an independently verified positive Devnet USDC balance delta. This isolated page does not authorize a swap or enable unattended trading.
