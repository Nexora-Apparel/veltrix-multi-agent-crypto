# VELTRIX Swap Execution Policy

Status: **Devnet only; transaction submission hard-disabled pending validation.** Phantom signing is manual and currently available only through a sign-only compatibility test. No mainnet, autonomous signing, or live execution is authorized.

## Product behavior

The browser accepts positive SOL amounts within the supported u64 lamport range. It does not expose a user-configurable slippage percentage.

The quote engine creates an immutable, SHA-256 committed quote snapshot with:
- exact input/output mints, token decimals and raw input amount;
- quoted output and integer minimum output;
- quote timestamp and 15-second expiry;
- USD-normalized trade size and pool liquidity;
- observed short-window volatility and route complexity;
- separately itemized pool trade fee, creator fee, route fee, network fee and platform-fee state.

Automatic execution protection is calculated from quote age, liquidity relative to trade size, volatility and route complexity. It is bounded to 15–300 bps. If the quote expires, output falls below the reviewed minimum, liquidity/volatility/route risk requires a wider threshold, or a fee schedule changes, the prepared transaction is invalidated and must be rebuilt and reviewed. No worse exchange is silently accepted.

## Fee accounting

The direct CPMM review discloses:
- DEX trade fee from the CPMM quote;
- creator fee from the CPMM quote, including its token denomination;
- route fee (zero for the single-pool direct route);
- network fee from the exact transaction message;
- persistent output-token account rent;
- temporary WSOL funding/rent (returned when the temporary account closes);
- Veltrix platform fee.

The current Veltrix platform fee is **0**. No platform fee recipient or verified transfer instruction is configured. The pricing module rejects a nonzero platform fee unless a bounded explicit rate, a valid recipient and an independently verified transfer instruction are provided. The current browser transaction builder does not enable a platform-fee transfer.

Pool trade/creator fees are included in the quote output and must not be deducted twice. Network fee and account rent remain separately visible. No hidden deduction or undisclosed recipient is permitted.

## Phantom KMS compatibility and exact bytes

The observed Android KMS diagnostic included the failure:
`Loaded accounts are not provided, but lookup table is used in the transaction`.

The Phantom compatibility path therefore builds legacy messages only and rejects v0/ALT transactions before the signing boundary.

The sign-only Android test:
1. runs the final quote, wallet, blockhash, pool, fee and semantic checks;
2. passes the immutable reviewed legacy transaction to Phantom `signTransaction`;
3. verifies the returned signed message bytes exactly equal the reviewed message bytes;
4. verifies the wallet signature and Solana packet-size limit;
5. never submits or broadcasts the signed transaction.

The actual execution function is protected by `EXECUTION_RELEASE_ENABLED = false`. The execution control remains disabled. This guard must not be removed until the full CI suite, Android signing test, exact-byte verification, fee and balance reconciliation, and adversarial release gates have passed.

If execution is separately authorized later, it must submit the exact locally verified signed wire bytes through canonical Devnet RPC. It must not use Phantom `signAndSendTransaction`, and it must reject any signed-message mutation, including ComputeBudget changes.

## Confirmation and balance reconciliation

The disabled execution path independently requires:
- exact confirmed message bytes equal to the immutable reviewed bytes;
- RPC-returned signature equal to the locally verified wallet signature;
- no on-chain execution error;
- SOL debit exactly equal to input + actual network fee + persistent ATA rent;
- USDC token-account balance delta meeting the reviewed minimum;
- final live SOL balance equal to the confirmed transaction post-balance;
- live USDC balance delta equal to the independently decoded transaction output.

Missing or inconsistent evidence is `CONFIRMED_BUT_UNVERIFIED`. Ambiguous submission is never automatically retried.

## Solana constraints

Retain the constraints required by Solana, Phantom, RPC and the selected CPMM:
- positive input amount encoded as u64 lamports;
- exact fee payer and required signer;
- valid recent blockhash;
- 1232-byte transaction packet limit;
- canonical Devnet genesis hash;
- pinned CPMM pool, mint, vault and token-program identities;
- correct account privileges and exact instruction data;
- live token-account presence/rent and wallet funding;
- RPC/faucet quotas and service availability.

A faucet chunk size is a transport detail, not a swap-input ceiling. If faucet funding is unavailable, fail closed; never silently reduce the requested swap amount.

## Validation gates

CI is read/simulation-only and must never sign, submit or broadcast. It runs:
- Python compile and regression suite;
- isolated Devnet/Phantom safety tests;
- browser JavaScript syntax validation;
- Phantom byte/semantic adversarial tests;
- automatic quote expiry, risk drift, snapshot integrity, token-decimal, fee-rounding, recipient and balance-reconciliation tests;
- unsigned direct CPMM legacy build and Devnet simulation;
- one-unit-higher minimum-output adversarial rejection;
- read-only historical Devnet signature reconciliation.

Live provider tests that require unavailable credentials or a physical wallet may be skipped only when explicitly reported as skipped. A skipped live test is not passing live evidence.

## Release boundary

Do not merge or deploy an execution-enabled build until:
1. the complete CI suite is green on the exact PR head;
2. Phantom Android sign-only legacy signing passes;
3. reviewed/signed/confirmed transaction message bytes are proven identical;
4. fee accounting is reconciled to on-chain data;
5. actual SOL and USDC wallet balance changes reconcile;
6. all adversarial tests pass;
7. independent release review is complete.

Devnet only. No mainnet, autonomous wallet signing, or automatic transaction retry.
