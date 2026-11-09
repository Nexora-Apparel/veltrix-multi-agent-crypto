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
    assert "LIVE_TRADING=false" in unit
    assert "PAPER_TRADING_ENABLED=true" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "CapabilityBoundingSet=" in unit


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


def test_process_isolation_document_keeps_observer_and_signer_unimplemented():
    document = (ROOT / "docs" / "phase-09-process-isolation.md").read_text(encoding="utf-8")
    assert "Future wallet observer" in document
    assert "not implemented" in document
    assert "No provider-to-signer bridge" in document
    assert "Phase 09.2 is not complete" in document
