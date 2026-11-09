# Paper-entry versus live-execution evidence gates

## Why the gates are separate

A market route observation and a successful transaction simulation are different
claims. A Jupiter quote can support a paper-market model, but it cannot prove
that a real wallet can transfer the token, pay fees, or land the transaction.
Likewise, an unsigned RPC simulation is only a point-in-time execution diagnostic;
it is not authorisation to sign or broadcast.

## Paper-entry gate

A paper entry may be considered only when all mandatory token-risk evidence is
complete and fresh, including:
- Finalized mint-account identity, authorities and complete Token-2022 extension review
- Reconciled holder inventory and supply concentration
- Developer ancestry and launch-window bundle evidence with explicit coverage
- Positive, independently verified liquidity evidence
- Composite safety certificate
- At least two distinct BUY agents agreeing on the same mint
- A fresh, bounded sell-side route observation for the intended paper exit size
- Configured paper balance, position size and concurrent-position limits

The paper route requirement is explicitly
`ROUTE_OBSERVED_NOT_EXECUTION_PROVEN`. The UI and paper ledger must preserve
that exact evidence state. Paper PnL must use sell-side quotes and realistic
fees/slippage assumptions. It must never be presented as proof of real-world
sellability or live profitability.

## Live-execution gate (not enabled)

Live execution requires a separate future certification:
- Dedicated funded wallet and isolated signer/key custody
- Exact token balance and SOL fee/rent preflight
- Fresh unsigned transaction assembled for that wallet
- Required-signer identity and unsigned-signature validation
- On-chain simulation with successful instruction execution and reviewed logs
- Transaction decoding, allowed-program and account-meta policy
- Quote expiry, slippage, price impact, priority fee and loss limits
- Explicit transaction approval boundary, idempotency, chain confirmation and kill switch

The current simulation provider does not sign or submit. A successful simulation
does not itself satisfy the live gate; a separate signer and transaction-security
review remain mandatory.

## Current implementation state

- Sell-route observations, controlled unsigned transaction simulation,
  simulation diagnostics, and finalized test-wallet balance preflight are
  implemented as independent evidence components.
- Simulation runs only for explicitly configured mint/raw-amount test cases.
- Both simulation and its preflight remain disabled by default.
- Shield now requires a fresh, provenance-backed sell-side route observation
  matching the intended raw-token exit size before it can approve a paper entry.
- The execution-only `executable_sellability` unknown remains unresolved and is
  not conflated with token-risk unknowns. Unknown mandatory token-risk claims
  still veto paper entry.
- No live entry path exists or is authorised.
