# VELTRIX Multi-Agent Crypto — Project Progress Ledger

**Last audited:** 2026-11-15 (UTC)  
**Repository:** `Nexora-Apparel/veltrix-multi-agent-crypto`  
**Default branch:** `main`  
**Audited main baseline:** `6158a5995c7f241a7c1ba56dc20161c3fd7e4fa2`  
**Document status:** Initial repository-wide baseline; update after every completed project task.

> This ledger distinguishes merged implementation, unmerged branch work, automated test results, live/provider evidence, and release authorization. A green unit-test suite is not production acceptance. An open PR is not a completed stage.

## 1. Executive status

VELTRIX is a Solana-first, multi-chain token-intelligence and paper-trading research system covering Solana, Base and BSC. Its staged architecture is discovery → market intelligence → security evidence → deterministic multi-agent consensus → paper trading → read-only dashboard → execution safety.

**Current accepted baseline:** Stages 1–8 and Stage 09.1 are merged into `main`. The core paper/research stack and read-only dashboard exist. Stage 09.2 isolation code is CI-verified in draft PR #191 but still lacks real-host and provider-egress acceptance. Stage 09.3 has a typed contract-only draft PR #192 stacked on #191. Stage 09.4/09.5 transaction verification remains partial in #181/#182. Dashboard and Phantom integration remain unmerged. A separate Stage 5 evidence-provider certification extension is also under review.

**Release state:** no live trading or mainnet execution is authorized. The core execution policy remains paper-only; signing/submission/broadcast must remain disabled except for the explicitly validated Devnet sign-only test path described below. No production deployment, scheduled worker, Telegram delivery or production data migration is certified by this audit.

### High-level stage matrix

| Stage | Scope | Repository status | What is still needed |
|---|---|---|---|
| 1 | Architecture and foundation | **Complete — merged** | No stage exit work identified |
| 2 | Candidate validation | **Complete — merged** | No stage exit work identified |
| 3 | Solana discovery | **Complete — merged** | Operational deployment/continuous feed not activated |
| 4 | Token intelligence | **Complete — merged** | Production source/coverage monitoring remains operational work |
| 5 | Security and risk engine | **Core implementation complete — merged** | Evidence-provider capability certification remains incomplete; see §4 |
| 6 | Evidence-gated multi-agent consensus | **Complete — merged** | Whale remains abstention-only until trusted wallet feed and allowlist are configured |
| 7 | Paper trading | **Complete — merged** | Runtime is not certified as continuously active in production |
| 8 | Read-only dashboard and analytics | **Complete — merged** | Mobile redesign PR #183 exact-head CI passes; Android visual QA remains outstanding |
| 09.1 | Fail-closed policy and operator authorization | **Complete — merged (PR #9)** | No live authorization |
| 09.2 | Paper/dashboard process and wallet-observation isolation | **Implementation under review — PR #191 open; host acceptance incomplete** | Exact-head CI plus real Linux host, systemd, UID/GID, mount, DAC, DB/WAL/SHM and egress evidence |
| 09.3 | Isolated signer boundary | **Typed contract-only draft PR #192 stacked on #191; CI verified** | Cannot merge before 09.2 host acceptance; metadata binding is not signed-byte or cryptographic verification; no signer exists |
| 09.4–09.5 | Transaction validation, simulation and semantics | **Partial — PRs #25/#26/#181/#182 and related branches** | Full semantic decoder/effect reconstruction, account-state provenance, independent review |
| Devnet integration | Phantom + direct CPMM sign-only test | **Sign-only validation reported successful — PR #184 open** | No submission; resolve/record prior ALT-path KMS failure separately; release remains locked |
| Stage 5 provider certification extension | Solana/Base/BSC liquidity, developer and launch evidence | **Incomplete — PRs #187/#189/#190 open** | Nine live read-only acceptance artifacts, registry/provider review and independent certification |


## 1.1 Repository inventory and workflow audit

A recursive Git tree was retrieved from the repository after the ledger commit. It was complete (GitHub reported `truncated=false`) and contained **213 tracked paths** at that tree:
- 92 paths under `veltrix/` (including package directories and source modules);
- 86 test files under `tests/`;
- 12 documents under `docs/` (including this progress ledger);
- 16 GitHub Actions workflow files under `.github/workflows/`;
- root configuration, README and dependency files.

The source tree contains the following major areas:
- Core policy/foundation, agents, composite safety, provider capability/readiness, developer concentration/evidence, launch intelligence/origin/bundling, liquidity evidence and monitoring.
- Provider adapters for DexScreener, Helius, Alchemy, GoPlus, signed developer roots, holder inventory and EVM/Solana liquidity readers.
- Solana discovery, enrichment, market refresh, risk evidence, consensus, paper runtime and dashboard modules.
- Jupiter quote/order preflight and Solana Devnet wallet preflight.
- 86 tests covering the core pipeline, data provenance, provider adapters, safety gates, dashboard, paper ledger and preflight workflows.

### Workflow trigger findings

The main-branch Python CI workflow runs on pushes to `main` and `work/**`, pull requests and manual dispatch. It runs compileall and pytest. Its three live Solana smoke steps are branch-scoped and can be skipped; skipped provider tests do not count as live acceptance.

The main branch also contains manual or narrowly triggered workflows for provider acceptance, liquidity acceptance, Raydium pool validation, Jupiter quote/unsigned-order preflight, Devnet wallet preflight, and Turso conformance. The liquidity workflow supports manual chain selection and can also run on selected file-path pushes. The Turso H3 workflow is manually dispatchable and path-triggered on main; it is not a recurring schedule.

**No cron/scheduled trigger was found in the 16 main-branch workflow files inspected.** This is a repository workflow finding, not proof that no external scheduler exists. No repository secret/variable inventory was available through the connected GitHub tools, so configured credential presence remains **not checked**.

The main-branch tree audit does not include unmerged files from PR branches. In particular, the Stage 5 developer/launch acceptance workflows in PR #187/#189 are not yet part of main.

## 2. Confirmed completed work

The following stages are marked complete because their implementation PRs are merged and their stage documents record exit criteria and post-merge validation.

### Stage 1 — architecture and foundation
- PR #170 merged to `main`.
- Architecture, service boundaries, trust model, canonical identity/evidence contracts, persistence rules, secret handling and release controls documented.
- Runtime rejects production, Telegram, scheduling and live-trading activation.
- Existing repository infrastructure, database paths, historical data and credentials were preserved.
- Stage document records 783 passed / 5 skipped and successful integrated main CI.

### Stage 2 — candidate validation
- PR #2 and hardening PR #171 merged; the later hardening adds reconstruction-based validation at ingestion, deduplication and SQLite persistence.
- Canonical Solana mint identity, chain identity, source provenance and timestamps are checked.
- Conflicting mint aliases and forged/mutated candidate objects are rejected.
- Invalid rows are audited/skipped without discarding valid peers.
- Stage document records 792 passed / 5 skipped and successful post-merge CI.

### Stage 3 — discovery
- PR #3, hardening PR #172 and worker-construction acceptance PR #178 merged.
- PumpPortal new-token stream and DEX Screener latest profile/boost feeds are read-only discovery sources.
- Paid trade streams are excluded; bounded polling, reconnect/backoff, provenance, deduplication and error redaction are implemented.
- Stage document records live read-only provider smoke and worker-construction checks, plus post-merge CI (852 passed / 5 skipped).
- This does not mean a permanent continuous production worker or schedule is running.

### Stage 4 — token intelligence
- PR #4 and hardening PR #173 merged.
- DEX Screener token-pair enrichment, batched requests, market snapshot normalization, explicit data-quality flags, append-only persistence, refresh leases and bounded retries are implemented.
- Missing values remain unknown rather than being silently treated as zero; pool liquidity/volume are not improperly aggregated.
- Stage document records a read-only BONK market-data smoke and post-merge CI.

### Stage 5 — security and risk core
- PR #5, hardening PR #174 and reconciliation corrections #176/#177 merged.
- Finalized Solana mint inspection, holder inventory, supply/decimal reconciliation, risk evidence persistence and strict unknown/missing-claim behavior are implemented.
- No generic SAFE verdict is emitted. Holder concentration is withheld unless coverage and supply/decimal reconciliation are complete.
- GoPlus is supplemental context only; its response alone does not certify complete liquidity locks or developer holdings.
- Historical evidence remains readable; legacy records without required proof are not accepted as current review-ready evidence.

### Stage 6 — deterministic multi-agent consensus
- PR #6, hardening PR #175 and worker construction/compatibility fixes merged.
- Scout, Whale, Sniper and Shield produce deterministic typed votes.
- Shield has veto authority; evidence, votes and decision identity are revalidated at persistence.
- No generative LLM can invent evidence or override a security veto.
- Whale abstains without a trusted provider and operator-approved wallet allowlist.
- Stage document records post-merge CI (847 passed / 5 skipped).

### Stage 7 — paper trading
- PR #7 and restart/idempotency acceptance PR #179 merged.
- Consensus-gated virtual entries, atomic SQLite decision consumption, durable positions/fills, modeled fees/slippage, position/exposure limits, stop logic and 2x/3x partial exits are implemented.
- Restart/reconciliation tests prove that a consumed decision does not open a duplicate position.
- This is simulated execution only; it does not connect to an exchange, wallet signer or transaction broadcaster.
- Stage document records post-merge CI (853 passed / 5 skipped).

### Stage 8 — dashboard and analytics
- PR #8 and dashboard correctness hardening PR #180 merged.
- Read-only dashboard and GET-only API expose account/equity/P&L, positions, fills, consensus outcomes, market snapshots, pipeline freshness and modeled costs.
- SQLite uses read-only mode and query-only enforcement; remote binding requires a strong bearer token.
- Quality-flagged/stale market data is not used as a current position mark.
- Stage document records post-merge CI (858 passed / 5 skipped).

### Stage 09.1 — policy and operator authorization
- PR #9 merged.
- Immutable policy snapshots, deterministic policy hashes, integer base-unit limits, short-lived HMAC authorization claims, scope checks, expiry and durable one-use replay protection are implemented.
- Runtime remains PAPER-only. This did not add wallet access, a signer, transaction submission or production live authorization.

## 3. Stage 09 execution-safety work — current branch/PR state

### PR #10 / #12 / #23 / #191 — Stage 09.2 host isolation
- PR #10 remains open/draft on `work/phase-9.2-wallet-isolation`; PR #12 was merged into that branch, not into `main`. No host was provisioned/tested in that original chain.
- PR #23's clean integration head `0125749e25d85fe7145b58982987864f60f00ec3` was 52 commits behind and diverged from current `main`; do not merge it.
- PR #191 replaces #23 and was created directly from the then-current main baseline. Current PR head: `76a9b54131c1c67b29ba533e4c7758b8c20b42b0`; draft/open; exact-head CI run `43767513662`: **886 passed / 5 skipped**. Compileall, shell syntax and `systemd-analyze verify` on temporary CI-adjusted copies passed. Subsequent progress-ledger commits on main leave the PR branch diverged and behind current main; synchronize/rebase, inspect the resulting diff and rerun exact-head CI before any integration.
- PR #191 adds separate paper/dashboard/observer identities, hardened systemd templates, host preflight and verifier, runbook, read-only wallet-domain types and mandatory dashboard bearer authentication on loopback and remote binds.
- The verifier was strengthened to reject symlinked database directories/files, environment files and WAL/SHM sidecars; require exact `veltrix-paper:veltrix-db` ownership and directory/database/sidecar modes; and independently test paper DAC write access and dashboard DAC non-write access outside the dashboard mount namespace.
- **Hard blocker:** no actual Linux/systemd host has been provisioned or verified. Installed-unit status, process UID/GID, mount namespace, real SQLite/WAL/SHM permissions and provider-egress firewall evidence are absent. Green CI validates scripts/templates only; it is not host acceptance. Keep #191 draft.

### Hosting alternative — PRs #13–#22
- PR #13 proposes a no-card/$0 architecture using GitHub Actions standard hosted Linux runners, Turso Free and Cloudflare Pages/Workers.
- PRs #14–#18 cover a disposable Turso DB-API probe, adapter, bounded orchestration and fencing hardening.
- PRs #19–#22 specify/integrate a fenced one-shot pipeline. PR #22 remains open/draft.
- Remote Turso conformance is independently verified from Actions run `42912477053` on `main`, commit `2d2618e` (full SHA `2d26187e5be3192cad0fda6c062e5f2bfeb034e5`): the dedicated disposable database identity check passed and all five DB-API/schema/orchestration/worker/pipeline conformance suites passed (**5 passed in 325.57s**). A separate successful run `42964334567` also exercised the integrated H1.1/H2/H3 branch at `dbdbabf18deece50e4f76075b0c0aec26f3de6a1`. This verifies the disposable Turso conformance path, not a production migration or deployment.
- No production database migration or Cloudflare production deployment is established. SQLite remains the documented default/rollback source. The $0/no-card constraint remains a project requirement.

### Stage 09.3 — signer boundary
- PR #11 and PR #24 are historical contract branches based on an old main baseline.
- PR #192 is the current dependent draft, based on PR #191's isolation branch; current head `8b2cef7eb57ba22bcd9a307ba51495c45821182d`. Exact-head CI run `43767800370`: **919 passed / 5 skipped**; compileall, shell syntax and systemd template verification passed.
- The contract uses immutable unsigned/signed envelopes, typed `Chain`/`Network` values, canonical policy hashes and metadata binding for intent, signer public key, chain/network, policy hash and exact unsigned-message digest.
- **Critical limitation:** metadata equality does not prove the opaque signed transaction contains the approved message or that the signature is cryptographically valid. A chain-specific signed-byte decoder and cryptographic signature verifier remain mandatory.
- No concrete signer, key loader/storage, signing algorithm, signature verifier, wallet adapter, submission or broadcaster exists. PR #192 must not merge before PR #191 host acceptance and independent review.

### Stage 09.4/09.5 — transaction validation and simulation
- Earlier standalone PRs #25 and #26 remain open/draft.
- The newer PR #181 adds the safety kernel: persistent emergency stop, risk gate, normalized unsigned transaction manifest validation, simulation receipt binding, idempotent intent coordinator, audit chain and one-use operator authorization.
- PR #182 adds a raw Solana legacy/v0 unsigned message decoder, structural ALT validation, raw-message hashing, same-context account snapshot acquisition, Jupiter V6 outer instruction parser and classic SPL Token parse-only decoder.
- PR #182 current head `b08b7645ee44ea4c45e7992111de2c0226fc611f` has successful Python CI: **979 passed / 5 skipped** (run `43548384099`).
- Structural parsing is not semantic swap approval. The Jupiter decoder is outer-instruction-only; the SPL parser is parse-only. The documented remaining requirements include invoked DEX/token instruction semantics, CPI provenance, authoritative account-effect reconstruction from same-context state, ALT snapshot provenance, complete transaction-state reconciliation and independent security review.
- PR #181's prior head `7a59161dbc78ca7cd2361f9814f060283f07e5ef` failed run `43521504346` (905 passed, 1 failed, 5 skipped) because the test assumed an intent-local predecessor while the audit chain is global. Corrected head `f984e1b1611b49d766d2eb2467683fb5d97f3d4d` passed exact-head CI run `43766530934`: **906 passed, 5 skipped**; compileall passed. The regression test checks the predecessor against the immediately preceding global event and retains offline-tamper detection. Five skipped checks are live-provider smoke tests.
- PR #181 also explicitly states that Stage 05 does not yet emit `LIVE_RISK_APPROVED`, the pipeline stops at `AUTHORIZED`, and `SIGNED`, `SUBMITTED` and `CONFIRMED` are blocked.

## 4. Stage 5 provider evidence certification extension

The original Stage 5 security/risk engine is merged. PRs #187–#190 extend it with stricter production evidence acquisition and certification across Solana, Base and BSC. This is a separate acceptance track, not evidence that Stage 5's original merge was undone.

### Current PR chain
1. **#187** — Solana and BSC developer funding graph adapters; Helius ancestry and Etherscan API V2 BSC ancestry; signed developer-root attestation and live acceptance.
   - Current head: `ea4df866dcb12a648490017f2ceedae57a3a15ff`
   - CI run `43724847113`: **809 passed / 5 skipped**.
2. **#189** — Raydium Burn & Earn CPMM permanent LP lock evidence, developer/launch acceptance workflows, nine-artifact aggregator and runbook.
   - Current head: `bd2a0b4c9a88003a620ec7de094f0b6562ccfcbf`
   - CI run `43726413685`: **899 passed / 5 skipped**.
3. **#190** — requires every one of the nine acceptance artifacts to explicitly attest `live_execution_enabled: false`; aggregator fails closed if any attestation is missing.
   - Current head: `a0d35dfa99221791f4762cc310ca852ef285cc0b`
   - CI run `43752432412`: **900 passed / 5 skipped**.

All three are open, draft and unmerged. PR #188 is a closed, unmerged earlier CPMM implementation attempt; #189 is the rebased continuation. PR #186 is a separate open deployment-block wiring correction, but its exact patches to `veltrix/monitor.py` and `veltrix/developer_evidence.py` are already present identically in PR #187. PR #187 also includes the associated developer-evidence tests. Treat #186 as superseded/redundant; do not merge it separately. Close it as superseded after confirming no other unique change is needed.

### Certification gate still outstanding
The runbook requires nine production read-only acceptance artifacts:

| Evidence family | Solana | Base | BSC |
|---|---|---|---|
| Liquidity lock | Required | Required | Required |
| Developer holdings/funding | Required | Required | Required |
| Launch bundle | Required | Required | Required |

- **Current evidence record: 0/9 verified.** Actions history was inspected, not merely PR descriptions. Liquidity workflow runs on the Stage 5 branches either skipped the actual finalized-state reader because required registries/RPC configuration/sample tokens were incomplete, or failed the preflight. For example, runs `43724610264` and `43725136999` concluded successfully at the job level but all three chain readers were skipped and no evidence artifact was uploaded. Latest run `43752387729` failed all three chain preflights; the actual readers were skipped and no artifacts were produced. A green workflow conclusion with a skipped reader is not acceptance. No developer/launch-bundle manual acceptance run was found in the 51 manual-dispatch runs returned by the Actions history query.
- Solana liquidity acceptance must cover both UNCX Raydium AMM V4 and Raydium Burn & Earn CPMM. The exact deployed program hash and complete registry must be operator-pinned; no hash should be guessed.
- Base and BSC require two independent finalized RPCs and complete factory/locker registries.
- Developer evidence requires complete holder inventory, funding ancestry and independently reviewed signed root-identity methodology. A connector existing in code does not certify its data.
- Launch-bundle evidence requires complete historical launch-window coverage, source identity, supply provenance and trusted funding evidence.
- The nine-artifact aggregator must not promote trust, capability flags or execution permission.
- Capability flags must remain false until all required chains pass and independent review is recorded.

The Actions run-history API was queried (6,578 total runs reported; the latest 100 plus the manual-dispatch run set were inspected), and the relevant Stage 5 run jobs/logs/artifact lists were checked. The GitHub connector does not expose repository secret/variable inventory. Workflow logs establish that specific required configuration was missing or incomplete at the time of those runs, but this audit does not infer the current state of every repository secret. Secret values must never be copied into this ledger.

## 5. Phantom / Devnet integration track

### PR #184 — current status
- Open, non-draft; current head `c7fe1ca22594957d8447f04d923d35e0ac7bdc85`.
- CI run `43720719285`: **809 passed / 5 skipped**.
- Read-only prior-signature reconciliation run `43720719274`: success.
- Raydium Devnet swap preflight run `43720719281`: success.
- PR description reports the Phantom Android legacy `signTransaction` sign-only test passed, the returned signature verified locally, and the reviewed message semantics remained intact.
- PR description also reports an unsigned 0.005 SOL direct CPMM transaction built and simulated successfully.
- No transaction was submitted, broadcast or executed. `EXECUTION_RELEASE_ENABLED=false`; the execution handler remains hard-disabled.

### Earlier Android diagnostic
The uploaded 12 November Phantom diagnostic log recorded an ALT-backed transaction being rejected by Phantom KMS because loaded account data was not provided while a lookup table was used. The latest PR #184 path uses a legacy, ALT-free transaction and reports a successful sign-only test. These are different paths and should not be conflated. The earlier ALT/KMS error is not evidence of a successful transaction submission.

## 6. Dashboard redesign track

### PR #183
- Open, non-draft; current PR head: `7fa969d7460d27eb06b7072454ed3d3c9e4d147c` (base: `work/phase-9-raw-message-decoder`).
- Mobile-first interface changes: Home, Trades, Agents and Wallet navigation, separate Search control, dedicated screens and read-only account/position/fill/market/consensus data.
- Signing, transfers, submission and live execution remain unavailable.
- Previous head `6d7cc1a348ae66c89ecf8dd7ba580380a4968cd2` failed run `43598754552` (978 passed, 1 failed, 5 skipped) because the test referenced `dashboard_js` before assignment. Corrected head `7fa969d7460d27eb06b7072454ed3d3c9e4d147c` passed exact-head CI run `43766534202`: **979 passed, 5 skipped**; compileall passed. The authenticated JavaScript asset is retrieved before assertions inspect it. Five skipped checks are live-provider smoke tests.
- The PR body records Vercel's free daily deployment quota (HTTP 402) as a blocker for the latest visual refinement. Final Android screenshot comparison is outstanding.

## 7. PR and branch hygiene

At the time of audit, the repository connector returned **190 PRs**:
- 28 merged;
- 150 open;
- 12 closed without merge.

This includes many historical, stacked, experimental and superseded Phase 09 branches. Do not merge an old PR merely because its tests once passed. Resolve dependencies and compare exact heads against the current intended integration branch first.

Key open PRs to track:
- #10 / #12 / #23: host isolation and deployment.
- #11 / #24: signer boundary contract.
- #13–#22: optional zero-cost/Turso one-shot hosting path.
- #25 / #26 / #181 / #182: execution safety, transaction validation and simulation.
- #183: dashboard redesign.
- #184: Phantom Devnet sign-only path.
- #186 / #187 / #189 / #190: Stage 5 provider evidence and certification.

PR #188 is closed/unmerged and superseded by #189. PR #185 is a closed CI-verification-only PR and must not be merged. Similar closed validation-only PRs should remain historical evidence, not release candidates.

## 8. Immediate work order

The remaining Stage 9 work is gated in strict dependency order.

1. **Complete PR #191 host acceptance (Stage 09.2).** A real Linux/systemd host must be provisioned using the runbook. Install the exact reviewed units; run `systemd-analyze verify`; capture active unit/process identity, mount namespace, database/DAC/WAL/SHM and credential-isolation results; independently test host/cloud provider egress. Review the redacted evidence package. Until this exists, 09.2 is not complete.
2. **Only after 09.2 acceptance, review and integrate PR #192 (Stage 09.3 contract).** Keep it contract-only. Do not implement or enable a signer. Require independent review of typed chain/network, policy and message bindings, and the explicit signed-byte/cryptographic-verification gap.
3. **Then complete Stage 09.4 raw transaction verification.** Extend #182's structural decoder with chain-specific transaction/account semantics, canonical ALT provenance, instruction/program allowlists and exact intent binding.
4. **Then complete Stage 09.5 semantic execution verification.** Verify invoked DEX/token instruction semantics, CPI parent/authority provenance, same-context account snapshots, token-account/vault effects, exact input/min-output/destination, simulation state and account-effect reconciliation. Unknown routes/programs fail closed.
5. **Independent security review and end-to-end release audit** across the exact candidate commit, policy, evidence, CI, host controls and disabled execution flags.
6. **Complete PR #183 visual acceptance** after Vercel quota availability; compare Android browser screenshots against the supplied reference. Its code CI pass is not visual acceptance.
7. **Complete Stage 5 production evidence**: inspect required configuration names, confirm only presence/absence without exposing values, verify registry/hash sources and sample-token eligibility, then run the nine manual read-only workflows. Record each artifact, chain, claim family, run ID, exact head, finalized slot/block, provider IDs and reviewer outcome. Do not claim a pass for a skipped workflow.
8. **Resolve redundant PR #186** after confirming there are no unique changes; close it as superseded rather than merging duplicate code.
9. Keep the system paper-only. Do not create or enable a signer, submitter, broadcaster, mainnet execution or live-trading capability until the entire release gate has passed and the user explicitly authorizes that separate release.

## 9. Constraints and non-negotiable controls

- No live trading or mainnet execution.
- Keep `LIVE_TRADING=false`, `EXECUTION_RELEASE_ENABLED=false` and all Stage 5 capability-certification flags false until their independent acceptance gates pass.
- No private keys, seed phrases or secret values in source, logs, artifacts or this document.
- No automatic schedule, Telegram delivery or production deployment is implied by code or CI.
- Preserve the existing SQLite database, historical rows, credentials and service infrastructure; no reset or migration without a separately validated backup/restore plan.
- Keep hosting within the user's $0/no-bank-card/no-paid-trial constraint.
- CI skips are recorded as skips, not passes. Mock tests and provider HTTP 200 responses are not production certification.
- Do not merge stacked PRs out of dependency order.

## 10. Evidence and confidence labels

Use these labels in all future updates:
- **Merged/verified:** change is on `main`, and its required CI/acceptance evidence is recorded.
- **Implemented/unmerged:** code exists on a branch/PR but is not part of `main`.
- **CI verified:** exact commit has a successful CI run; says nothing by itself about live providers or production.
- **Live accepted:** exact production read-only acceptance workflow succeeded and artifact was inspected.
- **Independently certified:** source ownership, methodology, registry/hash and coverage have been reviewed independently.
- **Blocked:** a named missing test, external configuration, physical host action, quota or review prevents progression.
- **Not checked:** the available audit tools could not establish the fact. Do not infer success or failure.

## 11. Update protocol — mandatory

Update this file immediately after each completed project task, not at the end of a long batch of work.

For every task:
1. Record the task and date/time (UTC).
2. Record repository branch and exact commit/PR head.
3. State exactly what changed.
4. Record the tests/workflows run, exact run IDs and pass/fail/skip counts.
5. Separate code completion from live acceptance and certification.
6. Update the stage matrix, immediate work order and blocker list if the status changed.
7. If nothing changed in the repository, record the audit finding without claiming a code change.
8. Never overwrite an earlier result silently; append a dated entry to the activity log below.

### Activity log

| Date (UTC) | Task | Branch / commit / PR | Result and evidence |
|---|---|---|---|
| 2026-11-15 | Initial repository-wide audit and progress baseline | `main` baseline `6158a5995c7f241a7c1ba56dc20161c3fd7e4fa2`; PR heads listed above | Inspected repository metadata, stage documentation, merged/open/closed PR inventory, exact-head CI runs and failure logs. No application source code changed in this audit. Initial ledger created. |
| 2026-11-15 | Recursive source/test/workflow inventory | `main` ledger commit `d54e4881ec93f676312cb5f6175f5424f194b069`; tree complete at subsequent ledger revision | Confirmed 213 tracked paths: 92 `veltrix/`, 86 tests, 12 docs, 16 workflow files. Reviewed main CI and key manual acceptance/preflight workflow triggers. No cron trigger found in the inspected main workflows; external schedulers and secret presence remain not checked. |
| 2026-11-15 | Full Actions history and production acceptance audit | Actions runs `42912477053`, `42964334567`, `43724610264`, `43725136999`, `43752387729`; ledger revision before this update | Confirmed Turso H3 disposable-database conformance: 5 passed in 325.57s. Confirmed Stage 5 liquidity workflow runs with reader steps skipped or preflight failures and no artifacts; 0/9 production evidence remains verified. |
| 2026-11-15 | Stage 5 PR overlap review | PRs #186 and #187, exact changed-file patches | Confirmed #186's monitor/developer-evidence patches are identical to code already included in #187, with tests present in #187. #186 is redundant and should not be merged separately. |
| 2026-11-15 | PR #181 audit-chain regression correction | `work/phase-9-execution-safety-gates` head `f984e1b1611b49d766d2eb2467683fb5d97f3d4d` | Corrected the test to validate the global append-only hash-chain predecessor across interleaved intents; offline-tamper assertion retained. Exact-head CI run `43766530934` passed: 906 passed / 5 skipped; compileall passed. No code merge or live-execution enablement. |
| 2026-11-15 | PR #183 dashboard test correction | `work/dashboard-reference-mobile-rebuild` head `7fa969d7460d27eb06b7072454ed3d3c9e4d147c` | Moved authenticated dashboard JavaScript asset retrieval before the `dashboard_js` assertions. Exact-head CI run `43766534202` passed: 979 passed / 5 skipped; compileall passed. Android visual QA remains blocked by Vercel daily quota. |
| 2026-11-15 | Stage 09.2 current-main integration | PR #191, `work/phase-9.2-isolation-current-main` | Rebuilt host-isolation integration on current main because PR #23 was 52 commits behind/diverged. Added systemd templates, preflight/verifier, runbook, wallet-domain types, loopback dashboard authentication, DAC hardening, symlink rejection and CI validation. |
| 2026-11-15 | Stage 09.2 CI and systemd template validation | PR #191 head `76a9b54131c1c67b29ba533e4c7758b8c20b42b0`; runs `43767022608`, `43767121211`, `43767229327`, `43767439888`, `43767513662` | Final exact-head run `43767513662` passed: 886 passed / 5 skipped; compileall, `bash -n` and `systemd-analyze verify` passed. Intermediate failures were recorded and corrected: host-specific ExecStart path in raw unit verification, then stale static-test expectation after DAC verifier hardening. No actual host install or host acceptance. |
| 2026-11-15 | Stage 09.3 dependent signer-boundary contract | PR #192, `work/phase-9.3-signer-contract-after-9.2` head `8b2cef7eb57ba22bcd9a307ba51495c45821182d`; runs `43767720194`, `43767800370` | Added typed chain/network immutable envelopes, canonical policy hashes and metadata-binding tests. Explicitly documented that metadata equality does not verify signed bytes or cryptographic signatures. Final exact-head CI passed: 919 passed / 5 skipped; compileall, shell syntax and systemd template verification passed. PR #192 remains draft and stacked on #191; no signer implementation. |

## 7. Hosting implementation status correction — 2026-11-15

This section is authoritative where earlier wording in this ledger conflicts with it.

### Selected hosting direction

The project direction remains the previously selected **$0/no-bank-card serverless architecture**:
- **GitHub Actions hosted Linux runners:** bounded scheduled/batch Python work and CI; not an always-on API server.
- **Turso Free:** remote SQLite-compatible persistence, subject to the existing driver/SQL compatibility and production migration gates.
- **Cloudflare Pages + Workers:** static dashboard hosting and a narrow read-only API/runtime layer, subject to a verified implementation and deployment.
- **GitHub repository:** source control and CI/CD.

Do not treat a dedicated Linux VPS or systemd host as a prerequisite for this selected deployment. Preserve PR #191's useful isolation controls as an optional alternate deployment path or translate applicable controls into serverless equivalents.

### What is actually complete

- PR #13 records the architecture decision and compatibility audit; it is documentation, not deployment.
- PRs #14–#18 contain Turso DB-API/conformance and adapter work.
- PRs #19–#22 contain bounded orchestration, worker fencing and one-shot pipeline integration.
- Remote Turso conformance passed in Actions run `42912477053` (five DB-API/schema/orchestration/worker/pipeline suites, 325.57 seconds) against a dedicated disposable database. Integrated H1.1/H2/H3 conformance also passed in run `42964334567`.
- These tests do **not** certify production credentials, production database migration, data cutover, Cloudflare deployment, DNS/HTTPS routing, deployed API health, scheduled production operation, or disaster recovery.
- SQLite remains the default/rollback data store until a separately approved and verified migration.
- PR #22 remains an open draft integration candidate and must be checked for current head, ancestry, diff and CI before merge; do not assume the recorded historical branch state is still current.

### Hosting acceptance work order

1. Re-audit current `main`, PRs #13–#22, and the existing serverless branch before changing implementation. Record current SHAs and CI, and do not repeat already-passed disposable Turso conformance unnecessarily.
2. Verify the Python runtime split: which modules run as bounded GitHub Actions jobs, which functions need a Cloudflare Worker/API boundary, and whether the current Python dashboard/API can run there without an unsupported runtime assumption. Define an explicit compatibility adapter or a supported runtime split; do not assume Python code runs unchanged inside Workers.
3. Finish and merge the reviewed H1.1/H2/H3 integration only after current-head synchronization, regression CI, security review, and a clear rollback plan.
4. Build and test Cloudflare Pages/Workers deployment configuration, API authentication, environment separation, least-privilege secrets, health checks, logs, and rollback. No deployment is complete until the deployed URLs and runtime health are verified.
5. Add scheduled bounded pipeline invocations through GitHub Actions only after idempotency, fencing, retry, concurrency, rate limits and WAT schedule semantics are verified. GitHub Actions schedules are not a guarantee of exact-minute execution or continuous WebSocket capture.
6. Provision and verify a separate production Turso database identity; test schema migrations, transactions, backup/restore and recovery against a disposable copy first. Never reuse destructive conformance credentials for production.
7. Perform a controlled production data migration only after export/import row counts, schema, constraints, ledger integrity and rollback are independently verified. Keep SQLite intact until cutover acceptance.
8. Verify end-to-end dashboard/API → worker → Turso operation, schedule delay/retry behaviour, provider egress and emergency stop. Only then mark hosting operational.

### Stage 09.2 disposition

The systemd/real-host requirement in the earlier Stage 09.2 row applies only to the **optional PR #191 host-isolation path**. It is not a blocker for the selected GitHub Actions + Turso + Cloudflare architecture. Serverless isolation acceptance must instead cover ephemeral job boundaries, workflow permissions, environment/secret scoping, Cloudflare runtime isolation, API authentication, Turso database identity and token scope, concurrency/fencing, egress controls, observability, backup/restore and operational stop controls.

PR #191 remains optional and unmerged; do not make PR #192's dependency on #191 a reason to delay hosting. Keep all signing, transaction submission, broadcasting and live execution disabled. Hosting completion does not grant execution authorization.

## 8. Hosting implementation progress — 2026-11-15

This section supersedes the earlier provisional hosting work order where the actual implementation status has changed. It does not imply production deployment.

### Reconciled worker/database integration

- **Implemented/unmerged:** PR #22's H1.1/H2/H3 code has been reconciled with current `main` on `work/hosting-integration-reconcile-20261115`, then composed into `work/hosting-complete-20261115`.
- The reconciled branch preserves current-main release locks, dashboard behavior, provider error sanitization and validation. It does not force-merge the 57-ahead/65-behind original PR #22 branch.
- Added transaction-scoped persistence for launch candidates, market snapshots, risk evidence and consensus decisions. Transaction methods re-run domain validators before writes.
- Added production schema initializer `scripts/initialize_turso_database.py`. It refuses the disposable H0 conformance database, initializes the application and empty paper-ledger schema, verifies expected tables, and performs no provider calls or data migration.
- Added one-shot entrypoint `scripts/run_solana_pipeline.py` and a manual-only Actions workflow. It uses a stable two-hour idempotency bucket and keeps paper trading, Telegram, scheduler activation and live execution disabled.
- Exact reconciled Python CI run `43771310697` on `work/hosting-integration-reconcile-20261115` head `e411dd4aafec396f19c051f68c44bb945a186983`: **914 passed / 10 skipped**, compileall included `scripts/`. The 10 skips are not counted as passes.
- Remote Turso conformance run `43770940483` passed against exact reconciled core commit `8dd58136880c3e6de853b446310ffaa704bc1dd7`: **5 passed in 391.62s** across remote DB-API, application schema, orchestration, worker and pipeline conformance on the dedicated disposable database. This does not certify production Turso credentials or migration.

### Cloudflare Pages and Worker API

- **Implemented/unmerged:** static Pages bundle, Wrangler configuration, Pages Functions API and automated deploy workflow are included in `work/hosting-complete-20261115`.
- The Pages Function uses `@libsql/client/web` and exposes fixed GET-only dashboard routes. It requires a 32+ character bearer token, validates a credential-free production `libsql://*.turso.io` URL, fails closed when bindings are missing, and has no write, signer, submitter or live-execution endpoint.
- Dashboard API uses a distinct read-only Turso token. The deployment workflow verifies that the token cannot execute a no-row UPDATE inside a rolled-back transaction before deploying.
- Node security suite and local Pages static/API smoke passed on the latest Cloudflare implementation branch head `e87c2172965f5d1141e1544aab6e4069ed0da774`: run `43771483897`, **10 passed / 0 failed**. This run also syntax-checked the read-only-token verifier, validated Wrangler project/secret/deploy CLI flags, built the Pages bundle and verified static/API auth behavior. Python CI run `43771483835` passed: **858 passed / 5 skipped**. The final composed branch still requires exact-head CI.
- The deployment workflow creates the Pages project if needed, writes production-only secrets, deploys assets and Functions, then checks the deployed dashboard and authenticated API health. No deployment has yet been performed.

### Scheduling and execution disposition

- The repository's Stage 9.1 release policy and `tests/test_foundation.py::test_existing_workflows_do_not_define_scheduled_triggers` explicitly prohibit scheduled workflow triggers. An attempted cron workflow failed this guard and was converted to `workflow_dispatch` only.
- Do not bypass this lock using Cloudflare Cron or another external scheduler. A two-hour UTC cadence is only a proposed post-release setting; no automatic schedule is active.
- Paper trading, Telegram delivery, signing, transaction submission, broadcasting and live execution remain disabled.

### Production acceptance and manual setup

Production hosting is **not yet accepted**. Required operator setup is:
1. Create a separate production Turso database (not the H0 conformance database).
2. Add GitHub Actions secrets: `TURSO_DATABASE_URL`, read/write `TURSO_AUTH_TOKEN`, read-only `TURSO_READONLY_AUTH_TOKEN`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `DASHBOARD_API_TOKEN`, and either `SOLANA_RPC_URL` or `HELIUS_API_KEY`. Optional `GOPLUS_ACCESS_TOKEN` may be configured separately.
3. Run the manual production schema initializer; then run the Cloudflare Pages deploy workflow and inspect its deployed API health check.
4. Run the manual one-shot pipeline and verify candidate/market/risk/consensus persistence, idempotent retry and recovery against the production database.
5. Independently verify Turso backup/restore and operational recovery before calling hosting complete.

Do not send secret values in chat. Secret inventory is not exposed by the repository audit tools, so current production-secret presence is **not checked**.

The repository tree contains no tracked SQLite database file. The initializer creates a new empty paper ledger account and does not migrate untracked local data. If an existing local SQLite file must be preserved, obtain that exact file and perform a separately tested export/import reconciliation before cutover.

### Current classification

- **Implemented/unmerged:** reconciled H1.1/H2/H3 integration, production schema initializer, manual one-shot runner, Cloudflare Pages static bundle and read-only Pages Function.
- **CI verified:** 914 passed / 10 skipped for reconciled Python integration; 10 passed / 0 failed for the prior Cloudflare Pages API test run. Recheck exact final branch CI after PR creation.
- **Pending:** exact-head remote Turso conformance, exact final-branch CI, production database provisioning, controlled data migration (if a source DB exists), Cloudflare deployment, authenticated production health, scheduled release authorization, backup/restore and end-to-end operational acceptance.
- **Blocked by operator setup:** production Turso credentials, Cloudflare API/account credentials, dashboard access token, and Solana RPC/Helius credentials.
