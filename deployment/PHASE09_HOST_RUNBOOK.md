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

id veltrix-paper >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --gid veltrix-db veltrix-paper
id veltrix-dashboard >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --gid veltrix-db veltrix-dashboard
id veltrix-wallet-observer >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin veltrix-wallet-observer
```

The observer identity is reserved only; no observer service is implemented.
It must not be granted membership in the database or service-secret groups.

## 2. Provision environment files

```sh
install -d -o root -g root -m 0750 /etc/veltrix
install -o root -g veltrix-paper-secrets -m 0640 /dev/null /etc/veltrix/paper.env
install -o root -g veltrix-dashboard-secrets -m 0640 /dev/null /etc/veltrix/dashboard.env
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
stat -c '%U:%G:%a %n' /etc/veltrix/paper.env /etc/veltrix/dashboard.env
```

Expected:
- `root:veltrix-paper-secrets:640`
- `root:veltrix-dashboard-secrets:640`

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
install -o root -g root -m 0644 deployment/systemd/veltrix-paper.service /etc/systemd/system/veltrix-paper.service
install -o root -g root -m 0644 deployment/systemd/veltrix-dashboard.service /etc/systemd/system/veltrix-dashboard.service
systemd-analyze verify /etc/systemd/system/veltrix-paper.service /etc/systemd/system/veltrix-dashboard.service
systemctl daemon-reload
```

Inspect the resolved unit properties before starting either service:

```sh
systemctl cat veltrix-paper.service
systemctl cat veltrix-dashboard.service
```

Confirm the installed paths, service users, environment files, database path,
loopback bind, and sandboxing settings match the actual host layout. Do not
remove `ProtectSystem`, `NoNewPrivileges`, `ReadOnlyPaths`,
`InaccessiblePaths`, or the bounded write-path controls to make startup pass.

## 5. Start paper and dashboard services

Only start the paper runtime if paper operation is intentionally enabled and
the operator has confirmed the provider configuration and database path.
This is simulation/paper execution only; live execution remains disabled.

```sh
systemctl enable --now veltrix-paper.service
systemctl enable --now veltrix-dashboard.service
systemctl --no-pager --full status veltrix-paper.service veltrix-dashboard.service
```

The dashboard must listen only on `127.0.0.1:8765`. If remote access is
required, expose it only through a separately hardened authenticated TLS
reverse proxy. Do not bind the dashboard directly to a public interface.

## 6. Run host isolation verification

From the deployed repository revision, as root:

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
- dashboard unauthenticated request returns HTTP 401;
- dashboard authenticated health request succeeds;
- confirmation that `LIVE_TRADING=false` remains in effect.

## Stop conditions

Stop and leave both services disabled if any identity, mount, permission,
credential-isolation, or egress check fails. Do not work around a failed check
by making the database writable to the dashboard, sharing credentials, opening
broad egress, or enabling live execution.

Phase 09.2 remains incomplete until the actual host evidence is reviewed and
the release gate is explicitly recorded as passed.
