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


### 09.2 — Wallet domain isolation

Implemented in `veltrix/wallet_isolation.py` with focused tests in `tests/test_wallet_isolation.py`.

- `PaperLedgerIdentity` is a synthetic ledger/account reference and is a distinct type from a blockchain wallet identity.
- `PaperExecutionContext` accepts only a `PaperLedgerIdentity`; it cannot be initialized with a live wallet identity.
- `ReadOnlyWalletContext` accepts only a public `WalletIdentity` and an immutable allowlist of read-only capabilities (`balance`, `transactions`).
- Write, signing, submission, and mutable capabilities are rejected.
- The module contains no private-key/seed fields, signer implementation, provider connection, transaction mutation, or submission code.
- The contexts intentionally expose no conversion path between paper and live wallet domains.

This is a type-level isolation boundary. It does not prove operating-system, process, database, or provider isolation; those require deployment-level controls and later integration tests. Phase 09.2 remains in progress until those controls are specified and independently verified.


### 09.2 deployment/process isolation controls — templates added

Deployment guidance and hardened systemd templates are in
`docs/phase-09-process-isolation.md` and `deployment/systemd/`.

- Paper runtime and dashboard use distinct non-login service identities.
- Paper runtime is restricted to the shared pipeline state directory; its
  environment file is separated from dashboard credentials.
- Dashboard is loopback-bound, authenticated, GET-only, and receives a
  read-only mount of the shared SQLite directory in addition to SQLite
  `mode=ro` and `PRAGMA query_only=ON`.
- Paper and dashboard credentials use separate supplementary groups.
- The future wallet observer is specified as a separate identity with no
  paper-database access and read-only RPC egress. It is not implemented.
- Provider egress is explicitly a host/cloud firewall responsibility; systemd
  address-family restrictions are not treated as destination allowlists.

The unit templates are not installed on a production host. Host-level UID,
filesystem, SQLite WAL/SHM, provider-egress and restart-isolation verification
remains mandatory before Phase 09.2 can be marked complete. CI checks the
presence of the deployment contract; it cannot attest to a real host.


## Post-retarget validation rule

A successful workflow run from before a pull request is retargeted does not count as validation against the new base. Require a fresh full CI run on the exact PR head after retargeting, and re-check merge-base and changed-file scope before considering integration. This repository-side CI gate does not replace the separate host-level process, database/WAL/SHM permission, and provider-egress verification described above.


### 09.3 — Signer boundary contract

Contract-only interface added in `veltrix/signer_boundary.py`, with tests in
`tests/test_signer_boundary.py`.

- Defines immutable unsigned and signed transaction envelopes.
- The unsigned envelope binds an intent, chain, network, signer public key,
  exact serialized message digest, and policy hash.
- The protocol accepts one exact unsigned envelope and returns an opaque signed
  envelope carrying the same intent, signer identity, chain, network, policy
  hash and message digest. Its contract explicitly forbids broadcasting.
- No concrete signer, key loading, key storage, signing algorithm, wallet
  provider, transaction submission, or broadcast implementation is included.
- Tests validate immutability and malformed envelope rejection without invoking
  a signer or contacting a network.

This is an interface milestone only. It does not establish signer isolation,
transaction validity, cryptographic correctness, or production readiness.
A future implementation must run in a separately isolated service, independently
verify the approved message and policy, and undergo security review. Live
execution remains disabled.
