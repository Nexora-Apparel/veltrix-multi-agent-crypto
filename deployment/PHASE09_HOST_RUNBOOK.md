# Phase 09.2 — host deployment runbook

## Purpose and safety boundary

This runbook installs the repository's paper-runtime and read-only dashboard
systemd templates so the host-isolation verifier can inspect the real process
and filesystem boundaries. It does not deploy a wallet observer or signer.
It does not enable live trading, sign transactions, or submit transactions.

Do not run this on a host until its operator has confirmed that it is the
intended VELTRIX host, has a current recoverable database backup, and has an
approved provider-egress policy. Never paste secrets, private keys, seed
phrases, dashboard tokens, or raw environment files into a chat or issue.

## 0. Read-only host preflight

Before provisioning users, changing permissions, installing units, or starting
services, run the compatibility inventory from the checked-out release:

```sh
bash deployment/phase09_host_preflight.sh
```

The script reports the Linux/systemd environment, expected application,
virtual-environment and database paths, service account/group presence, and
Python availability. It does not read environment-file contents or mutate
the host. A non-Linux host or missing systemd is a hard stop. Missing paths,
users or groups are expected on a fresh host but must be reconciled before
continuing. This preflight is not an isolation proof and does not replace the
root-only verifier in step 6.

For non-default installation paths, set `VELTRIX_APP_DIR`,
`VELTRIX_VENV_DIR`, and `VELTRIX_DB_PATH` for the preflight. The deployed
systemd templates and verifier must be reviewed and adjusted through a
separately tested deployment change before using non-default paths; do not
assume these variables reconfigure the services.

## Preconditions

- Linux host using systemd, with root/sudo access.
- Application release installed at `/opt/veltrix/app`.
- Python environment installed at `/opt/veltrix/venv`.
- The deployed source includes the Phase 09.2 branch commit being validated.
- Canonical SQLite database is `/var/lib/veltrix/solana_discovery.sqlite3`.
- A tested backup exists. If SQLite WAL is active, use SQLite's online backup
  mechanism or a controlled maintenance procedure; do not copy only the main
  database file.
- Provider egress allowlist has been designed for the actual providers and
  endpoints configured on this host. This runbook intentionally does not guess
  provider IP ranges or domains.

## 1. Provision service identities

Run as root. These commands are idempotent for existing groups and users:

```sh
getent group veltrix-db >/dev/null || groupadd --system veltrix-db
getent group veltrix-paper-secrets >/dev/null || groupadd --system veltrix-paper-secrets
getent group veltrix-dashboard-secrets >/dev/null || groupadd --system veltrix-dashboard-secrets
getent group veltrix-worker-secrets >/dev/null || groupadd --system veltrix-worker-secrets

id veltrix-paper >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --gid veltrix-db veltrix-paper
id veltrix-dashboard >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --gid veltrix-db veltrix-dashboard
id veltrix-worker >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --gid veltrix-db veltrix-worker
id veltrix-wallet-observer >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin veltrix-wallet-observer
```

The observer identity is reserved only; no observer service is implemented.
It must not be granted membership in the database or service-secret groups.

## 2. Provision environment files

```sh
install -d -o root -g root -m 0750 /etc/veltrix
install -o root -g veltrix-paper-secrets -m 0640 /dev/null /etc/veltrix/paper.env
install -o root -g veltrix-dashboard-secrets -m 0640 /dev/null /etc/veltrix/dashboard.env
install -o root -g veltrix-worker-secrets -m 0640 /dev/null /etc/veltrix/pipeline.env
```

Populate `paper.env` with only the provider credentials and configuration
required by the deployed paper data pipeline. Do not add any wallet key,
seed phrase, mnemonic, signer credential, or dashboard token.

Set `VELTRIX_DASHBOARD_TOKEN` in `dashboard.env` to a randomly generated
secret of at least 32 characters. Generate and store it using the host
operator's approved secret-management procedure. Do not print it into logs,
commit it, place it in a URL, or send it through chat.

Verify ownership and modes without printing file contents:

```sh
stat -c '%U:%G:%a %n' /etc/veltrix/paper.env /etc/veltrix/dashboard.env /etc/veltrix/pipeline.env
```

Expected:
- `root:veltrix-paper-secrets:640`
- `root:veltrix-dashboard-secrets:640`
- `root:veltrix-worker-secrets:640`

## 3. Verify application and database paths

Confirm the service users can traverse and read the immutable application
release and Python environment. Do not grant them write access to application
code or the virtual environment.

Confirm the canonical database exists and the paper service is its only writer.
For an existing database, preserve its current contents and use the backup and
maintenance process before changing ownership or permissions. The paper
service's `StateDirectory=veltrix` manages the state directory when systemd
creates it; reconcile an existing directory deliberately rather than applying
a blind recursive ownership change.

## 4. Install and validate systemd templates

From the checked-out repository revision:

```sh
install -o root -g root -m 0644 deployment/systemd/veltrix-pipeline.service /etc/systemd/system/veltrix-pipeline.service
install -o root -g root -m 0644 deployment/systemd/veltrix-paper.service /etc/systemd/system/veltrix-paper.service
install -o root -g root -m 0644 deployment/systemd/veltrix-dashboard.service /etc/systemd/system/veltrix-dashboard.service
systemd-analyze verify /etc/systemd/system/veltrix-pipeline.service /etc/systemd/system/veltrix-paper.service /etc/systemd/system/veltrix-dashboard.service
systemctl daemon-reload
```

Inspect the resolved unit properties before starting either service:

```sh
systemctl cat veltrix-pipeline.service
systemctl cat veltrix-paper.service
systemctl cat veltrix-dashboard.service
```

Confirm the installed paths, service users, environment files, database path,
loopback bind, and sandboxing settings match the actual host layout. Do not
remove `ProtectSystem`, `NoNewPrivileges`, `ReadOnlyPaths`,
`InaccessiblePaths`, or the bounded write-path controls to make startup pass.

The fenced intelligence pipeline is the upstream producer for discovery,
market snapshots, OHLCV candles, risk evidence and consensus. Start it before
the dashboard is used for live monitoring:

```sh
systemctl enable --now veltrix-pipeline.service
systemctl status veltrix-pipeline.service
journalctl -u veltrix-pipeline.service -f
```

Check authenticated `GET /api/health` for `worker.status=healthy` and a
recent `last_success_at`. The dashboard process is read-only and must not
write to the ledger. The pipeline service and paper stage share the same
persistent database; keep the explicit paper opt-in disabled until the
simulation test gates have passed.

## 5. Start the continuous pipeline and dashboard services

The pipeline supervisor owns discovery, enrichment, market refresh, candle
refresh, risk, consensus and the explicitly selected paper ledger. Start the
pipeline and dashboard services:

```sh
systemctl enable --now veltrix-pipeline.service
systemctl enable --now veltrix-dashboard.service
systemctl --no-pager --full status veltrix-pipeline.service veltrix-dashboard.service
journalctl -u veltrix-pipeline.service -f
```

Set exactly one of `PAPER_TRADING_ENABLED=true` or
`SOL_PAPER_TRADING_ENABLED=true` in the protected `pipeline.env` only after
the corresponding simulation acceptance tests pass. Both default to false.
Never enable both. Keep `LIVE_TRADING=false`.

The standalone `veltrix-paper.service` is a legacy separate paper runtime.
It must remain disabled when the pipeline supervisor is active; running both
can create duplicate entries against separate ledgers and invalidate results.
The pipeline's own paper stage is the supported continuous execution path.

The dashboard must listen only on `127.0.0.1:8765`. If remote access is
required, expose it only through the Cloudflare authenticated HTTPS edge or a
separately hardened authenticated TLS reverse proxy. Do not bind the dashboard
directly to a public interface.

### Publish the API through Cloudflare Tunnel

The supported Cloudflare path keeps the Python API loopback-only and runs a
separate unprivileged `cloudflared` service. Follow
`docs/cloudflare-tunnel-origin.md` to create the named tunnel, install its
credentials file and configure the public API hostname with an Access
**Service Auth** policy. The tunnel user must not belong to the database-write
group or read pipeline secrets.

Install the example unit only after replacing the tunnel UUID and hostname in
`/etc/cloudflared/config.yml` and installing the credentials JSON with
`root:cloudflared` ownership and mode `0640`:

```sh
useradd --system --home /var/lib/cloudflared --shell /usr/sbin/nologin cloudflared
install -o root -g cloudflared -m 0640 deployment/cloudflared/config.yml.example /etc/cloudflared/config.yml
install -o root -g root -m 0644 deployment/systemd/cloudflared-veltrix.service /etc/systemd/system/cloudflared-veltrix.service
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
systemd-analyze verify /etc/systemd/system/cloudflared-veltrix.service
systemctl daemon-reload
systemctl enable --now cloudflared-veltrix.service
systemctl status cloudflared-veltrix.service
```

The copied config must contain the actual UUID and API hostname; do not deploy
the example placeholders. Keep port 8765 closed to inbound network traffic.

## 6. Run host isolation verification

From the deployed repository revision, as root, after the pipeline,
dashboard and Cloudflare Tunnel services are installed and active:

```sh
bash deployment/verify_phase09_isolation.sh
```

The verifier must pass distinct service UIDs, active units, effective process
UID/GID checks, supplementary group boundaries, dashboard read-only access to
the database and existing WAL/SHM sidecars from its real mount namespace, and
cross-service environment-file isolation.

The script is intentionally read-only. It does not repair host state.

## 7. Verify provider egress independently

The verifier prints a manual firewall gate. Use the host/cloud network
administrator's approved test procedure to prove:

- Dashboard process: no outbound provider or public-network egress.
- Paper process: outbound traffic only to the approved data-provider and RPC
  destinations required by the enabled pipeline.
- Observer identity (not implemented): no paper database or secret-group
  access; if later deployed, read-only RPC destinations only.
- No signer service, signer socket, or transaction-submission endpoint exists.

Record the effective firewall rules and test outcomes, not secret values.
Systemd address-family restrictions are not destination allowlists. Do not
declare this gate passed based only on unit configuration or DNS resolution.

## 8. Evidence package

Record the following in the release evidence, with secrets and sensitive host
details redacted:

- host OS/systemd version and deployment revision;
- UTC timestamp and operator identity;
- `systemd-analyze verify` result;
- active unit status and resolved security-relevant unit properties;
- complete output of `verify_phase09_isolation.sh`;
- database ownership/mode and whether WAL/SHM sidecars were present;
- firewall policy reference and observed egress test results;
- dashboard unauthenticated request returns HTTP 401;\n- pipeline authenticated health reports a recent successful fenced cycle;
- dashboard authenticated health request succeeds;
- confirmation that `LIVE_TRADING=false` remains in effect.

## Stop conditions

Stop and leave both services disabled if any identity, mount, permission,
credential-isolation, or egress check fails. Do not work around a failed check
by making the database writable to the dashboard, sharing credentials, opening
broad egress, or enabling live execution.

Phase 09.2 remains incomplete until the actual host evidence is reviewed and
the release gate is explicitly recorded as passed.
