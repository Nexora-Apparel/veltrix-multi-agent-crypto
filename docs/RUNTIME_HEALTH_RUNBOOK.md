# Veltrix read-only worker runtime deployment runbook

## Purpose

Keep worker-cycle liveness, individual agent evaluations, and provider connectivity as separate telemetry signals. This runbook is for a future supervised read-only worker deployment. It does not enable the GitHub Actions scheduler or trading.

## Current hosting boundary

- Cloudflare Pages serves the dashboard and read-only API; it is not a persistent Python process host.
- `.github/workflows/solana-pipeline-schedule.yml` remains manual-dispatch only and sets `SCHEDULER_ENABLED=false`.
- The existing Python runtime entry points are long-running processes. They must be hosted by a supervised container/process platform to produce continuous cycle heartbeats.
- No worker service is deployed by PR #226.

## Required runtime configuration

Provide these values through the service platform's encrypted environment/secret manager. Never commit real values.

- `TURSO_DATABASE_URL`: production `libsql://*.turso.io` URL for the shared application database.
- `TURSO_AUTH_TOKEN`: least-privilege write token for append-only telemetry and normal worker state changes.
- `SOLANA_RPC_URL` or `HELIUS_API_KEY`: configured Solana read access for the existing risk/data components.
- `HELIUS_API_KEY`: only if Helius wallet evidence is enabled.
- `TRUSTED_WALLET_PROVIDERS`: must explicitly include `helius` before Helius wallet evidence is enabled.
- `VELTRIX_TRUSTED_SOLANA_WALLETS`: explicit allowlist; never infer trusted wallets from arbitrary activity.
- `VELTRIX_AGENT_HEARTBEAT_MAX_AGE_MS` and `VELTRIX_PROVIDER_PROBE_MAX_AGE_MS`: optional bounded freshness windows; default 300000 ms.
- `VELTRIX_MARKET_REFRESH_INTERVAL_MS`, `VELTRIX_MARKET_INTELLIGENCE_POLL_SECONDS`, `VELTRIX_CONSENSUS_POLL_SECONDS`: existing worker cadence controls.

Keep `LIVE_TRADING=false`, `PAPER_TRADING_ENABLED=false`, and `SCHEDULER_ENABLED=false`. Do not provide private keys, seed phrases, wallet signing credentials or transaction submission credentials.

## Existing read-only process entry points

Run as separate supervised processes with restart-on-failure and graceful SIGTERM:

- Discovery: `python -m veltrix.solana_meme.worker`
- Token intelligence and market refresh: `python -m veltrix.solana_meme.intelligence_worker`
- Multi-agent consensus: `python -m veltrix.solana_meme.consensus_runtime`

Each process must use the same production Turso database. The discovery worker may require `VELTRIX_PUMPPORTAL_WS_ENDPOINT`. Consensus wallet evidence remains disabled unless the explicit Helius and wallet allowlist requirements above are met.

Do not start multiple competing copies of the same logical worker until lease/claim ownership and service replica limits have been reviewed. Configure one replica per worker role initially.

## Health acceptance gates

After service deployment, verify all of the following from the real production database and authenticated dashboard API:

1. `WORKER_HEARTBEAT` events advance for `solana-token-intelligence`, `solana-market-refresh`, and `solana-consensus` even when no candidates are due.
2. `AGENT_HEARTBEAT` events advance only when the corresponding agent actually completes a vote during a consensus evaluation. An idle agent is not fabricated as healthy.
3. `PROVIDER_PROBE` events advance at the configured interval even when the market-refresh queue is empty.
4. Failed probes are visible as FAILED with a sanitized error code; successful HTTP connectivity is not treated as proof of token-pair availability or safety.
5. Worker status becomes STALE after the configured freshness window when a process stops; restart recovery is visible without editing or deleting historical events.
6. The dashboard's worker, agent and provider ages reconcile with the same database snapshot.

## Rollback

Stop the relevant supervised process or roll back its container image. Do not delete telemetry events, clear queues, reset dead jobs, fabricate heartbeats, or relax data-freshness and safety gates. Trading and scheduler flags remain disabled throughout.


## Portable worker image

Build the shared image from the repository root:

```sh
docker build -f Dockerfile.worker -t veltrix-readonly-worker:latest .
```

Create one supervised service per role using the same image and override its command:

- Discovery: `python -m veltrix.solana_meme.worker`
- Token intelligence: `python -m veltrix.solana_meme.intelligence_worker`
- Consensus: `python -m veltrix.solana_meme.consensus_runtime`

Inject the required environment variables from the platform secret manager. Do not put secrets in Docker build arguments, image layers, source control or service command strings. Start with one replica per role. Configure automatic restart on process failure and graceful termination.


## Host-agnostic Docker Compose deployment

The repository includes `compose.worker.yaml` and a non-secret `.env.worker.example` template. This removes the need to manually compose three different container commands.

1. Copy `.env.worker.example` to `.env.worker` on the host and populate the production Turso URL/token. Keep the file outside source control.
2. Build and start one replica for each role:

```sh
docker compose -f compose.worker.yaml build
docker compose -f compose.worker.yaml up -d
docker compose -f compose.worker.yaml ps
```

3. Inspect service logs without printing environment values:

```sh
docker compose -f compose.worker.yaml logs --tail=100 discovery token-intelligence consensus
```

4. Stop the read-only services:

```sh
docker compose -f compose.worker.yaml down
```

The Compose stack has no published ports, drops Linux capabilities, uses a read-only root filesystem, runs as the non-root user baked into the image, and restarts failed processes. It does not provide wallet signing, swap submission, scheduler activation, or trading credentials.

A host with Docker and network access to the configured Turso endpoint is still required. Do not start the stack until the production secret values have been installed in the host-side `.env.worker` file and the host has been selected.
