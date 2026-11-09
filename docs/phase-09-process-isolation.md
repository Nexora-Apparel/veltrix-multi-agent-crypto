# Phase 09.2 — Deployment, process, database and provider isolation

## Status and boundary

This document defines the host-level deployment profile for the Phase 09.2
wallet-domain separation. It is not evidence that a production host has already
been configured. The repository currently contains no deployed host inventory,
Docker deployment, or systemd installation record.

The supplied systemd units are hardened deployment templates for the existing
paper runtime and read-only dashboard. A wallet-observation service is not
implemented and must not be started from a placeholder command. No signer
service exists.

## Process identities

Use separate non-login operating-system accounts:

| Process | Unix user | Database access | Network | Signing |
|---|---|---|---|---|
| Paper runtime / paper writers | `veltrix-paper` | Read/write to the single paper pipeline SQLite DB | Approved market, risk and RPC providers | Prohibited |
| Dashboard | `veltrix-dashboard` | Read-only to the same DB | Loopback listener only; expose through an authenticated TLS reverse proxy if remote access is needed | Prohibited |
| Future wallet observer | `veltrix-wallet-observer` | No access to paper DB | Read-only RPC endpoints only | Prohibited |
| Signer | Not deployed | No paper DB access | No general network access; not implemented | Disabled |

Do not run the paper worker, dashboard and future wallet observer under the same
Unix account. Do not grant the dashboard or observer membership in any group
that can write the paper database. The future observer must receive only a
public wallet address, chain/network and read-only RPC configuration. It must
not receive a seed phrase, private key, keypair file, signer token, or writable
paper database path.

## SQLite boundary

The existing Phase 03–07 pipeline deliberately shares one persistent SQLite
file. Keep that canonical file; do not split it into per-worker databases as
part of this change because the existing workers and dashboard depend on the
shared schema and history.

- Place the file under `/var/lib/veltrix/solana_discovery.sqlite3`.
- The paper service owns the database directory and may create/update tables.
- The dashboard service receives a read-only mount of `/var/lib/veltrix`.
- The dashboard's existing `mode=ro` URI and `PRAGMA query_only=ON` remain
  mandatory; filesystem read-only isolation is an independent second layer.
- Use a shared group only to grant read access to the dashboard. The paper
  process remains the only writer identity.
- Use a restrictive umask so database, WAL and SHM files are not world-readable.
- If SQLite WAL mode is enabled, verify that the dashboard can read the
  existing `-wal` and `-shm` sidecars without creating or modifying them.
  Do not change journal mode during deployment without a backup and a
  maintenance window.
- Backups must be created by the writer/backup job, not by the dashboard.
  Never copy only the main database file while a WAL is active.

The systemd dashboard unit applies `ReadOnlyPaths=/var/lib/veltrix` in its
mount namespace. A failed read-only mount or inaccessible database must stop
the service from serving data; do not fall back to a writable connection.

## Provider and network boundary

Provider adapters in this repository are data-ingestion components. They are
not signer adapters and must not be given wallet key material.

- Paper/data workers may use only the configured provider API credentials and
  RPC endpoints required by their enabled data tasks.
- Dashboard has no provider API credentials and no direct provider dependency.
- A future wallet observer may call only explicitly approved read-only RPC
  methods (account/balance and transaction-history reads). It must not call
  transaction submission, simulation-with-write side effects, token approval,
  or signing methods.
- Enforce destination-level egress at the host firewall or cloud network layer.
  Systemd address-family restrictions alone do not enforce destination allowlists.
- Keep provider credentials in separate root-owned environment files with
  mode 0640 and the narrowest service-readable group. Never place credentials
  in unit files, repository files, command-line arguments, logs or dashboard
  responses.
- Never put wallet private keys, seed phrases, mnemonic phrases or keypair JSON
  into any service environment file.

No provider-to-signer bridge is allowed. The signer boundary from Phase 09.3 is
only a contract; there is no implementation or process endpoint to connect to.

## Host provisioning

Create the group and non-login service users using the host's account-management
tools. The intended identities are:

```sh
sudo groupadd --system veltrix-db
sudo groupadd --system veltrix-paper-secrets
sudo groupadd --system veltrix-dashboard-secrets
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --gid veltrix-db veltrix-paper
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --gid veltrix-db veltrix-dashboard
sudo useradd --system --no-create-home --shell /usr/sbin/nologin veltrix-wallet-observer
```

Create `/etc/veltrix` as root-owned (0750). Put provider credentials only in
`/etc/veltrix/paper.env` (0640, readable by root and the paper service's
dedicated credential group) and the dashboard token only in
`/etc/veltrix/dashboard.env` (0640, readable by root and dashboard service).
Do not reuse a single broad environment file across services.

Install the unit templates from `deployment/systemd/` after reviewing paths,
users and Python virtual-environment location for the actual host. Enable the
paper service only when paper trading is intentionally enabled. The dashboard
must remain bound to loopback unless a separately configured TLS reverse proxy
and strong dashboard token are in place.

## Verification checklist

Run these checks on the actual deployment host; CI cannot prove them:

1. `id veltrix-paper`, `id veltrix-dashboard` and `id veltrix-wallet-observer`
   show distinct UIDs.
2. `systemctl show veltrix-paper.service -p User -p Group -p ReadWritePaths`
   shows the paper identity and only the required writable state directory.
3. `systemctl show veltrix-dashboard.service -p User -p Group -p ReadOnlyPaths`
   shows a different identity and a read-only database mount.
4. As the dashboard user, the database and any active WAL/SHM sidecars are
   readable but attempts to create, update, truncate or delete files under the
   database directory fail.
5. Dashboard API remains authenticated, GET-only, and bound to loopback.
6. Provider egress tests prove the dashboard has no provider egress and the
   observer (when implemented) can reach only the approved read-only RPC
   destinations.
7. The observer cannot read the paper environment file or paper database.
8. Static process and filesystem inspection confirms no signer process, private
   key file, seed phrase, signing socket or transaction-submission endpoint is
   present.
9. Restarting any one service does not change the UID, database permissions,
   provider allowlist or emergency-stop state of another service.

Record command output, host identity, deployment revision and timestamp in the
release evidence. Redact hostnames, addresses and credential paths where
necessary; never attach secret values.

## Release gate

Phase 09.2 is not complete until the actual host-level checklist passes and
review evidence is attached. The repository templates and CI tests establish
intended controls only. If no production host exists yet, record deployment
verification as pending and keep the entire system in PAPER mode.
