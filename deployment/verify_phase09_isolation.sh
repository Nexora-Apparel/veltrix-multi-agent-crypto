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
for unit in "$PAPER_UNIT" "$DASHBOARD_UNIT"; do
  systemctl is-active --quiet "$unit" || fail "$unit must be active"
done
[[ "$(unit_value "$PAPER_UNIT" User)" == veltrix-paper ]] || fail "paper unit User mismatch"
[[ "$(unit_value "$PAPER_UNIT" Group)" == veltrix-db ]] || fail "paper unit Group mismatch"
[[ "$(unit_value "$DASHBOARD_UNIT" User)" == veltrix-dashboard ]] || fail "dashboard unit User mismatch"
[[ "$(unit_value "$DASHBOARD_UNIT" Group)" == veltrix-db ]] || fail "dashboard unit Group mismatch"
paper_rw="$(unit_value "$PAPER_UNIT" ReadWritePaths)"
dashboard_ro="$(unit_value "$DASHBOARD_UNIT" ReadOnlyPaths)"
[[ "$paper_rw" == *"/var/lib/veltrix"* ]] || fail "paper writable state path missing"
[[ "$dashboard_ro" == *"/var/lib/veltrix"* ]] || fail "dashboard read-only state path missing"
# Inspect the effective systemd properties, not only the unit files. Fail
# closed if a host's systemd does not expose the expected sandbox settings.
for unit in "$PAPER_UNIT" "$DASHBOARD_UNIT"; do
  for property in NoNewPrivileges PrivateTmp ProtectHome ProtectKernelTunables ProtectKernelModules ProtectControlGroups RestrictSUIDSGID LockPersonality; do
    [[ "$(unit_value "$unit" "$property")" == yes ]] || fail "$unit must enforce $property=yes"
  done
  [[ "$(unit_value "$unit" ProtectSystem)" == strict ]] || fail "$unit must enforce ProtectSystem=strict"
  [[ -z "$(unit_value "$unit" CapabilityBoundingSet)" ]] || fail "$unit must have an empty capability bounding set"
  [[ -z "$(unit_value "$unit" AmbientCapabilities)" ]] || fail "$unit must have no ambient capabilities"
  [[ "$(unit_value "$unit" RestrictAddressFamilies)" == "AF_UNIX AF_INET AF_INET6" ]] || fail "$unit address-family restriction mismatch"
done
[[ "$(unit_value "$PAPER_UNIT" Environment)" == *"LIVE_TRADING=false"* ]] || fail "paper unit must explicitly set LIVE_TRADING=false"
[[ "$(unit_value "$PAPER_UNIT" Environment)" == *"PAPER_TRADING_ENABLED=true"* ]] || fail "paper unit must explicitly enable paper-only processing"
pass "effective systemd sandbox, capability, address-family and paper-only properties match"

pass "systemd identities and database mount policies match"

# Confirm effective credentials of the running processes, not only unit config.
paper_pid="$(unit_value "$PAPER_UNIT" MainPID)"
dashboard_pid="$(unit_value "$DASHBOARD_UNIT" MainPID)"
[[ "$paper_pid" =~ ^[1-9][0-9]*$ && "$dashboard_pid" =~ ^[1-9][0-9]*$ ]] \
  || fail "both services must be running for process identity checks"
proc_id() {
  local pid="$1" field="$2"
  # /proc/PID/status lines begin with "Uid:" or "Gid:", followed by
  # real, effective, saved-set and filesystem IDs. Effective is awk field $3.
  awk -v field="$field" '$1 == field ":" { print $3; found=1 } END { if (!found) exit 1 }' "/proc/$pid/status"
}
[[ "$(proc_id "$paper_pid" Uid)" == "$paper_uid" ]] || fail "paper process effective UID mismatch"
[[ "$(proc_id "$dashboard_pid" Uid)" == "$dashboard_uid" ]] || fail "dashboard process effective UID mismatch"
db_gid="$(getent group veltrix-db | cut -d: -f3)"
paper_secrets_gid="$(getent group veltrix-paper-secrets | cut -d: -f3)"
dashboard_secrets_gid="$(getent group veltrix-dashboard-secrets | cut -d: -f3)"
[[ "$(proc_id "$paper_pid" Gid)" == "$db_gid" ]] || fail "paper process effective GID mismatch"
[[ "$(proc_id "$dashboard_pid" Gid)" == "$db_gid" ]] || fail "dashboard process effective GID mismatch"
proc_has_group() {
  local pid="$1" gid="$2"
  awk -v gid="$gid" '$1 == "Groups:" { for (i=2; i<=NF; i++) if ($i == gid) found=1 } END { exit !found }' "/proc/$pid/status"
}
proc_has_group "$paper_pid" "$paper_secrets_gid" || fail "paper process lacks paper credential group"
proc_has_group "$dashboard_pid" "$dashboard_secrets_gid" || fail "dashboard process lacks dashboard credential group"
proc_has_group "$paper_pid" "$dashboard_secrets_gid" && fail "paper process has dashboard credential group"
proc_has_group "$dashboard_pid" "$paper_secrets_gid" && fail "dashboard process has paper credential group"
observer_groups="$(id -G veltrix-wallet-observer)"
for forbidden_gid in "$db_gid" "$paper_secrets_gid" "$dashboard_secrets_gid"; do
  for observer_gid in $observer_groups; do
    [[ "$observer_gid" != "$forbidden_gid" ]] || fail "observer belongs to a paper/dashboard data or secret group"
  done
done
pass "running process UIDs, GIDs and supplementary group boundaries match"

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

# Prove the services run in distinct mount namespaces and that the kernel
# reports the database mount writable for paper but read-only for dashboard.
host_mount_ns="$(readlink /proc/1/ns/mnt)"
paper_mount_ns="$(readlink "/proc/$paper_pid/ns/mnt")"
dashboard_mount_ns="$(readlink "/proc/$dashboard_pid/ns/mnt")"
[[ "$paper_mount_ns" != "$host_mount_ns" ]] || fail "paper service shares the host mount namespace"
[[ "$dashboard_mount_ns" != "$host_mount_ns" ]] || fail "dashboard service shares the host mount namespace"
[[ "$paper_mount_ns" != "$dashboard_mount_ns" ]] || fail "paper and dashboard share a mount namespace"
pass "paper and dashboard use distinct non-host mount namespaces"

paper_mount_options="$(nsenter -t "$paper_pid" -m -- findmnt --noheadings --output OPTIONS --target "$DB")"
dashboard_mount_options="$(nsenter -t "$dashboard_pid" -m -- findmnt --noheadings --output OPTIONS --target "$DB")"
case ",$paper_mount_options," in *,rw,*) ;; *) fail "paper database mount is not effectively read-write: $paper_mount_options" ;; esac
case ",$dashboard_mount_options," in *,ro,*) ;; *) fail "dashboard database mount is not effectively read-only: $dashboard_mount_options" ;; esac
pass "kernel mount options confirm paper=rw and dashboard=ro"

# Verify effective DAC access as the actual service identities, inside their
# actual systemd mount namespaces. A read-write mount alone does not prove the
# paper UID can write the database after Unix ownership/mode checks.
nsenter -t "$paper_pid" -m -- runuser -u veltrix-paper -- test -r "$DB" \
  || fail "paper service cannot read the shared database"
nsenter -t "$paper_pid" -m -- runuser -u veltrix-paper -- test -w "$DB" \
  || fail "paper service cannot write the shared database"
nsenter -t "$paper_pid" -m -- runuser -u veltrix-paper -- test -w "$(dirname "$DB")" \
  || fail "paper service cannot write the database directory"
pass "paper service identity has effective database read/write access"

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
