from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _unit(name: str) -> str:
    return (ROOT / "deployment" / "systemd" / name).read_text(encoding="utf-8")


def test_paper_unit_has_dedicated_identity_and_bounded_writable_state():
    unit = _unit("veltrix-paper.service")
    assert "User=veltrix-paper" in unit
    assert "Group=veltrix-db" in unit
    assert "SupplementaryGroups=veltrix-paper-secrets" in unit
    assert "StateDirectory=veltrix" in unit
    assert "UMask=0007" in unit
    assert "ReadWritePaths=/var/lib/veltrix" in unit
    assert "InaccessiblePaths=/etc/veltrix/dashboard.env" in unit
    assert "LIVE_TRADING=false" in unit
    assert "PAPER_TRADING_ENABLED=true" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "CapabilityBoundingSet=" in unit
    for directive in (
        "NoNewPrivileges=true", "PrivateTmp=true", "ProtectHome=true",
        "ProtectSystem=strict", "ProtectKernelTunables=true",
        "ProtectKernelModules=true", "ProtectControlGroups=true",
        "RestrictSUIDSGID=true", "LockPersonality=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "AmbientCapabilities=",
    ):
        assert directive in unit


def test_dashboard_unit_is_distinct_and_database_read_only():
    unit = _unit("veltrix-dashboard.service")
    assert "User=veltrix-dashboard" in unit
    assert "Group=veltrix-db" in unit
    assert "SupplementaryGroups=veltrix-dashboard-secrets" in unit
    assert "ReadOnlyPaths=/var/lib/veltrix" in unit
    assert "VELTRIX_DASHBOARD_HOST=127.0.0.1" in unit
    assert "EnvironmentFile=/etc/veltrix/dashboard.env" in unit
    assert "InaccessiblePaths=/etc/veltrix/paper.env" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=" not in unit
    for directive in (
        "NoNewPrivileges=true", "PrivateTmp=true", "ProtectHome=true",
        "ProtectSystem=strict", "ProtectKernelTunables=true",
        "ProtectKernelModules=true", "ProtectControlGroups=true",
        "RestrictSUIDSGID=true", "LockPersonality=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "AmbientCapabilities=", "CapabilityBoundingSet=",
    ):
        assert directive in unit


def test_process_isolation_document_keeps_observer_and_signer_unimplemented():
    document = (ROOT / "docs" / "phase-09-process-isolation.md").read_text(encoding="utf-8")
    assert "Future wallet observer" in document
    assert "not implemented" in document
    assert "No provider-to-signer bridge" in document
    assert "Phase 09.2 is not complete" in document



def test_dashboard_code_has_no_provider_or_execution_adapter_imports():
    source = (ROOT / "veltrix" / "dashboard_server.py").read_text(encoding="utf-8")
    assert "from .providers" not in source
    assert "from ..providers" not in source
    assert "SignerBoundary" not in source
    assert "submit_transaction" not in source
    assert "LIVE_TRADING" not in source


def test_service_templates_do_not_embed_wallet_key_material():
    for name in ("veltrix-paper.service", "veltrix-dashboard.service"):
        unit = _unit(name).lower()
        for forbidden in ("private_key=", "seed_phrase=", "mnemonic=", "keypair_json="):
            assert forbidden not in unit



def test_host_verification_script_checks_actual_systemd_mount_namespace():
    script = (ROOT / "deployment" / "verify_phase09_isolation.sh").read_text(encoding="utf-8")
    assert "nsenter -t" in script
    assert "ReadOnlyPaths" in script
    assert "ReadWritePaths" in script
    assert "test ! -w" in script
    assert "systemctl is-active" in script
    assert "effective UID mismatch" in script
    assert "print $3" in script  # label is field 1; effective UID/GID is field 3
    assert "paper service cannot write the shared database" in script
    assert "paper service cannot write the database directory" in script
    assert "supplementary group boundaries" in script
    assert "paper process has dashboard credential group" in script
    assert "dashboard process has paper credential group" in script
    assert "observer belongs to a paper/dashboard data or secret group" in script
    assert "veltrix-wallet-observer" in script
    assert "MANUAL REQUIRED" in script


def test_host_preflight_is_read_only_and_checks_systemd_prerequisites():
    script = (ROOT / "deployment" / "phase09_host_preflight.sh").read_text(encoding="utf-8")
    assert "command -v systemctl" in script
    assert "veltrix-wallet-observer" in script
    assert "VELTRIX_DB_PATH" in script
    assert "environment-file contents are never read" in script
    assert "preflight is read-only" in script
    for forbidden in ("useradd ", "groupadd ", "chown ", "chmod ", "systemctl enable", "systemctl start"):
        assert forbidden not in script

def test_host_verifier_checks_distinct_mount_namespaces_and_kernel_mount_flags():
    script = (ROOT / "deployment" / "verify_phase09_isolation.sh").read_text(encoding="utf-8")
    assert 'readlink "/proc/$paper_pid/ns/mnt"' in script
    assert 'readlink "/proc/$dashboard_pid/ns/mnt"' in script
    assert '"$paper_mount_ns" != "$host_mount_ns"' in script
    assert '"$dashboard_mount_ns" != "$host_mount_ns"' in script
    assert '"$paper_mount_ns" != "$dashboard_mount_ns"' in script
    assert 'findmnt --noheadings --output OPTIONS --target "$DB"' in script
    assert '*,rw,*)' in script
    assert '*,ro,*)' in script

