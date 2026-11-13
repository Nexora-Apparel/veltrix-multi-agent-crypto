from pathlib import Path

import pytest

from veltrix.foundation import validate_runtime_controls


def test_runtime_controls_default_to_safe_development_profile():
    controls = validate_runtime_controls({})
    assert controls.deployment_target == "development"
    assert controls.telegram_enabled is False
    assert controls.scheduler_enabled is False
    assert controls.live_trading_enabled is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
@pytest.mark.parametrize("name", ["TELEGRAM_ENABLED", "SCHEDULER_ENABLED", "LIVE_TRADING"])
def test_release_sensitive_capabilities_cannot_be_enabled(name, value):
    with pytest.raises(RuntimeError):
        validate_runtime_controls({name: value})


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " FALSE "])
def test_explicit_false_values_remain_disabled(value):
    controls = validate_runtime_controls({
        "TELEGRAM_ENABLED": value,
        "SCHEDULER_ENABLED": value,
        "LIVE_TRADING": value,
    })
    assert not controls.telegram_enabled
    assert not controls.scheduler_enabled
    assert not controls.live_trading_enabled


@pytest.mark.parametrize("name", ["TELEGRAM_ENABLED", "SCHEDULER_ENABLED", "LIVE_TRADING"])
def test_malformed_activation_flag_fails_closed(name):
    with pytest.raises(ValueError, match="explicit boolean"):
        validate_runtime_controls({name: "sometimes"})


@pytest.mark.parametrize("target", ["production", " Production ", "PRODUCTION"])
def test_production_deployment_is_locked(target):
    with pytest.raises(RuntimeError, match="Production deployment is locked"):
        validate_runtime_controls({"VELTRIX_DEPLOYMENT_TARGET": target})


def test_preview_is_an_allowed_nonproduction_target():
    controls = validate_runtime_controls({"VELTRIX_DEPLOYMENT_TARGET": "preview"})
    assert controls.deployment_target == "preview"


def test_unknown_deployment_target_is_rejected():
    with pytest.raises(ValueError, match="not a supported target"):
        validate_runtime_controls({"VELTRIX_DEPLOYMENT_TARGET": "staging-ish"})


def test_existing_workflows_do_not_define_scheduled_triggers():
    workflow_dir = Path(".github/workflows")
    assert workflow_dir.is_dir()
    scheduled = []
    for path in workflow_dir.glob("*.y*ml"):
        source = path.read_text(encoding="utf-8")
        if any(line.strip() == "schedule:" for line in source.splitlines()):
            scheduled.append(path.name)
    assert scheduled == [], f"Scheduled workflows must remain disabled: {scheduled}"


def test_example_environment_keeps_release_switches_disabled():
    source = Path(".env.example").read_text(encoding="utf-8")
    for name in ("TELEGRAM_ENABLED", "SCHEDULER_ENABLED", "LIVE_TRADING"):
        values = [
            line.split("=", 1)[1].strip().lower()
            for line in source.splitlines()
            if line.startswith(name + "=")
        ]
        assert values == ["false"], f"{name} must have one explicit false default"


def test_shared_settings_factory_enforces_release_locks(monkeypatch):
    from veltrix.config import settings

    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    with pytest.raises(RuntimeError, match="Telegram delivery is locked"):
        settings()


def test_platform_production_environment_cannot_be_overridden():
    with pytest.raises(RuntimeError, match="Production deployment is locked"):
        validate_runtime_controls({
            "VELTRIX_DEPLOYMENT_TARGET": "development",
            "VERCEL_ENV": "production",
        })


def test_vercel_preview_environment_is_recognized():
    controls = validate_runtime_controls({"VERCEL_ENV": "preview"})
    assert controls.deployment_target == "preview"
