# Continuous pipeline runtime

## Runtime contract

The continuous supervisor is `python -m veltrix.solana_meme.pipeline_runtime`.
It invokes the bounded `OneShotSolanaPipeline` once per UTC interval. Every
cycle retains the global orchestration lease, per-item fencing, persisted
idempotency, and fixed stage order. A restart in the same interval reuses the
same idempotency key and cannot replay a successful cycle.

The worker writes `solana_pipeline_health` in the shared database. The
read-only dashboard `GET /api/health` exposes the last status, timestamps,
duration, job id, error summary, and consecutive failure count. A failed cycle
is logged, persisted as degraded, and retried on the next interval; SIGTERM
stops cleanly. Database health persistence is best-effort if the database
itself is unavailable.

## Configuration

- `VELTRIX_SOLANA_DISCOVERY_DB`: shared SQLite path or approved Turso URL.
- `TURSO_AUTH_TOKEN`: required only when the shared DB URL is Turso.
- `VELTRIX_PIPELINE_POLL_SECONDS`: 15–3600 seconds; default 60.
- `PAPER_TRADING_ENABLED`: default false. Set true only for the USD paper
  simulation; `LIVE_TRADING=true` is rejected.
- `SOL_PAPER_TRADING_ENABLED`: default false. Set true instead of the USD
  flag to run the native lamport-denominated SOL simulation.
- `PAPER_BALANCE_SOL`: starting simulated SOL balance; default 1.0 SOL. It
  initializes the wallet once and never resets an existing ledger.
- `VELTRIX_CANDLE_INTERVAL`: supported candle interval; default 1m.

The dashboard remains read-only. Do not expose the dashboard without its
existing authentication token and HTTPS termination.

## systemd installation

Copy `deployment/systemd/veltrix-pipeline.service` to
`/etc/systemd/system/`, create the restricted `/etc/veltrix/pipeline.env`
file, then run:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now veltrix-pipeline.service
sudo systemctl status veltrix-pipeline.service
journalctl -u veltrix-pipeline.service -f
```

The service is a host deployment target, not a free serverless runtime. For
Cloudflare deployment, keep the Python worker on a supported execution host
and place Cloudflare Workers/Pages in front of the authenticated read-only API;
do not attempt to run the SQLite-backed Python worker inside a request-scoped
edge function. Persist the ledger and orchestration state in one supported
Turso database and configure TLS/custom-domain routing at Cloudflare.

## Acceptance gates

1. `pytest -q tests/test_solana_meme_pipeline_runtime.py tests/test_solana_meme_pipeline.py`
2. `GET /api/health` reports `worker.healthy=true` after a successful cycle.
3. A forced provider/database failure increments consecutive failures and the
   next successful cycle resets the count.
4. Restarting the worker inside one interval does not duplicate a completed job.
5. Chart candles remain provider-sourced and are marked stale/unavailable when
   observations exceed the freshness threshold.
6. Paper tests use the existing simulated USD ledger; no wallet signer, RPC
   transaction submission, or live execution endpoint is present.
