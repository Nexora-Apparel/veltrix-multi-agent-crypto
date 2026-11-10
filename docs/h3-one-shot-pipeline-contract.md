# H3 one-shot Solana analysis pipeline contract

## Objective and current implementation

Compose the existing bounded Solana workers into one finite scheduled pipeline. A scheduler invocation runs one orchestration attempt and exits; it does not start a daemon, background thread, websocket supervisor, or hidden retry loop.

Implementation lives in `veltrix/solana_meme/pipeline.py` as `OneShotSolanaPipeline`. Its production builder reuses the existing provider factories and constructs discovery with PumpPortal websocket supervision disabled.

## Fixed stage order

1. **Discovery** — one bounded poll of configured read-only discovery sources. The ephemeral runner uses bounded secondary-feed polling and does not start the long-lived PumpPortal stream supervisor.
2. **Initial enrichment** — process at most 25 newly discovered mints and persist timestamped market observations.
3. **Market refresh** — refresh at most 25 due observations; preserve source, retrieval-time, quality and staleness metadata.
4. **Risk evidence** — collect and persist current read-only security evidence for at most 5 eligible candidates.
5. **Consensus** — compute and persist read-only decisions from persisted, timestamped market and risk evidence, at most 10 candidates.
6. **Optional paper-only stage** — process exits and evaluate paper entries from the latest fully revalidated consensus decision, at most 25 decisions. This stage is disabled by default and only calls the existing paper broker.

No stage constructs, signs, submits or broadcasts a Solana transaction.

## Fencing contract

One active `RunContext` is passed through every stage.

- The context carries run ID, idempotency key, attempt, deadline, item budget, owner ID, lease name and fencing token.
- The context supports a transaction-scoped global-lease assertion using the caller's exact database connection.
- Every worker claim, completion, failure, retry scheduling and evidence mutation checks the global fence and its per-row owner/token claim in the same transaction as the mutation.
- Provider reads happen outside write transactions; the global fence and row claim are revalidated before returned evidence is persisted.
- Discovery feed candidate/source-health/outage writes are fenced on the same transaction connection.
- Paper entries, skipped/blocked outcomes and exit fills assert the global fence on the same connection as the paper-ledger mutation.
- A worker that loses its lease stops. It does not mark the stale attempt successful or failed; the next valid lease owner performs bounded recovery.
- The paper stage has no external trading side effect. It is simulation-only.

## Bounds, deadline and idempotency

- One orchestration attempt per scheduler invocation.
- Stable idempotency key per scheduled analysis window and pipeline version.
- Identical key and payload is a no-op; a changed payload under the same key is rejected.
- Stage caps: discovery 25, enrichment 25, market refresh 25, risk 5, consensus 10, paper 25.
- Default cooperative item budget: 150, covering the full maximum bounded path.
- Default runtime: 180 seconds; the global lease TTL is 240 seconds. Per-row leases remain independently enforced.
- Retries are deferred to later scheduler invocations; there is no in-process retry loop.
- The pipeline returns a sanitized aggregate stage report. Credentials and raw provider response bodies are not included.

## Paper-only safety

Paper processing is disabled unless `paper_enabled=True` is explicitly supplied. When enabled, it may call only the existing `PaperBroker` ledger. Paper ledger mutations use caller-owned transactions so the global fence assertion and ledger write commit or roll back together. `LIVE_TRADING` must remain false. Signer, transaction builder, RPC submission and broadcast boundaries remain absent.

## Validation gates

1. H1.1 shared database adapter and H2 fencing changes are integrated on one branch.
2. Current-head local CI passes.
3. All worker and paper ledger mutation paths use transaction-scoped fencing.
4. Deterministic tests cover stage order, idempotency, stale claims, lease loss, rollback and paper-off default.
5. Live Turso conformance passes against the exact dedicated disposable database.
6. Full one-shot pipeline runs against both SQLite and the dedicated Turso DB.
7. Scheduled-run reporting and recovery are verified before any production cadence is enabled.

## Current non-completions

- H1.2/H2 remote Turso conformance is **not verified by the evidence currently available in this repository**. The earlier H1 workflow used generic `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` secrets and only ran two tests; it has been removed from the clean integration branch. Use only the default-branch H3 workflow with dedicated disposable secrets.
- Full one-shot pipeline execution and recovery against remote Turso remains a separate validation gate. The local SQLite pipeline tests cover stage order, idempotency, paper-off default, builder configuration and optional paper-stage behavior.
- No Cloudflare API/dashboard, domain, DNS, HTTPS, scheduler or production deployment is configured by this phase.
