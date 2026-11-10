#!/usr/bin/env bash
# Read-only verification of the installed Phase 09 systemd isolation profile.
# Run as root after installing and starting both service units.
set -Eeuo pipefail

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }
require() { command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in systemctl getent id stat runuser nsenter awk cut findmnt readlink; do require "$command"; done

DB="${VELTRIX_SOLANA_DISCOVERY_DB:-/var/lib/veltrix/solana_discovery.sqlite3}"
PIPELINE_ENV=/etc/veltrix/pipeline.env
DASHBOARD_ENV=/etc/veltrix/dashboard.env
PIPELINE_UNIT=veltrix-pipeline.service
PAPER_UNIT=veltrix-paper.service
DASHBOARD_UNIT=veltrix-dashboard.service

for user in veltrix-worker veltrix-dashboard veltrix-wallet-observer; do
  getent passwd "$user" >/dev/null || fail "missing service account: $user"
done
pipeline_uid="$(id -u veltrix-worker)"
dashboard_uid="$(id -u veltrix-dashboard)"
observer_uid="$(id -u veltrix-wallet-observer)"
[[ "$pipeline_uid" != "$dashboard_uid" && "$pipeline_uid" != "$observer_uid" && "$dashboard_uid" != "$observer_uid" ]] \
  || fail "pipeline, dashboard and observer UIDs must all be distinct"
pass "pipeline, dashboard and observer use distinct service UIDs"

unit_value() { systemctl show "$1" --property="$2" --value; }
for unit in "$PIPELINE_UNIT" "$DASHBOARD_UNIT"; do
  systemctl is-active --quiet "$unit" || fail "$unit must be active"
done
if systemctl is-active --quiet "$PAPER_UNIT"; then
  fail "standalone paper service must be inactive; paper stages run inside the fenced pipeline"
fi
[[ "$(unit_value "$PIPELINE_UNIT" User)" == veltrix-worker ]] || fail "pipeline unit User mismatch"
[[ "$(unit_value "$PIPELINE_UNIT" Group)" == veltrix-db ]] || fail "pipeline unit Group mismatch"
[[ "$(unit_value "$DASHBOARD_UNIT" User)" == veltrix-dashboard ]] || fail "dashboard unit User mismatch"
[[ "$(unit_value "$DASHBOARD_UNIT" Group)" == veltrix-db ]] || fail "dashboard unit Group mismatch"
pipeline_rw="$(unit_value "$PIPELINE_UNIT" ReadWritePaths)"
dashboard_ro="$(unit_value "$DASHBOARD_UNIT" ReadOnlyPaths)"
[[ "$pipeline_rw" == *"/var/lib/veltrix"* ]] || fail "pipeline writable state path missing"
[[ "$dashboard_ro" == *"/var/lib/veltrix"* ]] || fail "dashboard read-only state path missing"
pass "pipeline/dashboard identities and database mount policies match"

# Confirm effective credentials of the running processes, not only unit config.
pipeline_pid="$(unit_value "$PIPELINE_UNIT" MainPID)"
dashboard_pid="$(unit_value "$DASHBOARD_UNIT" MainPID)"
[[ "$pipeline_pid" =~ ^[1-9][0-9]*$ && "$dashboard_pid" =~ ^[1-9][0-9]*$ ]] \
  || fail "pipeline and dashboard processes must be running for identity checks"
proc_id() {
  local pid="$1" field="$2"
  awk -v field="$field" '$1 == field ":" { print $3; found=1 } END { if (!found) exit 1 }' "/proc/$pid/status"
}
[[ "$(proc_id "$pipeline_pid" Uid)" == "$pipeline_uid" ]] || fail "pipeline process effective UID mismatch"
[[ "$(proc_id "$dashboard_pid" Uid)" == "$dashboard_uid" ]] || fail "dashboard process effective UID mismatch"
db_gid="$(getent group veltrix-db | cut -d: -f3)"
pipeline_secrets_gid="$(getent group veltrix-worker-secrets | cut -d: -f3)"
dashboard_secrets_gid="$(getent group veltrix-dashboard-secrets | cut -d: -f3)"
[[ "$(proc_id "$pipeline_pid" Gid)" == "$db_gid" ]] || fail "pipeline process effective GID mismatch"
[[ "$(proc_id "$dashboard_pid" Gid)" == "$db_gid" ]] || fail "dashboard process effective GID mismatch"
proc_has_group() {
  local pid="$1" gid="$2"
  awk -v gid="$gid" '$1 == "Groups:" { for (i=2; i<=NF; i++) if ($i == gid) found=1 } END { exit !found }' "/proc/$pid/status"
}
proc_has_group "$pipeline_pid" "$pipeline_secrets_gid" || fail "pipeline process lacks provider credential group"
! proc_has_group "$pipeline_pid" "$dashboard_secrets_gid" || fail "pipeline process has dashboard credential access"
! proc_has_group "$dashboard_pid" "$pipeline_secrets_gid" || fail "dashboard process has provider credential access"
proc_has_group "$dashboard_pid" "$dashboard_secrets_gid" || fail "dashboard process lacks dashboard credential group"
observer_groups="$(id -G veltrix-wallet-observer)"
for forbidden_gid in "$db_gid" "$pipeline_secrets_gid" "$dashboard_secrets_gid"; do
  for observer_gid in $observer_groups; do
    [[ "$observer_gid" != "$forbidden_gid" ]] || fail "observer belongs to a paper/dashboard data or secret group"
  done
done
pass "running process UIDs, GIDs and supplementary group boundaries match"

REMOTE_DB=false
if [[ "$DB" == libsql://* ]]; then
  REMOTE_DB=true
  pass "remote Turso database URL selected; no local database file is expected"
else
  [[ -f "$DB" ]] || fail "database file not found: $DB"
fi
[[ -f "$PIPELINE_ENV" && -f "$DASHBOARD_ENV" ]] || fail "pipeline/dashboard environment files are missing"
[[ "$(stat -c '%U:%G:%a' "$PIPELINE_ENV")" == "root:veltrix-worker-secrets:640" ]] \
  || fail "pipeline.env must be root:veltrix-worker-secrets mode 0640"
[[ "$(stat -c '%U:%G:%a' "$DASHBOARD_ENV")" == "root:veltrix-dashboard-secrets:640" ]] \
  || fail "dashboard.env must be root:veltrix-dashboard-secrets mode 0640"
pass "pipeline and dashboard environment files have separate root-owned credential groups"

pipeline_pid="$(unit_value "$PIPELINE_UNIT" MainPID)"
dashboard_pid="$(unit_value "$DASHBOARD_UNIT" MainPID)"
[[ "$pipeline_pid" =~ ^[1-9][0-9]*$ && "$dashboard_pid" =~ ^[1-9][0-9]*$ ]] \
  || fail "pipeline and dashboard must be running for namespace checks"

# Prove the services run in distinct mount namespaces and that the kernel
# reports the database mount writable for paper but read-only for dashboard.
host_mount_ns="$(readlink /proc/1/ns/mnt)"
pipeline_mount_ns="$(readlink "/proc/$pipeline_pid/ns/mnt")"
dashboard_mount_ns="$(readlink "/proc/$dashboard_pid/ns/mnt")"
[[ "$pipeline_mount_ns" != "$host_mount_ns" ]] || fail "pipeline service shares the host mount namespace"
[[ "$dashboard_mount_ns" != "$host_mount_ns" ]] || fail "dashboard service shares the host mount namespace"
[[ "$pipeline_mount_ns" != "$dashboard_mount_ns" ]] || fail "pipeline and dashboard share a mount namespace"
pass "pipeline and dashboard use distinct non-host mount namespaces"

if [[ "$REMOTE_DB" == false ]]; then
  pipeline_mount_options="$(nsenter -t "$pipeline_pid" -m -- findmnt --noheadings --output OPTIONS --target "$DB")"
  dashboard_mount_options="$(nsenter -t "$dashboard_pid" -m -- findmnt --noheadings --output OPTIONS --target "$DB")"
  case ",$pipeline_mount_options," in *,rw,*) ;; *) fail "pipeline database mount is not effectively read-write: $pipeline_mount_options" ;; esac
  case ",$dashboard_mount_options," in *,ro,*) ;; *) fail "dashboard database mount is not effectively read-only: $dashboard_mount_options" ;; esac
  pass "kernel mount options confirm pipeline=rw and dashboard=ro"
  
  # Check access from inside the dashboard's actual systemd mount namespace.
  nsenter -t "$pipeline_pid" -m -- runuser -u veltrix-worker -- test -r "$DB" \
    || fail "pipeline cannot read the shared database"
  nsenter -t "$pipeline_pid" -m -- runuser -u veltrix-worker -- test -w "$DB" \
    || fail "pipeline cannot write the shared database"
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
  pass "pipeline has database write access; dashboard and existing SQLite sidecars are read-only"
  
  nsenter -t "$dashboard_pid" -m -- runuser -u veltrix-dashboard -- test ! -r "$PIPELINE_ENV" \
    || fail "dashboard can read provider credentials"
  nsenter -t "$pipeline_pid" -m -- runuser -u veltrix-worker -- test ! -r "$DASHBOARD_ENV" \
    || fail "pipeline can read dashboard API credentials"
  else
  pass "remote Turso database selected; authenticated remote DB conformance verifies dashboard query-only access"
fi

pass "pipeline and dashboard cannot read each other's credential files"

[[ "$(unit_value "$DASHBOARD_UNIT" Environment)" == *"VELTRIX_DASHBOARD_HOST=127.0.0.1"* ]] \
  || fail "dashboard is not explicitly loopback-bound"
pass "dashboard loopback binding is configured"

printf '\nMANUAL REQUIRED: verify host/cloud egress firewall rules.\n'
printf 'Dashboard: no provider egress. Pipeline: approved provider/RPC egress only.\n'
printf 'Wallet observer: approved read-only RPC destinations only when deployed.\n'
printf 'This script does not prove firewall policy, provider behavior, or signer isolation.\n'
