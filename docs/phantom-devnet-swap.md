# Phantom Devnet direct CPMM swap

This is a Devnet-only, single-pool SOL → official Devnet USDC test interface. It is not a live trading interface. Mainnet is unsupported.

## Transaction acquisition

- The page loads the pinned `@raydium-io/raydium-sdk-v2@0.2.64-alpha` package.
- It reads the pinned CPMM pool `5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPg8K8HZYvjoMiwo` from canonical Solana Devnet RPC.
- It builds exactly one legacy transaction. Versioned transactions and address lookup tables are rejected on this Phantom KMS compatibility path.
- The only supported swap pair is WSOL to the official Devnet USDC mint.
- SOL input is parsed as integer lamports. The browser does not expose a user-configurable slippage field.
- Price impact is displayed but is not a universal rejection threshold.

## Automatic quote and execution protection

The browser loads the side-effect-free `wallet/automatic-exchange-pricing.mjs` policy module (also exposed through the static-site entrypoint `automatic-exchange-pricing.mjs`).

The quote snapshot binds:
- canonical Devnet identity, quote ID and pool;
- input/output mint, raw amount and token decimals;
- quoted raw output and exact minimum raw output;
- quote timestamp and 15-second expiry;
- USD-normalized pool liquidity and trade size;
- observed short-window pool-price volatility and route hop count;
- trade fee, creator fee, route fee, network fee and platform-fee state;
- a canonical SHA-256 snapshot commitment.

The automatic protection threshold is bounded to 15–300 basis points and is calculated from quote age, trade size relative to pool liquidity, observed volatility and route complexity. The threshold is not user-configurable. Minimum output uses integer floor arithmetic and is encoded into the CPMM instruction.

Before Phantom can be asked to sign, the page:
1. verifies the immutable quote snapshot hash;
2. re-reads the pool and recomputes output, liquidity and short-window volatility;
3. rejects the review if the quote expired, output is below the reviewed minimum, current risk requires a wider protection threshold, or the trade identity changed;
4. rechecks the exact network fee and CPMM trade/creator fee amounts;
5. rechecks the wallet's SOL and USDC balance baselines, blockhash, pool/account state, token-account rent, and the exact reviewed transaction bytes;
6. reruns the browser semantic audit and unsigned simulation against the exact reviewed bytes.

Any material drift invalidates the transaction and requires a new quote and a new user review. The page does not silently worsen the exchange terms or mutate the reviewed transaction.

## Transparent fees

The review separately displays:
- CPMM trade fee from the SDK quote (input-token units);
- CPMM creator fee from the SDK quote, with its actual input/output token denomination;
- route fee (zero for this direct single-pool route);
- estimated Solana network fee from `getFeeForMessage`;
- persistent USDC associated-token-account rent, when a new ATA is needed;
- temporary WSOL funding, whose rent is returned when the temporary account closes;
- Veltrix platform fee.

The Veltrix platform fee is currently **0**. No platform recipient or transfer instruction is configured. A nonzero fee is rejected by the pricing module unless a bounded explicit rate, valid public-key recipient and independently verified transaction instruction are supplied. The current direct CPMM page does not enable that path.

The estimated net SOL debit is input + network fee + persistent ATA rent. Temporary WSOL rent is a peak-funding requirement and is returned in the transaction. DEX trade and creator fees are already reflected in the CPMM quote output; they are disclosed, not deducted a second time.

## Exact-byte and Phantom boundary

- The page requires Phantom `signTransaction` and legacy transaction support.
- The reviewed message bytes are snapshotted before the wallet provider receives the transaction.
- The sign-only Android test calls Phantom `signTransaction`, verifies the returned message is byte-for-byte identical, verifies the wallet signature and packet-size limit, and does not call any submission RPC.
- The actual execution function has a hard `EXECUTION_RELEASE_ENABLED = false` guard. The execution control remains disabled.
- If execution is ever separately enabled after release review, the transaction path must submit the exact locally verified signed wire bytes through canonical Devnet RPC; it must not delegate signing and submission together to `signAndSendTransaction`.
- Any wallet mutation, including a changed ComputeBudget instruction, account key, blockhash, fee payer or instruction payload, fails closed.

## Confirmation reconciliation implementation

The still-disabled execution path requires:
- exact equality between the confirmed message and immutable reviewed message bytes;
- matching RPC-returned signature and local wallet signature;
- confirmed transaction metadata with no on-chain error;
- exact SOL debit equal to input + actual network fee + persistent ATA rent;
- actual USDC token-balance delta meeting the reviewed minimum;
- current wallet SOL balance equal to the confirmed transaction post-balance;
- current wallet USDC balance delta equal to the independently decoded transaction output.

A confirmed transaction with missing or inconsistent evidence is `CONFIRMED_BUT_UNVERIFIED`; it must never be automatically retried.

## CI validation

The preflight workflow is read/simulation-only. It:
- runs the Python safety/regression tests;
- validates the inline browser JavaScript;
- runs the Phantom byte/semantic adversarial suite;
- runs automatic quote expiry, liquidity/volatility, snapshot-integrity, token-decimal, fee-rounding, recipient-guard and balance-reconciliation tests;
- builds and simulates an unsigned 0.005 SOL direct CPMM legacy transaction;
- runs the browser audit and one-unit-higher minimum-output adversarial simulation;
- reconciles the historical Devnet signature read-only.

CI must never sign, submit or broadcast a transaction. Live provider smoke tests that require credentials or wallet interaction may remain explicitly skipped and must not be represented as passing live evidence.

## Android sign-only validation

A manual Phantom Android in-app-browser test is still required:
1. Open the current HTTPS preview inside Phantom on Android.
2. Connect the intended wallet and verify Solana Devnet.
3. Build a fresh quote and inspect the full fee breakdown, minimum output, quote expiry and total debit.
4. Select **Verify Phantom legacy signing (no broadcast)**.
5. Review the legacy transaction in Phantom and approve the signature prompt.
6. Confirm the page reports exact reviewed-message equality and local signature verification, and explicitly reports that no transaction was submitted or broadcast.
7. Confirm the execution control remains disabled.

This validates the legacy Phantom KMS signing operation without executing a swap. A successful Android sign-only test is necessary but does not itself authorize execution.

## Release boundary

Keep the PR open and unmerged until exact-head CI is green and the Android sign-only test has been observed. Do not enable submission until the exact-byte, fee, balance, confirmation and adversarial release gates are all verified. Devnet only; no mainnet or automated signing.
