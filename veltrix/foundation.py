"""Fail-closed runtime activation controls for the VELTRIX release boundary.

This module intentionally contains no provider, wallet, scheduler, Telegram or
deployment clients. It only validates that release-sensitive capabilities stay
disabled until a separately reviewed release change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ALLOWED_TARGETS = frozenset({"development", "test", "ci", "preview", "production"})


def _boolean(environ: Mapping[str, str], name: str) -> bool:
    raw = str(environ.get(name, "false")).strip().lower()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    raise ValueError(f"{name} must be an explicit boolean value")


@dataclass(frozen=True)
class RuntimeControls:
    """Validated deployment and activation switches.

    Production, Telegram delivery, scheduled execution and live trading are
    deliberately rejected in this release line. A future release must change
    this policy in a reviewed commit and satisfy the release gates first.
    """

    deployment_target: str
    telegram_enabled: bool
    scheduler_enabled: bool
    live_trading_enabled: bool


def validate_runtime_controls(
    environ: Mapping[str, str] | None = None,
) -> RuntimeControls:
    """Return safe controls or raise before any runtime work starts."""
    import os

    env = os.environ if environ is None else environ
    configured_target = str(env.get("VELTRIX_DEPLOYMENT_TARGET", "")).strip().lower()
    platform_target = str(env.get("VERCEL_ENV", "")).strip().lower()
    target = configured_target or platform_target or "development"
    if target not in _ALLOWED_TARGETS:
        raise ValueError("VELTRIX_DEPLOYMENT_TARGET is not a supported target")
    if platform_target == "production":
        raise RuntimeError("Production deployment is locked until final release gates pass")

    controls = RuntimeControls(
        deployment_target=target,
        telegram_enabled=_boolean(env, "TELEGRAM_ENABLED"),
        scheduler_enabled=_boolean(env, "SCHEDULER_ENABLED"),
        live_trading_enabled=_boolean(env, "LIVE_TRADING"),
    )

    if controls.deployment_target == "production":
        raise RuntimeError("Production deployment is locked until final release gates pass")
    if controls.telegram_enabled:
        raise RuntimeError("Telegram delivery is locked until final release gates pass")
    if controls.scheduler_enabled:
        raise RuntimeError("Scheduling is locked until final release gates pass")
    if controls.live_trading_enabled:
        raise RuntimeError("Live trading is disabled in this project")

    return controls
