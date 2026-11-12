# Phantom Devnet direct CPMM page

This static page is a wallet-owner-controlled, one-time Solana Devnet swap test. It is not a live trading interface.

## Acquisition path

- The browser dynamically loads the pinned `@raydium-io/raydium-sdk-v2@0.2.64-alpha` package.
- It resolves the exact Devnet CPMM pool `5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo` through canonical Devnet RPC.
- It builds one direct CPMM legacy (non-versioned) transaction. Address Lookup Tables are prohibited on this Phantom compatibility path; the auto-routing Trade API is not used for transaction acquisition.
- The wallet compatibility adapter checks the provider's advertised transaction versions when available, requires legacy support before quote construction and rechecks before approval. When Phantom does not expose a version list, it uses Phantom's documented legacy `Transaction` provider API. It never silently falls back to v0.
- The only supported pair is WSOL to the official Devnet USDC mint.
- Input is fixed at exactly 0.001 SOL (1,000,000 lamports).
- The page retains a 0.05 SOL balance reserve.
- Price impact is calculated from live pool reserves and must not exceed 2%.
- The CPMM instruction minimum output is derived from the final quoted raw output using integer floor arithmetic at 1% slippage and is checked against the exact reviewed transaction bytes.

## Browser semantic audit

Before displaying the review summary, the page independently:

1. Checks the canonical Devnet genesis hash.
2. Requires a legacy message with zero address-table lookups, reconstructs every static account key, and rejects any v0/ALT transaction on this path.
3. Reconstructs effective signer and writable privileges.
4. Checks the exact five-instruction direct-CPMM setup/swap/cleanup shape, temporary WSOL account creation/initialization/close semantics, and USDC ATA creation semantics.
5. Reads the live CPMM pool/config/vault/mint/observation/authority accounts and checks their owners, identities and token pairings.
6. Simulates the exact unsigned transaction, inspects parsed inner CPIs, and proves the exact 1,000,000-lamport input transfer and actual USDC output transfer.
7. Simulates an adversarial clone with minimum output set one raw unit above the observed output; the CPMM program must reject it.

CI builds a fresh legacy transaction through the pinned SDK, enforces the 1232-byte packet limit, and invokes this browser-side verifier against those exact serialized bytes and current canonical Devnet account state. Evidence includes transaction size, static account/privilege map, zero ALT count, actual output, and adversarial rejection result.

## User approval and final freshness gate

The page exposes one explicit user-triggered Phantom approval action after the browser audit. It never signs or submits on page load or when a quote is created. Immediately before opening Phantom, the page:

- Requires the prepared review to be no older than 15 seconds.
- Rebinds the connected Phantom public key and canonical Devnet genesis.
- Rechecks the current SOL balance against input, estimated network fee and the 0.05 SOL reserve.
- Rechecks that the wallet's Devnet USDC balance has not changed since quote preparation.
- Verifies the transaction's original recent blockhash is still valid.
- Re-reads the pinned CPMM pool and recomputes output, minimum output and price impact. Any quote drift invalidates the prepared transaction.
- Re-runs the exact-byte semantic audit, finalized ALT resolution, live account checks, simulation and one-unit-higher adversarial rejection.
- Rechecks the serialized transaction bytes after the final audit.

If any freshness condition changes, the page rejects the old transaction and requires a new quote/build/audit. It does not mutate or silently refresh a reviewed transaction.

## Submission and settlement verification

- Phantom's `signAndSendTransaction` is called only after the final freshness gate passes and only from the user's explicit button click.
- The wallet owner must inspect and approve the transaction in Phantom. The page cannot approve on the user's behalf.
- The page waits for Devnet confirmation, retrieves the confirmed legacy transaction, and compares its message bytes with the reviewed message.
- The page then checks the wallet's Devnet USDC balance increase against the reviewed minimum output.
- A signature or successful simulation alone is not reported as a successful swap.
- If Phantom explicitly rejects approval, the action may be retried only while the prepared review remains fresh. If the submission result is ambiguous or a signature exists, the action is locked against retries until wallet activity is inspected.
- No seed phrase or private key is requested, accepted, derived, exported or stored.
- No Mainnet cluster, mint, RPC endpoint or execution path is supported.

## Android in-app-browser test

1. Open the HTTPS preview inside Phantom on Android.
2. Connect the intended Devnet wallet and verify the canonical Devnet network.
3. Request a fresh quote for exactly 0.001 SOL.
4. Inspect pair, input, quoted output, automatically calculated minimum output, 1% slippage, price impact and fee.
5. Select the approval action only if the displayed transaction matches the intended test.
6. Review the transaction in Phantom. Reject it if any wallet, amount, mint, pool or fee detail is unexpected.
7. If approved, wait for the page to report confirmed signature and verified USDC balance increase.

There is no automatic router or multi-hop fallback. Any failed verifier, freshness check, confirmation, or balance-delta check must fail closed.

## Evidence boundary

A quote or successful simulation is not a swap. The test is complete only after a user-approved transaction has a real signature, Devnet confirmation, exact-message reconciliation, and a verified positive Devnet USDC balance delta meeting the minimum output.
