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
