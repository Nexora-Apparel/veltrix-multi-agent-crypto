# Phase 09 — Live Execution Safety Gate

## Status

DESIGN / IMPLEMENTATION IN PROGRESS. Live execution is disabled. This phase does not authorize wallet signing, swap submission, token approvals, or any real-money transaction.

## Objective

Define and enforce an independently testable boundary between the completed simulation pipeline and any future live execution adapter. The default system must remain paper-only, including when configuration is missing, malformed, stale, or contradictory.

## Non-negotiable invariants

1. **Paper-only default:** live execution is disabled unless every independent gate is explicitly satisfied. Missing configuration always resolves to disabled.
2. **No private keys in the application:** never accept, persist, log, transmit, or expose seed phrases, raw private keys, or exported wallet secrets. A future signer must be isolated behind a separately reviewed signing boundary; this phase must not implement key import.
3. **Wallet isolation:** paper ledger and live wallet state are separate domains. Simulation code cannot obtain a signer or submit transactions.
4. **Explicit authorization:** live mode requires a deliberate operator action bound to an environment, wallet identity, approved limits, expiry, and an auditable authorization record. A generic boolean environment flag is insufficient.
5. **Fail closed:** stale market data, unavailable RPC, failed simulation, unknown token state, failed risk checks, clock skew, nonce/blockhash uncertainty, or any validation error must reject execution.
6. **Idempotency:** every proposed action has a stable unique intent ID. Retries must not create duplicate submissions.
7. **Bounded exposure:** enforce per-trade, per-token, total portfolio, and rolling daily-loss ceilings in integer base units. Limits are checked immediately before signing/submission, not only at strategy time.
8. **Emergency stop:** a persistent kill switch blocks new submissions; activation must not depend on the strategy worker or the same process that submits transactions.
9. **Transaction preflight:** decode and validate the complete transaction, destination program allowlist, token mint, spend amount, minimum output/slippage, fee ceiling, account changes, and simulation result before any signer is called.
10. **Auditability:** append-only records capture intent, evidence references, gate decisions, simulation, authorization, submission state, signature reference, and settlement. Never log secrets.
11. **No hidden fallback:** do not silently downgrade to weaker RPC, skip simulation, relax limits, or switch from live to paper while reporting a live success.
12. **No production activation in Phase 09:** implementation and tests must not submit a real transaction. A later, separately approved release process is required.

## Required architecture

- **ExecutionPolicy:** immutable policy snapshot with mode, limits, approved programs, slippage/fee ceilings, data freshness bounds, and policy version.
- **OperatorAuthorization:** short-lived, one-use authorization bound to wallet public key, chain/cluster, policy hash, and intent scope.
- **RiskGate:** deterministic validation of exposure, daily loss, token risk, market freshness, and emergency-stop state.
- **TransactionValidator:** parses the unsigned transaction and checks every instruction against explicit allowlists and intent parameters.
- **SimulationGate:** requires successful simulation against the intended cluster and rejects errors, missing simulation, or materially changed account state.
- **SignerBoundary:** interface only in this phase; no secret-loading or signing implementation. Tests use a fake signer that proves it is never invoked when any gate fails.
- **SubmissionCoordinator:** state machine with durable idempotency and explicit states: CREATED, VALIDATED, SIMULATED, AUTHORIZED, SIGNED, SUBMITTED, CONFIRMED, FAILED, CANCELLED. Terminal states cannot be reopened by retry.
- **EmergencyStop:** independently readable persistent state; fail closed if unavailable.
- **ExecutionAudit:** append-only, redacted event log with correlation IDs.

## Mandatory rejection conditions

Reject before signing if any of the following holds:
- live mode is not explicitly enabled by the approved release configuration;
- authorization is absent, expired, reused, mismatched, or has an invalid policy hash;
- wallet public key or cluster differs from the authorized values;
- emergency stop is active or cannot be read;
- market quote, token-risk evidence, RPC state, or simulation is stale/unavailable;
- token mint/program is not allowlisted or has unsupported extensions;
- instruction set contains an unknown program, unexpected account, delegate/authority change, or transfer outside the approved intent;
- requested spend, exposure, slippage, priority fee, or daily loss exceeds policy;
- transaction simulation fails or output/account changes differ from expected bounds;
- intent ID was already processed or submission outcome is ambiguous;
- audit persistence fails.

## Required test matrix

- default configuration remains paper-only;
- malformed or missing live configuration fails closed;
- every rejection condition prevents fake-signer invocation;
- one-use authorization cannot be replayed;
- authorization is invalidated by policy, wallet, cluster, or intent changes;
- concurrent duplicate intents result in at most one accepted state transition;
- limit calculations use integer base units and handle boundary values;
- kill switch blocks submissions across worker restarts;
- unavailable kill-switch storage blocks submissions;
- transaction parser rejects unknown instructions and unexpected account mutations;
- failed or absent simulation blocks signing;
- RPC timeout, stale blockhash, clock skew, and ambiguous submission are safely handled;
- audit records are append-only, correlated, and secret-redacted;
- paper-trading regression suite remains unchanged and passing.

## Release gates

Phase 09 is not complete until:
1. design and threat model are reviewed;
2. all gates above are implemented with unit, integration, and adversarial tests;
3. fake-signer tests prove zero signer calls on every rejection path;
4. concurrency and restart tests prove idempotency and persistent emergency-stop behavior;
5. static scans confirm no private-key/seed phrase handling or accidental secret logging;
6. full compilation and repository test suite pass on the PR head and after merge;
7. a separate operator-facing checklist documents manual wallet isolation and emergency-stop verification without requiring the user to expose secrets.

Passing CI is not proof of safe live trading. No real-money transaction is part of this phase.


## Implementation progress

### 09.1 — Execution policy and operator authorization

Implemented in `veltrix/execution_policy.py` with regression coverage in `tests/test_execution_policy.py`.

- Frozen policy snapshots use integer base-unit limits and a deterministic SHA-256 policy hash.
- Environment loading defaults to PAPER and rejects every truthy `LIVE_TRADING` value; unknown values fail closed.
- Authorization claims bind a one-use authorization ID to wallet public key, chain, policy hash, intent ID, issuance time and expiry.
- HMAC-SHA256 verification uses an injected key of at least 32 bytes; no key is loaded from disk or environment by this module.
- Authorization lifetime is capped at 300 seconds; scope, expiry, signature and replay checks are enforced before consumption.
- `veltrix/authorization_store.py` now provides a SQLite-backed atomic consumer. A unique primary key plus `BEGIN IMMEDIATE` prevents concurrent/restart replay; consumed IDs are retained rather than expired/deleted. The store persists authorization IDs and timestamps only, never signing keys or wallet secrets. This is a safety primitive, not a live execution path.
- LIVE policy objects exist only to test authorization binding semantics. Runtime environment construction can only produce PAPER, and no signer or transaction submission code is present.

This is an implementation milestone, not a release approval. The SQLite consumer has restart, concurrent-consumption, malformed-input, and authorization-integration tests. CI on the final PR head and code review are still required.


### 09.5 — fail-closed simulation evidence gate

Implemented in `veltrix/simulation_gate.py` with adversarial coverage in `tests/test_simulation_gate.py`.

- Requires a prior structural `TransactionValidationResult` and the same immutable `TransactionIntent`.
- Binds simulation evidence to the exact SHA-256 message digest and approved recent blockhash.
- Requires the configured cluster and genesis hash to match exactly, and the RPC provider identifier to be allowlisted.
- Rejects missing/error simulation, stale or excessively future-dated evidence, context slots below the caller's minimum, and blockhashes whose last-valid block height has passed.
- Enforces maximum compute units and fee lamports.
- Requires the complete simulated token-account set (account, mint, owner) and each raw-unit pre/post balance delta to match the explicit expected effects exactly. Missing, extra, duplicate, or altered account records fail closed.
- Uses integer raw token amounts only; no floating-point token arithmetic.
- The module consumes normalized evidence. It does not call RPC and cannot independently authenticate a provider response. A future RPC adapter must use authenticated transport, bind the raw response to the request/message, preserve provider/cluster/context metadata, and reject incomplete RPC fields before constructing evidence.
- This gate does not independently decode arbitrary SPL/Anchor instructions, verify mint extensions, model native SOL rent/fee balance changes, prove external program behavior, sign, or broadcast. Those remain separate requirements; a passing gate is not live-trade authorization.

No RPC client, signer, private-key handling, transaction submission, or live execution is introduced. Live execution remains disabled.


### 09.6 — provider-pinned read-only Solana RPC simulation adapter

Implemented in `veltrix/solana_rpc_simulation.py` with mocked-RPC adversarial coverage in `tests/test_solana_rpc_simulation.py`.

- Pins the HTTPS endpoint to an explicit host allowlist, rejects URL credentials/query/fragment, disables redirects, enforces bounded request timeouts, and validates JSON-RPC response IDs/envelopes.
- Checks the configured cluster genesis hash and exact approved recent blockhash validity; rejects expired blockhashes.
- Fetches pre-simulation token-account states with `getMultipleAccounts`, then simulates the exact serialized message with `sigVerify=false`, `replaceRecentBlockhash=false`, `encoding=base64`, and the requested post-simulation account list.
- Constructs only an unsigned transaction with default placeholder signatures. No signing keys are loaded and no `sendTransaction` method exists.
- Requests pre- and post-simulation token-account data and decodes only the fixed SPL Token base layout (mint, token authority, raw amount) for the legacy SPL Token and Token-2022 program owners.
- Requires pre-state, simulation, and fee-quote context slots to match exactly. This is intentionally strict: if a provider cannot supply a consistent bank snapshot, evidence collection fails rather than comparing balances from different slots.
- Retrieves the exact-message fee with `getFeeForMessage`, compute units from simulation, and block height / blockhash validity from read-only RPC methods.
- Does not implement native SOL balance-delta accounting, mint-extension policy, transfer-hook semantics, arbitrary program decoding, or live transaction submission. Token-2022 extension-specific semantics remain unsupported and must fail closed in the semantic decoder.

This is an RPC evidence adapter, not a trade executor. Live execution remains disabled.


### 09.7 — narrow semantic transfer decoder

Implemented in `veltrix/semantic_transfer_decoder.py` with adversarial coverage in `tests/test_semantic_transfer_decoder.py`.

- Re-runs structural validation on the exact message and requires the supplied validation result to match.
- Supports only System Program `Transfer`, classic SPL Token `TransferChecked`, and bounded Compute Budget `SetComputeUnitLimit` / `SetComputeUnitPrice`.
- Decodes raw integer lamports and token base units, binds token transfer source/mint/destination/authority/amount/decimals, enforces aggregate native/token ceilings, and checks compute-unit / micro-lamport priority-fee caps.
- Requires decoded effects to equal the explicitly approved effect tuples exactly.
- Rejects unknown programs, all Token-2022 instructions, unchecked SPL Token `Transfer`, multisig/extra-account variants, account creation, authority changes, duplicate/unknown Compute Budget instructions, and every unsupported System/Token instruction.
- This is deliberately not a DEX swap decoder. Jupiter, Raydium, Orca, route aggregators, CPI-heavy swaps, transfer hooks, Token-2022 extensions, ATA creation, and arbitrary Anchor/Borsh programs are unsupported and must fail closed. A future DEX-specific parser must decode the complete instruction and inner-CPI effect graph before any swap is eligible for simulation approval.

No signing, RPC submission, wallet access, or live execution is introduced.


### 09.8 — final evidence-composition preflight

Implemented in `veltrix/execution_preflight.py` with adversarial coverage in `tests/test_execution_preflight.py`.

- Requires matching message digests from structural validation, semantic instruction decoding, and SimulationGate approval.
- Requires fresh risk evidence for the exact approved mint, finalized mint slot, immutable mint/holder evidence IDs, classic SPL Token ownership, revoked mint/freeze authorities, completed extension review, complete holder coverage, and exact mint/holder supply reconciliation.
- Applies configurable top-1/top-5/top-10 holder concentration ceilings and blocks critical/high risk flags by default.
- Requires every currently defined live-risk claim: mint state/authority/freeze/extension review, complete holder inventory and supply reconciliation, exhaustive liquidity-pool lock inventory, developer-wallet linkage, bundled-supply analysis, creator-wallet provenance, and executable sellability.
- The current Phase 05 collector intentionally leaves several of these claims missing (liquidity-lock completeness, developer linkage, bundled supply, creator provenance and executable sellability). Therefore this gate will reject current reports until independent evidence sources are added and those claims are actually established. This is an explicit release blocker, not a reason to weaken the gate.
- A pass means only `eligible_for_operator_authorization`: it does not issue operator authorization, call a signer, sign a transaction, submit a transaction, or enable live execution.
- The semantic decoder is currently limited to direct classic SPL Token transfers; DEX swap transactions remain unsupported.

No live execution path is introduced. Paper trading remains separate and unchanged.


### 09.9 — read-only Jupiter Swap API V2 quote intake

Implemented in `veltrix/jupiter_quote.py` with adversarial coverage in `tests/test_jupiter_quote.py`.

- Uses Jupiter's current Swap API V2 `GET /swap/v2/order` quote path.
- Intentionally omits `taker`, so the response must contain no transaction payload and no taker binding. This is quote intake only.
- Supports optional `JUPITER_API_KEY`; keyless requests are lower-rate prototype requests. Production rate limits require the API key.
- Validates exact input/output mints, raw input amount, minimum output threshold, route router, route-step public keys/labels/amounts, price impact, fees, and quote identifiers.
- Preserves router and route-plan metadata for subsequent program allowlisting and semantic review.
- No order execution, signing, wallet connection, Phantom interaction, Jupiter `/execute`, or RPC transaction submission is implemented.
- Jupiter's managed `/order` + `/execute` path is the intended future integration direction. The current transaction validator still rejects lookup tables and v1 messages; transaction payload intake remains blocked until those message versions and address resolution are independently implemented and tested.
- Axiom remains an optional manual monitoring/trading terminal, not an execution dependency of this adapter.

No live trading capability is added.


### 09.10 — v0 address lookup table resolution

Implemented in the extended `veltrix/transaction_validator.py` and `veltrix/solana_rpc_simulation.py`, with adversarial coverage in the transaction-validator and RPC simulation suites.

- v0 address lookup tables are now supported only when policy explicitly enables them and the RPC adapter fetches every referenced table from the configured cluster.
- Each table must exist, be owned by the canonical Address Lookup Table program, deserialize successfully, be active (deactivation slot is `u64::MAX`), and have been extended before the observed bank slot.
- The validator reconstructs Solana's exact runtime key order: static keys, all loaded writable keys, then all loaded readonly keys. It validates lookup indexes, signer/writable privileges, duplicates, unused accounts, and the 128-account runtime lock ceiling.
- Lookup-table and token-account snapshots must share the same confirmed context slot; the simulation must use that same slot. Missing, stale, deactivated, same-slot-extended, duplicate, out-of-range, or mismatched tables fail closed.
- Semantic direct-transfer decoding can now consume the same verified resolved table set.
- Transaction v1 remains unsupported. Jupiter quote intake requests no transaction and the next order-intake layer must keep `maxSupportedTransactionVersion=0` until v1 support is separately implemented and tested.
- Tables with any non-max deactivation slot are conservatively rejected, even where Solana's deactivation cooldown might still permit use. This is an intentional false-negative safety choice.

This enables structural validation of v0 transactions that use active lookup tables; it does not yet approve Jupiter DEX swap instructions or authorize signing/execution.


### 09.11 — Jupiter v0 order transaction intake

Implemented in `veltrix/jupiter_order.py` with mocked API and transaction fixtures in `tests/test_jupiter_order.py`.

- Requests a Jupiter Swap API V2 order for an explicit taker wallet and exact-in raw amount.
- Sends `maxSupportedTransactionVersion=0` and excludes routers outside local policy. The default policy excludes JupiterZ until its maker/fee-payer signing path is separately reviewed.
- Requires response taker, input/output mints, exact input amount, output threshold, router, fees, route plan, transaction version and last-valid block height to match policy.
- Rejects gasless orders, third-party signature fee payers, unexpected/partially signed transactions, non-v0 messages, malformed base64 and mismatched fee payer. The taker must be the fee payer and required signer.
- Extracts exact v0 message bytes and preserves the original serialized transaction for audit. Active address lookup tables are preserved for the Phase 09.10 resolver.
- This is order intake only. It does not construct an approved `TransactionIntent`, decode Jupiter/DEX instruction semantics, call Jupiter `/execute`, request a Phantom signature, or broadcast.

The next required component is a Jupiter/DEX-specific semantic parser. Until that exists, the returned transaction remains untrusted and cannot pass the complete execution preflight.


### 09.12 — Jupiter DEX program registry and simulated CPI trace

Implemented in `veltrix/jupiter_program_trace.py`, with RPC trace capture in `veltrix/solana_rpc_simulation.py` and adversarial registry/gate tests.

- Fetches Jupiter's Swap API V2 `/program-id-to-label` mapping, validates every program ID and label, timestamps the snapshot, and hashes the normalized registry.
- The RPC simulation request now asks for inner instructions and resolves each inner `programIdIndex` against the same validated static+loaded account-key order. The simulation evidence records top-level and inner CPI program IDs.
- The route gate binds the exact Jupiter order message digest, taker/fee payer, recent blockhash, last-valid block height, simulation response, route labels and program trace.
- DEX and Jupiter program IDs must be locally pinned in policy. Jupiter's label registry is descriptive metadata, not a trust root; a label alone cannot authorize a program.
- Every route label must map to a locally pinned DEX program ID and that ID must appear in the simulated inner-CPI trace. Every top-level and inner program ID must be in the standard-program, pinned-Jupiter, or pinned-DEX allowlist.
- Missing inner-instruction evidence, unknown programs, stale registry snapshots, unpinned DEX IDs, and route/trace mismatches fail closed.
- This gate does not decode DEX instruction payloads, prove CPI account semantics, or verify input/output token balance deltas. It is a program-trace allowlist stage, not final swap approval. DEX-specific instruction decoders and quote-to-balance-effect checks remain mandatory.
- Program IDs still require independent review and explicit pinning before a route can pass. No signing, Jupiter `/execute`, wallet signature request, or broadcast is implemented.


### 09.13 — Jupiter quote-to-user-balance effect gate

Implemented in `veltrix/jupiter_swap_effects.py`, with classic-token program ownership preserved by the RPC simulation adapter.

- Derives the taker's classic SPL Associated Token Accounts for the order input and output mints.
- Produces slippage-aware expected deltas for SimulationGate: input ATA decreases by the exact `inAmount`; output ATA must increase by at least `otherAmountThreshold`.
- Requires exactly those two pre-existing user ATAs, with matching owner/mint and classic SPL Token program ownership.
- Requires exact message digest, taker fee payer, recent blockhash, last-valid height, RPC response identity, slot, compute and fee evidence.
- Rejects stale orders, Token-2022, missing/extra/duplicate account snapshots, wrong mint/owner, mismatched input spend, and simulated output below `otherAmountThreshold`. The simulated output may differ from `outAmount` while remaining above the on-chain minimum.
- This is deliberately conservative: it supports only swaps whose input and output effects land in pre-existing classic SPL ATAs. Input spend is exact; output is bounded by the quoted minimum, not forced to equal the quote. Native SOL wrapping/temporary WSOL accounts, ATA creation, transfer-fee tokens and Token-2022 remain unsupported.
- The effect gate does not decode DEX instructions or authorize signing. It must be combined with the pinned program trace gate, semantic instruction policy, risk preflight, and operator authorization.

No Jupiter `/execute`, Phantom signing, transaction submission, or live execution is introduced.


### 09.14 — Router scope reduced to the semantically supported Metis path

The `/order` adapter and Jupiter program-trace policy now approve **Metis only**.

- Jupiter Swap API V2 `/order` can return Metis, JupiterZ, Dflow, or OKX transactions. These are not interchangeable transaction formats or trust boundaries.
- The current downstream trace model assumes a Jupiter V6 aggregator top-level instruction plus DEX CPI evidence. It is not a valid semantic decoder for Dflow or OKX transactions, and JupiterZ has a separate RFQ/market-maker signing and expiry path.
- Order intake and the trace policy therefore reject Dflow, OKX and JupiterZ until each has its own independently reviewed instruction, signer, fee-payer, expiry, and simulation-effect decoder.
- The default allowed-router policy is now `("metis",)`; explicitly configuring another router is a configuration error, not an override.
- This narrows coverage but prevents a route label or generic program allowlist from being mistaken for semantic approval of a different router.

No transaction signing or submission is enabled.


### 09.15 — Slippage-bounded simulated output

The generic SimulationGate now supports exact deltas and explicit minimum/maximum delta bounds. Jupiter uses an exact negative input delta and a minimum positive output delta equal to `otherAmountThreshold`. This matches Solana swap semantics: `outAmount` is the quote, while `otherAmountThreshold` is the enforceable minimum. A simulated fill may differ from the quote and still be valid if it clears the minimum. Unknown/extra token-account effects remain rejected.


### 09.16 — Context-specific program allowlists

The Jupiter program-trace gate now separates top-level transaction instructions from inner CPI programs.

- Top-level instructions are limited to exactly one pinned Jupiter V6 aggregator instruction plus optional Compute Budget instructions.
- Top-level System Program, SPL Token Program and Associated Token Program instructions are rejected. This prevents a standard-program allowlist from implicitly authorizing arbitrary SOL transfers, token transfers or account creation outside the swap instruction.
- Inner CPI programs are limited to locally pinned DEX programs and the classic SPL Token Program. System Program, Associated Token Program and Compute Budget are not blanket-approved inside CPI.
- The policy rejects any attempt to add System/ATA or other program IDs to the generic standard-program configuration.
- The transaction must contain exactly one approved Jupiter aggregator instruction.

This is a stricter allowlist boundary, not a substitute for decoding Compute Budget instruction payloads or DEX account layouts. If a future supported route requires additional programs, it must receive an explicit program- and instruction-specific decoder rather than broadening the generic allowlist.


### 09.17 — Compute Budget instruction decoder

Implemented in `veltrix/compute_budget.py` with adversarial tests in `tests/test_compute_budget.py`.

- Decodes only Solana Compute Budget `SetComputeUnitLimit` (tag 2, exact u32 payload) and `SetComputeUnitPrice` (tag 3, exact u64 payload).
- Rejects deprecated `RequestUnits`, heap-frame requests, loaded-account-size overrides, unknown discriminators, malformed lengths, account metas and duplicate limit/price instructions.
- Enforces a 300,000 CU default request ceiling, a 10,000,000 micro-lamport/CU price ceiling and a 5,000,000 lamport estimated-priority-fee ceiling. Policy can be tightened further.
- If no explicit limit is present, estimates priority fees using Solana's 200,000 CU default for this single-swap instruction shape.
- Binds decoding to the exact message SHA-256, structural validator's static/resolved account-key order, top-level program IDs and instruction count.
- This decoder is currently a separate validation component; the complete swap orchestrator must call it before any future signer boundary is considered.

No signing, transaction submission or live execution is added.


### 09.18 — Compute Budget integrated into Jupiter route preflight

`evaluate_jupiter_route_programs` now invokes `validate_compute_budget_instructions` against the exact order message and structural validation result. A route-program gate cannot pass if the Compute Budget instruction payloads are malformed, duplicated, or exceed configured CU, micro-lamport price, or estimated priority-fee limits. The gate result carries the decoded compute-budget values for audit. Tests now use actual serialized v0 message fixtures rather than arbitrary message-byte placeholders.

The DEX route instruction payload/account semantic decoder remains outstanding; this integration does not authorize signing.


### 09.19 — Jupiter V6 route payload and fixed-account inspector

Implemented in `veltrix/jupiter_v6_route.py` with adversarial route fixtures in `tests/test_jupiter_v6_route.py`.

- Recognizes only the Jupiter V6 exact-in `route` and `sharedAccountsRoute` Anchor discriminators; rejects other Jupiter instruction variants.
- Decodes the Borsh route-plan vector and a reviewed subset of Jupiter V6 Swap enum variants (Raydium, Orca Whirlpool, Invariant, Meteora, Raydium CLMM, Meteora DLMM, Raydium CLMM V2 and Raydium CPMM). Unknown enum variants fail closed.
- Binds each on-chain DEX enum, percent/bps split and topological token indexes to the Jupiter order routePlan.
- Binds the on-chain input amount, quoted output amount, slippage BPS and zero platform-fee policy to the exact order.
- Validates the fixed classic-SPL Token, taker, input/output ATA, mint, shared-route authority PDA, event-authority PDA and Jupiter program account roles.
- Requires each quoted AMM key to appear in the instruction's trailing remaining accounts.
- Explicitly returns `fully_decoded=false` when DEX-specific remaining account roles are present. Pool/vault/tick-array and DEX-specific writable/read-only account layouts remain a release blocker; this inspector is not a signer authorization.

No signing, `/execute`, broadcast or live execution is introduced.
