# Transaction-intent verifier contract (Devnet one-time test)

Status: **design contract only; signing remains disabled**.

## Scope

Accept only the exact Devnet SOL -> Devnet USDC swap prepared for the connected wallet, with:
- input exactly 1,000,000 lamports (0.001 SOL) for this one-time test;
- output mint `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`;
- minimum output copied from the freshly reviewed quote and checked against the decoded swap instruction;
- no additional signer and no unexpected economic instruction;
- a maximum 2% quoted price impact.

The verifier must reject all other amounts, mints, recipients, signers, instruction variants and transaction shapes. No mainnet support is in scope.

## Important instruction-layer distinction

The simulation logs observed a Raydium route/aggregator instruction that invokes CPMM through CPI. The top-level instruction in the serialized transaction is therefore not necessarily the CPMM `SwapBaseInput` instruction. Decoding the inner CPMM instruction layout alone is **not** sufficient to validate the top-level transaction.

The verifier must first identify and decode the exact top-level Devnet router instruction and its route accounts using a version-pinned, authoritative Raydium Devnet IDL/SDK. It must then reconcile the router's route with the expected CPMM CPI semantics. If the exact router program, discriminator, argument layout, account order, or CPI path cannot be established from authoritative artifacts, reject the transaction.

## CPMM semantic reference

Raydium's published CPMM instruction reference defines `SwapBaseInput` as exact-input with two u64 arguments:
1. `amount_in`
2. `minimum_amount_out`

Its 13 ordered accounts are:
1. payer (signer)
2. authority
3. amm_config
4. pool_state (writable)
5. input_token_account (writable)
6. output_token_account (writable)
7. input_vault (writable)
8. output_vault (writable)
9. input_token_program
10. output_token_program
11. input_token_mint
12. output_token_mint
13. observation_state (writable)

Anchor discriminator for `global:swap_base_input`: `8fbe5adac41e33de`.

The published mainnet CPMM IDL address is `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C`. Do not assume this is the Devnet program address. The actual Devnet program ID must be pinned from the live Devnet route and verified against the corresponding authoritative deployment/IDL before acceptance.

## Versioned-message and account validation

- Resolve every address lookup table (ALT) from canonical Devnet RPC.
- Verify each table account owner, deserialization, deactivation/extension state and referenced index bounds.
- Reconstruct the complete account-key list in Solana v0 message order before resolving any compiled instruction index.
- Validate signer and writable privileges after reconstruction.
- Verify wallet ownership of the input account, mint identity and exact wrapped-SOL amount.
- Verify the output token account is owned by the connected wallet and has the exact Devnet USDC mint.
- Verify pool/vault mint pairing and token-program ownership against live account data.
- Check the exact quote input and minimum output against the decoded router and CPMM instruction values.

## Reject conditions

Reject the entire bundle on any:
- unknown top-level program, router discriminator, inner CPI program or instruction;
- extra signer, unexpected fee payer, or signer privilege escalation;
- System Program transfer not explicitly proven to be a required, bounded account-rent/setup operation;
- SPL Token transfer, close-account, approve/revoke, set-authority, mint, burn, or delegate operation not explicitly proven necessary and bounded;
- unexpected recipient, mint, token account, pool, vault, or authority;
- amount mismatch, altered minimum output, missing minimum output, or route mismatch;
- unresolved, malformed, stale, or out-of-range ALT;
- unrecognized setup instruction or transaction bundle ordering;
- any mismatch between inspected serialized bytes and the exact bytes later submitted to Phantom.

## Required adversarial tests

Include fixtures for:
- arbitrary SOL transfer to an attacker;
- arbitrary SPL transfer to an attacker;
- close-account to an unexpected lamport recipient;
- set-authority / approve / revoke / mint / burn;
- wrong input or output mint and wrong ATA owner;
- altered input amount and altered minimum output;
- extra signer and changed fee payer;
- unknown router program/discriminator;
- wrong CPMM discriminator or wrong account order;
- ALT index substitution, missing ALT, wrong ALT owner and out-of-range index;
- unexpected instructions before, between or after swap setup/swap instructions;
- multiple transactions with altered order or unexpected extra transaction.

## Release gate

The Phantom page must continue to throw from `inspectTransaction` until all checks above are implemented and tested against the exact serialized transaction bytes. A successful quote, simulation, CI run, or Phantom connection is not authorization to sign and is not evidence of an executed swap.


## Live transaction fixture findings (2026-11-11)

The unsigned transaction captured by CI run
[43070594409](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43070594409)
was decoded as a Solana v0 message. It contains one required signature (the wallet fee payer),
16 static account keys, five top-level instructions, and one address lookup table. The lookup
table contributes one writable key and one read-only key. The live simulation logs identify:

- router program: `DRaybByLpbUL57LJARs3j8BitTxVfzBg351EaMr5UTCd`;
- CPMM Devnet program invoked by CPI: `DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb`;
- pool: `5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo`;
- input: `1,000,000` lamports;
- minimum output: `3,396` raw Devnet USDC units;
- observed CPMM CPI log: `Instruction: SwapBaseInput`.

The two router instructions in this fixture are:
1. instruction tag `5`, nine bytes total, followed by little-endian u64 `1,000,000`;
   simulation logs identify this as `process_create_sync_native`;
2. instruction tag `0`, followed by little-endian u64 input `1,000,000` and
   little-endian u64 minimum output `3,396`. The SDK route builder serializes this as
   `u8(insId) + u64(amountIn) + u64(amountOut)` for a CPMM route.

The matching router account order in the pinned SDK implementation is:
- fixed prefix: classic SPL Token program, Token-2022 program, Associated Token program,
  System Program, wallet signer, source token account, destination token account;
- per CPMM pool: CPMM program ID, output token account, input mint, output mint,
  CPMM authority, AMM config, pool state, input vault, output vault, observation state.

The authoritative Raydium SDK source used for this layout is
`raydium-io/raydium-sdk-V2` commit
`1cf5bb129dbbb1c76dab67819c3d478349c8ed25`,
file `src/raydium/tradeV2/instrument.ts`, function
`swapBaseInAutoAccount`. This is SDK evidence for instruction construction, not a substitute
for deployed-program verification. The exact Devnet router deployment and its executable
semantics still require independent confirmation.

The CPMM instruction schema is mirrored in
`raydium-io/raydium-idl` commit
`e7e0c96fe77bcf6a020b84a44c47a722aac8e359`,
file `raydium_cpmm/raydium_cp_swap.json`. Its `swap_base_input` discriminator bytes are
`[143,190,90,218,196,30,51,222]`; the deployed Devnet program ID is the Devnet ID listed
above, not the mainnet `address` field in that IDL.

## Remaining verification requirements specific to the captured fixture

- Resolve lookup table `EFhMuDw1PKEuckuFRW9PavNfTH4LKP5uKHgyXDmWpFCq` from canonical Devnet RPC.
  Confirm owner is the Address Lookup Table program, deserialize it, validate its lifecycle
  metadata and index bounds, and reconstruct the complete key list and effective privileges.
- Decode and validate the first router instruction's complete account list and the
  `process_create_sync_native` semantics. It must create/fund only the wallet's wrapped-SOL
  account with exactly 1,000,000 lamports.
- Decode the route instruction using the pinned SDK layout and match every account position
  against the resolved ALT-expanded key list and live account state.
- Verify the CPMM pool state, both vault accounts, both vault mint identities, authority PDA,
  config and observation account using Devnet account data and the pinned CPMM schema.
- Inspect transaction metadata with inner instructions enabled and require exactly the expected
  CPMM CPI plus only the necessary ATA/token/system setup/cleanup CPIs. Simulation logs alone
  are diagnostic evidence, not a cryptographic proof of the final execution path.
- Verify the close-token-account CPI returns rent only to the connected wallet and closes only
  the temporary wrapped-SOL account created by the first router instruction.
- Keep the signer guard active until these checks and mutation tests are implemented against
  the captured bytes and a newly built transaction.


## Canonical Devnet RPC audit findings (2026-11-11)

A read-only audit now resolves the captured ALT from `https://api.devnet.solana.com`
using finalized commitment. It checks the ALT program owner, initialization/deactivation
metadata, all referenced indexes, same-slot extension safety, the full v0 key order, and
effective signer/writable privileges. It also fetches the referenced program, mint, user ATA,
pool, config, vault and observation accounts in a finalized account snapshot.

The captured transaction's resolved router route has these verified structural properties:
- one wallet fee-payer signature slot, still all-zero/unsigned;
- 18 resolved account keys: 16 static keys, one loaded writable config key, one loaded
  read-only CPMM program key;
- the router is the pinned Devnet AMM Routing program and the CPMM CPI target is the pinned
  Devnet CPMM program;
- the three router instruction tags are `5` (wrapped-native setup), `0` (CPMM route),
  and `6` (wrapped-native cleanup), in that order;
- the outer route instruction carries exactly 1,000,000 lamports input and 3,396 raw USDC
  minimum output;
- the CPMM pool state, AMM config, authority PDA/bump, observation account, vault mint pair,
  vault token-program owners, wallet token-account derivations and mint decimals are checked
  against live Devnet account data.

**Critical rejection:** simulation of the captured route shows the inner CPMM
`SwapBaseInput` CPI receives `amount_in=1,000,000` but
`minimum_amount_out=0`. The outer router instruction carries `minimum_amount_out=3,396`,
but the AMM Routing program's source is not publicly available in Raydium's current program
reference. Therefore the outer threshold cannot be independently proven to be enforced after
the CPMM CPI from public source/IDL evidence alone. A successful simulation is not proof of
that post-CPI check. The route is consequently rejected for wallet signing.

The next safe implementation path is a direct Devnet CPMM transaction (not the opaque router):
the user ATA setup and cleanup instructions remain explicitly decoded, while the top-level
CPMM `SwapBaseInput` must itself carry the reviewed non-zero minimum output. The current
router transaction remains a valuable adversarial fixture and must never be signed.
