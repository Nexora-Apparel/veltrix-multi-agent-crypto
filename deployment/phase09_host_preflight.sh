#!/usr/bin/env bash
# Read-only compatibility inventory for the Phase 09.2 host runbook.
# Never creates users, changes permissions, starts services or reads secrets.
set -u
warn_count=0
report() { printf '%-12s %s\n' "$1" "$2"; }
warn() { warn_count=$((warn_count + 1)); report "WARNING" "$1"; }

if [[ "$(uname -s 2>/dev/null || true)" != Linux ]]; then
  report "BLOCKED" "This runbook requires a Linux host."
  exit 2
fi
report "OS" "$( . /etc/os-release 2>/dev/null; printf '%s %s' "${ID:-unknown}" "${VERSION_ID:-unknown}" )"
if ! command -v systemctl >/dev/null 2>&1; then
  report "BLOCKED" "systemctl is unavailable; this host is not ready for the systemd runbook."
  exit 2
fi
report "SYSTEMD" "$(systemctl --version 2>/dev/null | head -n 1 || printf 'present; version unavailable')"
if [[ "$(id -u)" -eq 0 ]]; then
  report "PRIVILEGE" "root"
else
  report "PRIVILEGE" "non-root; sudo required for installation and verification"
fi

check_path() {
  local label="$1" path="$2" kind="$3"
  if { [[ "$kind" == dir ]] && [[ -d "$path" ]]; } || { [[ "$kind" == file ]] && [[ -f "$path" ]]; }; then
    report "$label" "present: $path"
  else
    warn "$label missing: $path"
  fi
}
check_path "APP" "${VELTRIX_APP_DIR:-/opt/veltrix/app}" dir
check_path "VENV" "${VELTRIX_VENV_DIR:-/opt/veltrix/venv}" dir
check_path "DATABASE" "${VELTRIX_DB_PATH:-/var/lib/veltrix/solana_discovery.sqlite3}" file
check_path "PAPER_UNIT" "/etc/systemd/system/veltrix-paper.service" file
check_path "DASHBOARD_UNIT" "/etc/systemd/system/veltrix-dashboard.service" file

for name in veltrix-db veltrix-paper-secrets veltrix-dashboard-secrets; do
  if getent group "$name" >/dev/null 2>&1; then report "GROUP" "$name exists"; else warn "group missing: $name"; fi
done
for name in veltrix-paper veltrix-dashboard veltrix-wallet-observer; do
  if id "$name" >/dev/null 2>&1; then report "ACCOUNT" "$name exists"; else warn "service account missing: $name"; fi
done
if command -v python3 >/dev/null 2>&1; then
  report "PYTHON" "$(python3 --version 2>&1)"
else
  warn "python3 is unavailable"
fi
report "SECRETS" "not inspected; environment-file contents are never read"
report "CHANGES" "none; preflight is read-only"
if [[ "$warn_count" -gt 0 ]]; then
  report "RESULT" "$warn_count prerequisite warning(s); reconcile before installation"
else
  report "RESULT" "basic prerequisites present; full runbook checks still required"
fi
