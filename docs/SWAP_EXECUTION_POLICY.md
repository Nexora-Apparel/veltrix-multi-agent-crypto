# VELTRIX Swap Execution Policy

Status: Devnet-only transaction policy. Phantom approval remains manual and user-controlled. This document does not enable mainnet or automated wallet signing.

## Product behavior

VELTRIX does not impose an arbitrary fixed SOL input amount. The browser accepts positive decimal SOL amounts up to Solana's u64 lamport encoding limit, parses them with integer arithmetic, and carries the exact raw input amount through quote construction, instruction decoding, CPI evidence, simulation and confirmed-transaction reconciliation.

Price impact is quote information, not a universal rejection threshold. The user chooses the maximum slippage from 0% to 99.99%. The resulting minimum output is calculated in integer raw units, encoded into the CPMM instruction, displayed in the review, rechecked before Phantom approval and verified after confirmation. A minimum output that rounds to zero is rejected.

The current browser route is still a pinned direct Raydium CPMM SOL → Devnet USDC adapter. It does not claim to support every DEX or arbitrary route shape. The transaction builder must continue to respect actual Solana and protocol constraints.

## Fees

VELTRIX platform fee: **0%**.

No Veltrix treasury recipient, platform-fee transfer, hidden deduction or platform-fee reconciliation is configured or included. The user pays only the Solana network fee and the applicable DEX/liquidity-pool fee. Those are distinct from Veltrix revenue.

## Real execution constraints

Retain constraints required by Solana, the wallet, RPC provider or the selected protocol:
- positive input amount encoded as a u64 number of lamports;
- actual wallet balance, temporary WSOL funding, token-account rent and network fee;
- pool reserves, liquidity, protocol arithmetic and executable swap constraints;
- transaction packet-size limit, signer/fee-payer requirements and valid blockhash;
- canonical Devnet identity, address-lookup-table validity and resolved account indexes;
- RPC/faucet quotas and service availability.

A faucet request chunk size is a faucet transport detail, not a swap-input ceiling. If the faucet cannot provide the required test balance, the user must fund the connected Devnet wallet; the builder must not silently reduce the requested swap amount.

## Transaction integrity

The user approves the reviewed transaction in Phantom. Before approval, the browser independently audits the exact unsigned message, signer/fee payer, program identities, account identities and privileges, pool/mint/vault relationships, exact input amount, configured minimum output, temporary WSOL funding, token-account setup, CPI transfer evidence and simulated output.

The final pre-sign gate refreshes pool state and verifies that the new quote still clears the exact minimum output already encoded in the reviewed transaction. Price impact is not used as a universal cutoff. If the quote, wallet, blockhash, account state or reviewed bytes become stale, the transaction is invalidated and must be rebuilt.

After submission, reconciliation checks the confirmed signature, confirmed CPMM instruction input and minimum output, transaction semantics, and wallet USDC token-balance effects. The final wallet USDC balance delta must equal the independently reconciled actual output and meet the reviewed minimum. A confirmed signature with unavailable or inconsistent metadata is reported as confirmed-but-unverified; it must never be treated as a failed swap that should be retried automatically.

## Current implementation status

Implemented in `wallet/phantom-devnet-swap.html` on this branch:
- exact decimal SOL-to-lamports parsing using BigInt, with no fixed 0.001 SOL assertion;
- dynamic input binding in the direct CPMM instruction, router compatibility policy, CPI transfers, simulation event/log checks and confirmation decoder;
- configurable slippage in basis points, with the calculated minimum encoded in the transaction and shown in review;
- removal of the universal 2% price-impact rejection from quote and final freshness gates;
- pre-funding before SDK transaction construction so faucet activity does not stale the transaction blockhash;
- confirmed instruction input/minimum validation and exact reconciliation between confirmed wallet token-balance delta and actual output.

Validation completed on the current CI head:
- full Python regression suite passed;
- isolated Devnet/Phantom page safety tests passed;
- embedded browser JavaScript syntax check and adversarial Node verifier passed;
- unsigned direct CPMM transaction built and simulated successfully at 0.005 SOL (5,000,000 lamports) input with 250 bps (2.50%) configured slippage;
- observed quote: 10,904 raw USDC output, 10,631 raw minimum output, 11,212 raw simulated actual output;
- measured informational price impact: 3.5196687%; the CI assertion required impact above 2% and the transaction still built and simulated successfully;
- adversarial simulation with minimum output one raw unit above the observed output was rejected; the unsigned transaction was not signed or submitted;
- the separate read-only prior-signature reconciliation workflow passed.

Still required before release:
- exercise additional user-selected input amounts and slippage values in a fresh browser quote;
- test Phantom's supported legacy and v0 approval paths on the deployed current build;
- perform a fresh manual Phantom Devnet approval only after reviewing the exact transaction in the in-app browser.

The previously reported successful Phantom swap was performed against an earlier deployed build. It is not evidence that this new dynamic-amount/slippage build has passed a live test.

## Release boundary

- Solana Devnet only.
- Phantom approval remains manual.
- No mainnet, automated signing, Veltrix platform fee or production release.
