# Phase 09 — Live Execution Safety Gate

## Status

SAFETY GATES IMPLEMENTED; RELEASE BLOCKED. Live execution is disabled. This phase does not authorize wallet signing, swap submission, token approvals, or any real-money transaction.

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


### 09.2 — deterministic pre-sign safety gates

Implemented in `veltrix/execution_safety.py`.

- `TransactionValidator` validates a canonical unsigned instruction/effect manifest against the exact intent, policy program allowlist, account allowlist, input/output mints and amounts, minimum output, slippage, priority fee, recent-blockhash lifetime, instruction set, authority/delegate invariants and wallet balance deltas.
- This validator consumes a normalized manifest; it is **not** a raw serialized Solana transaction decoder. A separately reviewed raw-message decoder is still required before a signer can ever be considered.
- `RiskGate` requires a future explicit `LIVE_RISK_APPROVED` state, zero missing claims/flags, fresh risk/market/RPC evidence and integer-base-unit exposure/daily-loss limits. The current Stage 05 evidence collector does not emit this approval state, so current production evidence cannot pass this gate.
- `SimulationGate` binds a successful, fresh simulation to the exact intent and message hash, and checks slot, account-state hash, output and fee parity.
- `ExecutionSafetyPipeline` orders the checks, consumes the one-use operator authorization only after transaction/risk/simulation gates pass, and records state transitions. It returns `AUTHORIZED` only as a preparation record with `live_execution_enabled=false` and reason `signing_adapter_not_implemented`.

### 09.3 — persistent stop, idempotency and audit

Implemented in `veltrix/emergency_stop.py` and `veltrix/submission_coordinator.py`.

- Emergency stop is active by default, requires an explicit operator clear, persists across restarts and fails closed if its storage cannot be read. State changes have a separate append-only event table.
- Intent creation is idempotent by stable intent ID and immutable intent/policy hashes. State transitions use `BEGIN IMMEDIATE` and compare-and-set semantics.
- The coordinator permits preparation states through `AUTHORIZED` and terminal `FAILED/CANCELLED`; `SIGNED`, `SUBMITTED` and `CONFIRMED` transitions are explicitly disabled.
- Execution audit records are append-only and limited to intent IDs, state/reason codes, timestamps and cryptographic hashes. There are no generic metadata fields that could persist a secret.
- `SignerBoundary` is a protocol only. No concrete signer, key loader, RPC submitter, wallet adapter or real transaction path is present.

### Current Stage 09 boundary

The Stage 09 implementation is a safety-gate kernel, not a live trading release. Runtime environment construction still resolves to PAPER and rejects truthy or malformed `LIVE_TRADING`. The live-risk approval state is not produced by the existing risk pipeline. The raw Solana message decoder, signer adapter, submission adapter and settlement reconciliation are intentionally not implemented. Existing Phase 09 Raydium/Devnet draft branches remain isolated and unmerged.

See `docs/phase-09-operator-checklist.md` for the future manual safety checklist. It does not request or require the user to expose any wallet secret.


## Safety-kernel acceptance status

- [x] Runtime policy defaults to PAPER; truthy or malformed LIVE_TRADING configuration is rejected.
- [x] One-use authorization is bound to wallet, chain, policy hash, intent ID and short expiry, with durable atomic replay protection.
- [x] Emergency stop is active by default, independently persisted, restart-safe, audited and fail-closed on read errors.
- [x] Risk gate enforces evidence state, missing-claim/flag checks, freshness and integer-base-unit exposure/daily-loss limits.
- [x] Normalized transaction manifest validator enforces program/account allowlists, exact intent-to-message-hash binding, mint/spend/output/slippage/fee limits and authority/delegate invariants.
- [x] Simulation gate binds successful simulation to the exact manifest hash and checks freshness, slot, account-state hash, output and fee parity.
- [x] Submission coordinator enforces durable intent idempotency, compare-and-set transitions and terminal-state protection; SIGNED/SUBMITTED/CONFIRMED are blocked.
- [x] Execution audit is append-only, redacted and rejects fields outside its allowlist; static scans reject key-import/signing/RPC submission adapters.
- [x] Adversarial tests cover gate rejection, emergency-stop restart/storage failure, authorization replay, concurrent intent creation and zero fake-signer calls.
- [x] Full repository CI passes on exact PR head `821881af1e21e054b563db18f16735a826d3a9a3` (two successful runs; 905 passed, 5 skipped; compileall passed).
- [ ] Post-merge CI passes on main.
- [ ] Independent security review signs off the exact diff and threat model.

The safety kernel can prepare an auditable AUTHORIZED record but cannot sign or submit. Structural raw legacy/v0 message decoding and ALT resolution are now implemented in the Stage 09.4 workstream, but program-specific semantic instruction decoding, account-effect reconstruction, and integration with the safety validator remain incomplete. A real signer, RPC submission adapter and settlement reconciler remain absent. Therefore Stage 09 safety controls are implemented in part, but live execution and final release are not complete or enabled.


### 09.4 — raw unsigned Solana message decoding (implementation in review)

Added `veltrix/solana_message_decoder.py` and focused adversarial coverage in
`tests/test_solana_message_decoder.py`.

- Parses the serialized transaction signature vector and legacy/v0 message wire format directly; does not accept a caller-normalized instruction manifest as its source of truth.
- Requires signature-slot count to match the message header and every signature slot to remain zero. Already-signed transactions are rejected.
- Enforces canonical compact-u16 lengths, bounded transaction/instruction/lookup counts, exact message consumption, valid signer/read-only header relationships, static fee-payer presence, and in-range compiled instruction indices.
- Reconstructs v0 account ordering as static keys, all loaded writable addresses, then all loaded readonly addresses. Program IDs must resolve to static keys.
- Resolves each ALT only from a caller-supplied raw account snapshot whose address matches the message key and whose owner is the canonical Address Lookup Table program. It parses the 56-byte metadata prefix, rejects uninitialized/deactivated tables, out-of-range/duplicate indices, snapshots preceding the last extension, and same-slot addresses that have not completed warm-up.
- Commits to exact message bytes and full transaction bytes with SHA-256; exposes ordered instruction program IDs, account indices/keys, raw data and per-instruction data hashes.
- No RPC fetcher, signer, submitter, broadcaster, token-program decoder, swap-program decoder, or account-effect inference is included in this module.

This is a structural wire decoder and ALT resolver, not a complete semantic transaction approval. Program-specific instruction decoding, account-state provenance verification, exact effect reconstruction, and integration with `TransactionValidator` remain separate acceptance gates. Exact-head GitHub Actions run `43544982357` passed: `compileall` succeeded and pytest reported **929 passed, 5 skipped**. The five skipped tests are the repository's live-provider smoke tests; no external live-provider evidence was produced.



### 09.5 — semantic transaction binding audit (in progress)

Repository inspection on `work/phase-9-raw-message-decoder` established the following implementation boundary:

- The current order path is `JupiterSwapV2OrderAdapter` in `veltrix/jupiter_order.py`. Its configured preflight permits only the Jupiter `metis` router; alternate Jupiter routers are explicitly withheld pending independent semantic decoders.
- The adapter validates the returned unsigned v0 envelope, taker/fee-payer, mint pair, amounts, fee fields and Jupiter route-plan metadata. The route plan is provider metadata; it is not a decoded proof of the compiled instruction semantics or resulting token-account mutations.
- `veltrix/providers/solana_liquidity_reader.py` identifies Raydium AMM V4 (`675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8`) and Raydium CPMM (`CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C`) for pool inventory. That reader's pool-state layouts do not constitute swap-instruction decoders. Its UNCX lock reader is not a swap adapter.
- Classic SPL Token and Token-2022 program IDs appear in the liquidity reader for token-account inventory/custody checks. Their presence does not establish that execution instructions are semantically decoded.
- No repository implementation was found that decodes the exact Jupiter/DEX compiled swap instructions and independently reconstructs their token-account, authority, pool-vault, fee and output effects from verified account state. The supported transaction-program set is therefore not yet certified for execution.

**Critical hash-domain distinction:**

- `ExecutionIntent.expected_message_hash` is currently compared by `TransactionValidator` to `UnsignedTransactionPlan.message_hash`.
- `UnsignedTransactionPlan.computed_message_hash()` hashes the canonical JSON payload of the normalized plan (instructions and caller-supplied effects). It is not the SHA-256 of serialized Solana message bytes.
- The Stage 09.4 decoder separately computes `DecodedSolanaTransaction.message_sha256` from the exact raw message bytes. That value is not yet an authorization field checked by `TransactionValidator`.
- Do not rename or silently repurpose the existing manifest hash. Add a distinct intent-bound raw-message commitment and verify it against the exact decoded bytes. The normalized manifest commitment must remain a separate domain.

**09.5 implementation progress — separate commitment field added (not yet decoder-integrated):**

- `ExecutionIntent.expected_raw_message_sha256` is now a required, validated field included in the canonical intent payload and therefore in the operator-authorization intent hash.
- `UnsignedTransactionPlan.raw_message_sha256` is a separate field from `message_hash`; the raw digest is included in the canonical manifest payload, while `message_hash` retains its existing normalized-manifest meaning.
- `TransactionValidator` rejects a plan whose raw digest differs from the intent-bound raw digest.
- `SimulationReceipt` now carries the raw-message digest separately, and `SimulationGate` requires it to match the plan.
- Adversarial tests cover raw-digest substitution independently from normalized-manifest hash substitution.

**Important limitation:** this change establishes a separate commitment domain, but the plan field is still supplied to the validator; the current pipeline does not yet decode transaction bytes internally and prove that the supplied digest equals `DecodedSolanaTransaction.message_sha256`. The next implementation must make the pipeline accept exact unsigned transaction bytes and ALT snapshots, decode them internally, and compare the resulting message digest and ordered instruction data against the plan. Until that is implemented and tested, this is not semantic transaction approval and must not be used to authorize signing.

**Required implementation order before any signer work:**

1. Extend the authorized intent contract to bind the exact raw message SHA-256 independently from the canonical manifest SHA-256; update canonical serialization, authorization and persistence tests.
2. Build a strict semantic decoder registry. Unknown program IDs, instruction discriminators, account layouts and unsupported route variants must reject.
3. Derive the normalized instruction plan and account effects only from decoded bytes plus independently verified, same-context account snapshots. Never accept caller-provided effects as authoritative.
4. Bind the decoder output, exact raw message hash, ordered program/account keys and account-state commitments to the intent; then pass the derived plan through the existing `TransactionValidator` without weakening any existing checks.
5. Add adversarial tests for wrong mint/vault/pool, amount and destination mismatches, authority/delegate changes, unexpected token transfers, fee/slippage mismatch, account aliasing, stale or inconsistent state, unknown programs and unsupported instructions.
6. Run exact-head CI and obtain independent review of the semantic decoders and threat model.

**Release status:** Stage 09.5 is not complete. No production-ready semantic swap decoder or authoritative account-effect reconstruction is present. The current validator's normalized-manifest hash must not be mistaken for a raw-message commitment. Phantom signing, transaction submission, broadcasting and Mainnet execution remain disabled.
