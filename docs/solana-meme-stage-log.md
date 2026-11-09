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
