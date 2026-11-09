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

The first CI attempt exposed a missing provider ID in the new test fixture;
the fixture was corrected. The latest run must be checked against the newest
branch head before this stage is marked CI-verified.
