# Solana Meme-Coin Rebuild — Stage Log

- Baseline branch: `main` at `77c0d63c12f38d909d266494df9b24fe3d5cc72f`.
- Working branch: `work/solana-meme-first`.
- Pull request: #1, draft and unmerged.
- Architecture and delivery roadmap: `docs/solana-meme-architecture.md` and `docs/solana-meme-roadmap.md`.
- Live trading remains disabled.

## Implemented components

- Canonical Solana mint validation, strict chain normalization and multi-source candidate deduplication.
- PumpPortal new-token launch stream adapter and supervised persistent ingestion.
- DexScreener market snapshot normalization and append-only SQLite history.
- Retry-safe candidate-to-market enrichment worker.
- Claim-separated risk evidence collector using GoPlus, holder inventory and optional finalized mint-account authority/Token-2022 evidence.
- Append-only risk evidence report storage and a leased, retry-safe five-minute refresh worker.
- Runtime optionally enables risk collection using existing GoPlus and Solana RPC credentials; no new secret was added.
- Solana Shield paper-entry policy requiring complete claims, verified composite safety,
  fresh evidence, same-mint multi-agent consensus, and position limits.
- Core now routes Solana mints through the Solana Shield before the existing SQLite paper broker.
  Valid Solana public keys cannot bypass the gate by omitting the chain label or by being
  mislabeled as an EVM chain.
- The generic paper broker remains paper-only; no signer, wallet, or live swap path was added.

## Validation boundaries

- Unit tests use synthetic provider/evidence fixtures. This is not real-provider certification.
- PumpPortal has no historical backfill; reconnect tracking cannot reconstruct missed launch events.
- The current risk collector still leaves developer linkage, bundled supply, creator provenance
  and executable sellability unresolved. Active mint/freeze authorities are vetoed; Token-2022
  mints with extensions remain blocked pending extension-specific payload and authority review.
- The recurring market-refresh worker maintains append-only market observations.
- A 24-hour paper operation and net-of-cost performance evaluation have not been completed.
- No live trading, wallet signing, or deposits are permitted.

## CI

Latest full CI passed at commit e1dfa1fe0472016386a8ded50ba47b7489e2df07:
373 passed, 5 skipped; compileall and pytest both succeeded. This includes the read-only runtime,
finalized mint parser, risk evidence factory, persistent risk store/worker, and pipeline wiring.
Provider-backed production validation has not been performed.


## Developer ancestry integration — 2026-11-09

Added an opt-in Solana developer evidence worker and append-only report store:
- verifies signed Ed25519 root attestations bound to the exact Solana mint;
- requires explicit trust entries for holder inventory, Helius funding graph and root provider;
- traces holder funding ancestry and counts only wallets whose ancestry reaches an attested root;
- records linked supply, evidence provenance and incomplete-coverage reasons;
- uses bounded SQLite leases, refresh scheduling and retry backoff;
- is wired into the read-only pipeline after risk collection.

The factory returns no worker unless the existing Helius and Solana RPC credentials,
signed-root endpoint/public key, and all three provider trust entries are configured.
No new API key or signing secret is introduced.

**Not a bundle detector:** this worker does not infer bundled token purchases, shared creator control,
or common ownership from timing or common exchange funding. It is not registered as a
certified safety provider (`certified_provider_id=None`), so its output cannot become
trusted `SafetyEvidence` or approve a paper/live entry. Real Helius/root-provider coverage
and methodology still require external validation.


## Pump.fun launch-window bundle evidence — implementation added

Added a bounded Helius Parsed Events Scan adapter for Pump.fun buy instructions.
It resolves the mint initialization transaction, scans the elapsed launch window,
validates buyer token transfers, reads finalized mint supply, and links buyers
to first-funder ancestry through the existing Helius funding graph.

Added append-only bundle report storage and a low-volume scheduled worker.
Activation requires explicit trust entries for:
- `helius-launch-origin`
- `helius-solana-launch-index`
- `helius-solana-funding-graph`

The output is a shared-funder launch-cohort heuristic, not proof of common
ownership or developer control. It is not certified safety evidence. Incomplete
scan pagination, missing buyer funding, stale supply, or invalid transaction
provenance withholds the bundle percentage. This adapter covers Pump.fun only.

The first CI attempts exposed missing provider IDs in test fixtures; both
fixtures were corrected. Latest full CI passed at commit
`5b3125924fe5c504afe5aeb0e78fa03fef2d897b`: **395 passed, 5 skipped**.
Provider-backed production validation has not been performed.


## Sell-route evidence evaluator — 2026-11-09

Added a provider-neutral, read-only sell-route evaluator and tests. It requires an
exact-in candidate-token to wrapped-SOL quote, exact raw input amount, provider
provenance, freshness and expiry, non-empty route, positive quoted/minimum output,
bounded supply fraction, and bounded price impact.

The evidence state is explicitly ROUTE_OBSERVED_NOT_EXECUTION_PROVEN. The
execution_proven field is permanently false; this evidence cannot satisfy the
existing executable_sellability claim, bypass Shield, or approve a paper/live
entry. No Jupiter adapter, API key, wallet, signer, transaction submission, or
runtime activation was added. Jupiter V2 taker requirements remain a blocker for
a real provider adapter without a separate dedicated public test-wallet address.

Unit fixtures are synthetic and do not constitute provider-backed sellability
validation. A later stage must add controlled unsigned-transaction simulation,
token transfer-semantics review, and chain-confirmed test results before any
execution claim.


## Sellability evidence persistence and worker — 2026-11-09

Added an append-only sellability evidence store and a bounded SQLite worker. The
worker only schedules candidates with finalized mint-account evidence and a
positive raw supply, tests a bounded 1% supply fraction by default, and persists
route/no-route/unverified observations with retry-safe leases.

The worker remains standalone and is not wired into the production runtime
because no independently reviewed Jupiter quote adapter or dedicated public
taker address is configured. It never modifies the risk report's missing
executable_sellability claim. Synthetic tests do not certify real sellability.


## Jupiter Swap API V2 quote-only adapter and opt-in runtime — 2026-11-09

Official Jupiter documentation confirms that /swap/v2/order returns quote data
without a taker and no transaction. Keyless access is supported at 0.5 RPS.
Added a provider adapter that:
- requests exact-in candidate-token to wrapped-SOL quotes without a taker;
- validates mint direction, exact raw input amount, route plan, output,
  minimum output, request provenance and price-impact fields;
- converts V2 priceImpact percentage points into basis points;
- enforces a 2.1-second minimum request interval and a 5-second local evidence TTL;
- never reads or processes transaction bytes and never calls /execute.

The provider is wired behind SOLANA_SELLABILITY_ENABLED=false by default. Enabling
it requires existing GoPlus and Solana RPC evidence so the worker can use
finalized supply. JUPITER_API_KEY is optional; keyless prototype access requires
no new secret. The worker remains read-only and does not clear the
executable_sellability claim.


## 2026-11-09 — Controlled sell-transaction simulation foundation

- Added `JupiterSolanaSellSimulationProvider` in `veltrix/providers/solana_sell_simulation.py`.
- The provider requests a Jupiter Swap API V2 unsigned order for a configured dedicated test-wallet public key, validates mint direction, amount, request ID, transaction encoding and returned taker identity, then calls Solana RPC `simulateTransaction`.
- Simulation explicitly sets `sigVerify=false`, `replaceRecentBlockhash=true`, and `commitment=processed`.
- The result records state, simulated slot, compute units, logs and error. It permanently records `signature_verification=false` and `transaction_submitted=false`.
- Added mocked tests for successful simulation, simulation errors, invalid wallet/RPC configuration and invalid sell amount.
- Added `docs/solana-sell-simulation.md` with operational and interpretation boundaries.
- This provider is not enabled by default or connected to the runtime. It requires a dedicated test wallet holding the candidate token balance; no Phantom address or private key is used.
- CI was triggered for the new commits; final status is pending verification.
- No live signing, transaction submission, paper-entry approval or production-main change.


## 2026-11-09 — Explicit-case sell simulation worker and runtime integration

- Added append-only `SolanaSellSimulationStore` and bounded `SolanaSellSimulationWorker`.
- The worker processes only explicit mint/raw-amount pairs supplied by operator configuration; it never loops across all discovered candidates or assumes a token balance.
- Added disabled-by-default settings: `SOLANA_SELL_SIMULATION_ENABLED`, `SOLANA_SELL_SIMULATION_TAKER`, and `SOLANA_SELL_SIMULATION_CASES_JSON`. The taker is a public key only; no private key setting exists.
- Added provider-factory construction, pipeline orchestration, runtime reporting and package exports.
- Hardened Jupiter transaction validation: decode the returned Solana transaction, require the configured taker among required signers, and reject any non-zero signature slot before simulation. Jupiter V2 docs do not define a taker response field, so identity is verified from the transaction itself.
- Added worker persistence/retry tests, factory opt-in tests, pipeline ordering/database isolation tests, and signer/signature validation tests.
- GitHub Actions run [42681836081](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42681836081) passed on code commit `d13194db9f683e17c0eee5a2eaed93c7622ade42`: 424 passed, 5 skipped. The runtime remains disabled unless the operator explicitly configures a dedicated test wallet and exact token amounts.
- No signer, sendTransaction, Jupiter /execute, paper-entry approval or production-main change.


## 2026-11-09 — Conservative sell-simulation failure diagnostics

- Added a deterministic diagnostic classifier for successful simulations, RPC/blockhash context failures, test-wallet balance/fee issues, missing token-account setup, transfer-restriction signals, unclassified program errors and unknown failures.
- Transfer-hook/non-transferable matches are only review signals. Missing accounts and insufficient funds are explicitly classified as wallet/setup issues.
- Every diagnostic has `honeypot_verdict=false`; the classifier cannot certify token safety or sellability.
- Added tests for all primary diagnostic categories and exported the diagnostic API.
- GitHub Actions run [42682023137](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42682023137) passed on code commit `fa4cab28ea91e0eddc2c589cbbc37cde71b6d703`: 430 passed, 5 skipped.


## 2026-11-09 — Finalized simulation-wallet balance preflight

- Added `SolanaSimulationWalletPreflightProvider` to read finalized SPL Token and Token-2022 accounts for the configured public test wallet and exact candidate mint.
- Sums raw token balances across matching accounts, validates wallet/mint ownership and supported token program IDs, records native SOL balance and conservative finalized slot.
- Added append-only `SolanaSimulationPreflightStore` and integrated the preflight gate into the simulation worker. Jupiter order assembly is skipped when the test wallet lacks the exact configured token amount.
- Added runtime reporting, factory integration and tests for multiple token accounts, insufficient balance, missing accounts, ownership mismatch and worker gating.
- Native SOL balance is recorded but not assigned an arbitrary universal threshold; route fee/rent requirements vary.
- Simulation remains opt-in and requires a dedicated funded test wallet; no seed phrase or signing key is accepted.
- GitHub Actions run [42682361273](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42682361273) passed on code commit `cdf88c95e07aa45f4a73cb97eea77e5ed81e7496`: 437 passed, 5 skipped.


## 2026-11-09 — Shield paper-entry sell-route gate

- Extended `SolanaShieldPolicy` to require a typed, fresh, provenance-backed sell-side route observation for the exact intended raw-token exit size.
- Validates canonical mint, quote expiry, route plan, positive quote/minimum output, price impact (maximum 300 bps), supply fraction (0.0001%–5%) and reconciliation of that fraction to the finalized raw token supply.
- Keeps all unknown mandatory token-risk claims as vetoes. The `executable_sellability` item remains unresolved as a live-execution-only claim; a paper route does not clear it.
- Added Core integration. Solana paper entries now fail closed when route evidence or the intended raw-token exit size is absent.
- The paper trade result and broker audit reason preserve the exact state `ROUTE_OBSERVED_NOT_EXECUTION_PROVEN` and the route evidence ID.
- Added tests for absent, stale, expired, wrong-size, unreconciled, high-impact and malformed route evidence; unknown token-risk claims still veto.
- GitHub Actions run [42684690748](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42684690748) passed on code/test commit `4bc3b2e6c4fc4abd06d0ae371736cbf320ac8c0e`: 441 passed, 5 skipped. Earlier Core integration run [42684511802](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42684511802) passed 440 tests.
- PR #1 remains draft and unmerged. The `main` branch remains unchanged. No wallet signing, transaction submission or live trading was added.


## 2026-11-09 — Exact paper exit sizing and supply reconciliation

- Added `calculate_paper_exit_size`, using Decimal arithmetic and downward raw-unit rounding to convert the configured USD paper notional and token USD price into a raw SPL-token amount.
- The sizing utility requires finalized token decimals and raw supply, rejects amounts below one raw unit and enforces a 0.0001%–5% supply-fraction band.
- Extended risk evidence to retain finalized mint supply and decimals and explicitly reconcile both against the holder inventory. Mismatches remain provider errors and cannot pass Shield.
- Shield now recomputes the expected raw exit amount from paper notional, current token price, finalized decimals and supply. A quote for a different size is rejected, even if it is otherwise fresh and low-impact.
- Core passes the current token USD price into Shield and preserves the exact raw exit amount, quote evidence ID and `ROUTE_OBSERVED_NOT_EXECUTION_PROVEN` state in the paper trade result/audit.
- GitHub Actions run [42685456844](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42685456844) passed on code/test commit `f6b89ce60cfb973a0a5adcd3fb7fafaae2aa35e2`: 453 passed, 5 skipped.
- No live transaction signing, submission or wallet use was added.


## 2026-11-09 — Runtime sell quotes use exact paper position size

- Connected `SolanaSellabilityWorker` to the shared market snapshot store in the read-only runtime.
- With sellability enabled, it derives the configured $50 paper-position raw-token amount from the latest USD market price, finalized mint decimals and reconciled raw supply. It no longer relies on a generic 1%-of-supply quote for this runtime path.
- The worker rejects missing or stale market snapshots (maximum age 120 seconds), stale risk evidence (maximum age 300 seconds), unreconciled supply/decimals, and out-of-band sizes. Failures are retried; they do not produce a route approval.
- The generic supply-fraction mode remains available only when no market store is supplied, for diagnostics and isolated tests.
- GitHub Actions run [42685911725](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42685911725) passed on commit `77877bd930d7162ed9459ec1f136c2c9d200704a`: 455 passed, 5 skipped.
- Runtime flag remains `SOLANA_SELLABILITY_ENABLED=false` by default. Quote requests remain read-only and do not require a taker, signer, or transaction submission.


## 2026-11-09 — Next integration boundary documented

- Added `docs/solana-paper-decision-runner.md` defining the next bridge from persisted evidence stores into Core and the paper broker.
- The runner is a design boundary only; it has not been implemented or enabled. It must reconstruct typed evidence, enforce freshness and exact quote sizing, retain audit provenance, and remain unable to sign or submit transactions.
- Acceptance tests are specified for stale/missing evidence, unknown risk claims, amount mismatches, duplicate processing, cash/position limits and transaction-submission exclusion.


## 2026-11-09 — Evidence-to-paper decision runner implemented

- Added `SolanaSafetyReportStore` for append-only persistence of typed composite safety reports. The store does not create or certify reports.
- Added `SolanaAgentContext` and `SolanaPaperDecisionRunner`. The runner joins persisted candidate, market, risk, developer, bundle, sell-route and composite-safety records; reconstructs typed reports; and invokes the existing paper-only Core.
- Added deterministic evidence-packet claims and append-only decision audits. A repeated evidence packet is not replayed. A process crash after a packet is claimed requires reconciliation rather than automatic replay, preventing an uncertain Core/broker outcome from being duplicated.
- The runner permits only the explicit `executable_sellability` unknown for paper evaluation. It remains in the persisted risk report; route evidence is still labelled `ROUTE_OBSERVED_NOT_EXECUTION_PROVEN`.
- Added explicit trusted-provider allowlisting for agent context, finalized supply/decimals reconciliation, developer and bundle provenance checks, freshness gates, and paper account profile checks.
- Added tests for complete typed evidence, two-agent paper entry, missing evidence, idempotency, missing agent context, and untrusted agent-context providers.
- GitHub Actions run [42687193171](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42687193171) passed on test commit `a28b00b071bdafbab824288693748eb8e9103d57`: 459 passed, 5 skipped.
- **Not runtime-enabled:** the current Solana provider capability catalogue still has no certifying Solana liquidity-lock provider and no certifying developer-holdings provider. A production composite SafetyReport producer is therefore not available. There is also no production Solana agent-context provider with a verified source for momentum, volume-spike and independent smart-wallet BUY signals. The runner fails closed and is not connected to the runtime.
- No live wallet, signer, Jupiter `/execute`, RPC write, or transaction submission capability was added.


## 2026-11-09 — Solana paper decision runner implemented

- Added `SolanaPaperDecisionRunner` to join persisted launch, market, risk, developer-ancestry, launch-bundle, sell-route and composite-safety evidence before invoking the existing paper-only Core.
- Added `SolanaSafetyReportStore` to persist typed composite safety certificates. The store does not create certificates or promote partial evidence.
- Added bounded candidate listing to `LaunchStore`.
- Runner reconstructs typed risk, sell-route and safety reports; enforces market freshness (120 seconds), risk/developer/bundle/safety freshness (300 seconds), sell-route freshness (15 seconds), quote expiry, finalized supply/decimal reconciliation, complete developer and bundle evidence, zero known bundled supply, and no unknown mandatory risk claims.
- Added an explicitly allowlisted, typed agent-context contract. It cannot replace market price or safety evidence. Core must still produce at least two distinct BUY agents for the same mint.
- Added deterministic evidence-cycle claims and append-only decision audits. The claim is persisted before Core evaluation; a crash leaves a non-replayable claimed cycle for reconciliation rather than risking a duplicate paper entry.
- Added tests for missing evidence, untrusted/missing agent context, complete typed evidence, two-agent consensus, paper broker cash/position handling and duplicate-cycle idempotency.
- CI run [42687215593](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42687215593) passed on code/test commit `f618e949893a7182d2e3eb0de7c28e2f167d7a87`: 459 passed, 5 skipped.
- The runner is not wired into the read-only runtime and is not enabled. Production composite-safety and trusted agent-context providers are not yet available; the documentation records these blockers. No live signing or transaction submission was added.


## 2026-11-09 — Paper decision crash reconciliation

- Added `reconcile_stale_claims()` to inspect decision claims left in CLAIMED state after a process interruption.
- If the durable PaperBroker has a matching position opened after the claim timestamp, the audit is marked `PAPER_ENTRY_OPENED_RECONCILED`.
- If no matching new position exists, the audit is marked `INDETERMINATE_NO_REPLAY`; it is never automatically retried. Manual ledger reconciliation is required.
- Added tests for both recovery paths and confirmed neither path creates a duplicate paper position.
- GitHub Actions run [42687501006](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42687501006) passed on code/test commit `2743734fcdb9f8f07ae4ebbdb67d1952c86e5022`: 461 passed, 5 skipped.
- The runner remains disabled in the runtime pending independently certified safety and agent-context providers.


## 2026-11-09 — Market-derived Scout context

- Added `SolanaMarketAgentContextProvider` using persisted DexScreener market snapshots.
- It derives a volume-spike flag only after five distinct prior observations from the same selected pool; compares latest hourly volume with the prior-observation median and carries a content-addressed evidence ID.
- Pool changes, stale snapshots, insufficient history or zero baseline volume cannot produce a spike signal. The adapter never invents smart-wallet BUY evidence; `smart_wallet_buys` remains empty.
- This context can support Scout only. A second independent BUY agent is still required; Sniper depends on a separately certified composite safety report. Whale remains inactive until a separate trusted wallet-evidence provider exists.
- The provider is exported and tested but not wired into runtime settings or allowlisted in production.
- GitHub Actions run [42687790632](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42687790632) passed on code/test commit `a0927118adef8a6897a8e970d5208ca6188ece8e`: 470 passed, 5 skipped.


## 2026-11-09 — Agent context and decision-runner crash recovery

- Added `SolanaMarketAgentContextProvider` for Scout market inputs. It compares hourly volume only within the same selected pool, using five distinct prior observations, a 2x median threshold, a 15-minute baseline freshness cap and a 120-second latest-snapshot cap. Pool changes or insufficient/stale history cannot create a volume-spike signal.
- Added `SolanaAgentContextProvider`, composing market context with the existing Helius wallet-flow coordinator. Helius wallet identities are included only when the configured tracked-wallet and provider allowlists accept them. The factory defaults to market-only context and adds Helius only when credentials, at least two tracked wallets and the Helius trust entry are present.
- Agent context now has a content-addressed evidence ID and explicit provider ID. The decision runner requires that provider ID in its own trusted-context allowlist.
- Hardened decision claims with atomic per-mint SQLite claiming. The runner reconciles stale claims against the PaperBroker ledger before processing; matching post-claim positions are marked reconciled, while uncertain outcomes become `INDETERMINATE_NO_REPLAY` and block later evidence cycles for that mint.
- Added `acknowledge_indeterminate_claim` for an operator to release a mint only after ledger reconciliation and a recorded operator note. No automatic replay is allowed.
- GitHub Actions run [42688293786](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42688293786) passed on commit `6ead1000f5b0996f078312c4c7fcdff1237efdda`: 474 passed, 5 skipped.
- Runtime activation remains withheld. The default capability catalogue still lacks certifying Solana liquidity-lock and developer-holdings providers, so no production composite-safety report can pass. The runner is not wired to the runtime and live trading remains disabled.


## 2026-11-09 — Solana UNCX liquidity-lock coverage audit

- Expanded the source-backed UNCX Solana locker candidate registry to include Raydium AMM V4, AMM V4 Smart Lockers, CP Swap, CLMM, Meteora DLMM and PumpSwap program IDs and documented LP-storage accounts.
- Only the Raydium AMM V4 locker currently has public source, Anchor IDL, deployed binary and a published verifiable executable hash pinned in this project. The other program IDs remain candidates; no decoder or certification is inferred from their names.
- Added a provider capability entry for the existing Raydium V4/CPMM inventory reader as **partial/non-certifying**. The composite safety validator continues to reject it as complete Solana liquidity-lock proof.
- Added `docs/solana-liquidity-coverage.md` with the program coverage matrix, source links, required account/upgrade checks and next implementation sequence.
- GitHub Actions run [42688687527](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42688687527) passed on commit `5e4bcdd9e12b15caf442532370abfbd7f898a9d1`: 475 passed, 5 skipped.
- No capability was promoted to complete. No live trading or transaction signing was enabled.


## 2026-11-09 — Agent-context packet consistency hardening

- `SolanaMarketAgentContextProvider` now requires the exact persisted market snapshot supplied in the decision packet.
- It rejects a packet if a newer market snapshot becomes the persisted history head between candidate selection and context construction. This prevents the runner from combining agent signals derived from one snapshot with evidence IDs from another.
- Updated tests to supply typed persisted market rows and added a superseded-snapshot race test.
- GitHub Actions run [42689166638](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/42689166638) passed on commit `aeac91603e6cd000465a5af7dfb557f55c615984`: 476 passed, 5 skipped.
- The first CI attempt on the implementation commit failed because one baseline-free test still passed an empty packet. The test was corrected to use the persisted snapshot; the latest code commit is green.
