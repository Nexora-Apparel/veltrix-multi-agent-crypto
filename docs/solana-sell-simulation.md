# Controlled Solana Sell-Transaction Simulation

## Purpose

A Jupiter sell quote is a route observation, not proof that the transaction can
execute against current token-account and program state. This optional provider
adds a simulation observation for a configured, dedicated test-wallet public key.

## Request sequence

1. Request Jupiter Swap API V2 `GET /order` for the exact candidate-mint to
   wrapped-SOL sell amount, supplying the configured test-wallet public key as
   `taker`.
2. Validate the returned mint pair, raw input amount, request ID and base64
   transaction. Decode the Solana versioned transaction, require the configured
   test wallet to appear in the required signer set, and reject any non-zero
   signature bytes before simulation.
3. Pass the unsigned transaction to the configured Solana RPC
   `simulateTransaction` method with `encoding=base64`,
   `sigVerify=false`, `replaceRecentBlockhash=true`, and
   `commitment=processed`.
4. Persist the simulation result, slot, compute units, logs and error state.

## Security boundary

- The taker is a public key only. No private key, seed phrase or signer is read.
- The returned transaction is never signed, submitted, broadcast or executed.
- The RPC method is simulation only; no sendTransaction call exists in this
  provider.
- The simulation provider is not enabled by default and is not yet wired into
  the production runtime.
- Use a dedicated test wallet, never the user's Phantom wallet or funding
  address. It must hold the actual candidate token amount and any required
  accounts for the simulation to be meaningful.

## Interpretation

`SIMULATION_SUCCEEDED` means the unsigned transaction simulated without an
instruction error against the RPC's state at the returned slot, with signature
verification explicitly disabled and the recent blockhash replaced. It does
not prove the transaction will land, that the quote will remain valid, that
market liquidity will remain available, or that the token can be sold later.

`SIMULATION_FAILED` is retained as a failed observation with the RPC error and
logs. Insufficient token balance, missing accounts, transfer hooks, transfer
fees, program restrictions and stale state can all cause failure. The failure
must not be translated into a permanent token-level honeypot verdict without
classifying the cause.

A simulation observation is not yet certified safety evidence and cannot
satisfy the `executable_sellability` claim or authorise a paper/live entry.


The Jupiter response does not expose a documented `taker` response field. The
provider therefore binds the requested taker by inspecting the returned
transaction's required signer set and verifies that every signature slot is
zero before calling RPC simulation. This avoids trusting an undocumented
response attribute.
