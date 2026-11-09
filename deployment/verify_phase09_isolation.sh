#!/usr/bin/env bash
# Read-only verification of the installed Phase 09 systemd isolation profile.
# Run as root after installing and starting both service units.
set -Eeuo pipefail

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }
require() { command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in systemctl getent id stat runuser nsenter; do require "$command"; done

DB="${VELTRIX_SOLANA_DISCOVERY_DB:-/var/lib/veltrix/solana_discovery.sqlite3}"
PAPER_ENV=/etc/veltrix/paper.env
DASHBOARD_ENV=/etc/veltrix/dashboard.env
PAPER_UNIT=veltrix-paper.service
DASHBOARD_UNIT=veltrix-dashboard.service

for user in veltrix-paper veltrix-dashboard veltrix-wallet-observer; do
  getent passwd "$user" >/dev/null || fail "missing service account: $user"
done
paper_uid="$(id -u veltrix-paper)"
dashboard_uid="$(id -u veltrix-dashboard)"
observer_uid="$(id -u veltrix-wallet-observer)"
[[ "$paper_uid" != "$dashboard_uid" && "$paper_uid" != "$observer_uid" && "$dashboard_uid" != "$observer_uid" ]] \
  || fail "paper, dashboard and observer UIDs must all be distinct"
pass "three distinct service UIDs exist"

unit_value() { systemctl show "$1" --property="$2" --value; }
[[ "$(unit_value "$PAPER_UNIT" User)" == veltrix-paper ]] || fail "paper unit User mismatch"
[[ "$(unit_value "$PAPER_UNIT" Group)" == veltrix-db ]] || fail "paper unit Group mismatch"
[[ "$(unit_value "$DASHBOARD_UNIT" User)" == veltrix-dashboard ]] || fail "dashboard unit User mismatch"
[[ "$(unit_value "$DASHBOARD_UNIT" Group)" == veltrix-db ]] || fail "dashboard unit Group mismatch"
paper_rw="$(unit_value "$PAPER_UNIT" ReadWritePaths)"
dashboard_ro="$(unit_value "$DASHBOARD_UNIT" ReadOnlyPaths)"
[[ "$paper_rw" == *"/var/lib/veltrix"* ]] || fail "paper writable state path missing"
[[ "$dashboard_ro" == *"/var/lib/veltrix"* ]] || fail "dashboard read-only state path missing"
pass "systemd identities and database mount policies match"

[[ -f "$DB" ]] || fail "database file not found: $DB"
[[ -f "$PAPER_ENV" && -f "$DASHBOARD_ENV" ]] || fail "service environment files are missing"
[[ "$(stat -c '%U:%G:%a' "$PAPER_ENV")" == "root:veltrix-paper-secrets:640" ]] \
  || fail "paper.env must be root:veltrix-paper-secrets mode 0640"
[[ "$(stat -c '%U:%G:%a' "$DASHBOARD_ENV")" == "root:veltrix-dashboard-secrets:640" ]] \
  || fail "dashboard.env must be root:veltrix-dashboard-secrets mode 0640"
pass "environment files have separate root-owned credential groups"

paper_pid="$(unit_value "$PAPER_UNIT" MainPID)"
dashboard_pid="$(unit_value "$DASHBOARD_UNIT" MainPID)"
[[ "$paper_pid" =~ ^[1-9][0-9]*$ && "$dashboard_pid" =~ ^[1-9][0-9]*$ ]] \
  || fail "both services must be running for namespace checks"

# Check access from inside the dashboard's actual systemd mount namespace.
nsenter -t "$dashboard_pid" -m -- runuser -u veltrix-dashboard -- test -r "$DB" \
  || fail "dashboard cannot read the shared database"
nsenter -t "$dashboard_pid" -m -- runuser -u veltrix-dashboard -- test ! -w "$DB" \
  || fail "dashboard can write the database file"
nsenter -t "$dashboard_pid" -m -- runuser -u veltrix-dashboard -- test ! -w "$(dirname "$DB")" \
  || fail "dashboard can write the database directory"
for suffix in -wal -shm; do
  if [[ -e "$DB$suffix" ]]; then
    nsenter -t "$dashboard_pid" -m -- runuser -u veltrix-dashboard -- test -r "$DB$suffix" \
      || fail "dashboard cannot read SQLite sidecar $suffix"
    nsenter -t "$dashboard_pid" -m -- runuser -u veltrix-dashboard -- test ! -w "$DB$suffix" \
      || fail "dashboard can write SQLite sidecar $suffix"
  fi
done
pass "dashboard database and existing SQLite sidecars are readable but not writable"

nsenter -t "$dashboard_pid" -m -- runuser -u veltrix-dashboard -- test ! -r "$PAPER_ENV" \
  || fail "dashboard can read paper provider credentials"
nsenter -t "$paper_pid" -m -- runuser -u veltrix-paper -- test ! -r "$DASHBOARD_ENV" \
  || fail "paper worker can read dashboard credentials"
pass "service processes cannot read each other's credential files"

[[ "$(unit_value "$DASHBOARD_UNIT" Environment)" == *"VELTRIX_DASHBOARD_HOST=127.0.0.1"* ]] \
  || fail "dashboard is not explicitly loopback-bound"
pass "dashboard loopback binding is configured"

printf '\nMANUAL REQUIRED: verify host/cloud egress firewall rules.\n'
printf 'Dashboard: no provider egress. Wallet observer: approved read-only RPC destinations only.\n'
printf 'This script does not prove firewall policy, provider behavior, or signer isolation.\n'
