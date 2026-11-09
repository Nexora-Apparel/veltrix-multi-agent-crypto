# Solana Sell-Route Evidence

## Purpose

A token can have a buy route while its sell route is missing, blocked, or economically
unusable. VELTRIX therefore evaluates an exact-in token-mint to wrapped-SOL quote as a
separate evidence claim.

The first implementation is a provider-neutral evaluator. It accepts a typed quote
observation and validates its identity, direction, amount, provenance, freshness,
expiry, route presence, minimum output, price impact, and test size relative to
finalized token supply. It does not make network calls itself and has no wallet or
transaction dependency.

## Conservative quote profile

- Sell direction must be the candidate mint into the canonical wrapped-SOL mint.
- Quote input amount must match the requested raw token amount exactly.
- The test amount must be 0.5%–5% of finalized raw token supply by default.
- Quote age must be at most 15 seconds and its expiry must still be in the future.
- A non-empty route plan, positive quoted output and positive minimum output are required.
- Minimum output cannot exceed quoted output.
- Price impact must be finite, non-negative and at most 300 basis points by default.
- Provider identity and a non-empty evidence identifier are mandatory.
- Missing, stale, malformed, mismatched or high-impact evidence fails closed.

These are initial evidence-quality bounds, not trading thresholds. They require
calibration against real market depth and must not be relaxed merely to increase
candidate throughput.

## What this does not prove

A quote is not an executable sale. It does not prove that token transfer hooks,
transfer fees, account state or program restrictions permit the actual transfer;
that the quote remains valid when a transaction is built or submitted; that an
unsigned transaction simulates successfully against current chain state; that the
transaction will land, settle, or deliver the quoted output; or that the route
remains available for the entire position size.

The execution_proven field is permanently false in this evidence type. The state
ROUTE_OBSERVED_NOT_EXECUTION_PROVEN must never satisfy the existing
executable_sellability safety claim or approve a paper/live entry by itself.

## Jupiter integration boundary

Jupiter Swap V2 order construction requires a taker public key. VELTRIX will not
borrow the user's Phantom address or ask for a private key to obtain quote evidence.
A future adapter must use a dedicated, non-custodial test-wallet public address
or a documented quote endpoint that genuinely supports taker-free quoting. The
adapter must not sign or submit the returned transaction.

Before any execution claim, a separate controlled simulation must verify the
unsigned transaction with an isolated test payer, and token-specific transfer
semantics must be reviewed. This stage intentionally does not add a Jupiter
network adapter, API key, signer, or live execution path.
