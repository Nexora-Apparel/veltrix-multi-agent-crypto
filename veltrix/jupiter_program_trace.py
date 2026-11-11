"""Jupiter program-label registry and route/CPI allowlist gate.

The registry is metadata, not a trust root. Local policy pins acceptable DEX
labels, and the simulated transaction's top-level and inner program trace must
match the Jupiter order's route plan.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any

import requests
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


JUPITER_PROGRAM_LABEL_URL = "https://api.jup.ag/swap/v2/program-id-to-label"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


class JupiterProgramTraceError(ValueError):
    """Raised when Jupiter route metadata and simulated program trace disagree."""


@dataclass(frozen=True, slots=True)
class JupiterProgramRegistrySnapshot:
    observed_at_epoch_ms: int
    program_labels: tuple[tuple[str, str], ...]
    sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.observed_at_epoch_ms, bool) or not isinstance(self.observed_at_epoch_ms, int) or self.observed_at_epoch_ms <= 0:
            raise JupiterProgramTraceError("registry observation timestamp is invalid")
        if not isinstance(self.program_labels, tuple) or not self.program_labels:
            raise JupiterProgramTraceError("program label registry must be a non-empty immutable tuple")
        ids = []
        for pair in self.program_labels:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise JupiterProgramTraceError("program label registry contains a malformed pair")
            program_id, label = pair
            try:
                if str(Pubkey.from_string(program_id)) != program_id:
                    raise ValueError("non-canonical")
            except Exception as exc:
                raise JupiterProgramTraceError("program label registry contains an invalid program ID") from exc
            if not isinstance(label, str) or not label:
                raise JupiterProgramTraceError("program label registry contains an empty label")
            ids.append(program_id)
        if len(ids) != len(set(ids)):
            raise JupiterProgramTraceError("program label registry contains duplicate program IDs")
        expected = hashlib.sha256(
            json.dumps(self.program_labels, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        if self.sha256 != expected:
            raise JupiterProgramTraceError("program label registry digest does not match its contents")

    def label_for(self, program_id: str) -> str | None:
        return dict(self.program_labels).get(program_id)

    def program_ids_for_label(self, label: str) -> tuple[str, ...]:
        return tuple(program_id for program_id, value in self.program_labels if value == label)


class JupiterProgramRegistryAdapter:
    """Fetches the official Jupiter v2 program-id-to-label mapping."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 8.0,
        session: Any | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise JupiterProgramTraceError("api_key must be non-empty when provided")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 20:
            raise JupiterProgramTraceError("timeout_seconds must be greater than zero and at most 20")
        self._api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self._session = session or requests.Session()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def fetch(self) -> JupiterProgramRegistrySnapshot:
        headers = {"Accept": "application/json"}
        if self._api_key is not None:
            headers["x-api-key"] = self._api_key
        try:
            response = self._session.get(
                JUPITER_PROGRAM_LABEL_URL,
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise JupiterProgramTraceError(f"Jupiter program registry returned HTTP {response.status_code}")
            body = response.json()
        except JupiterProgramTraceError:
            raise
        except Exception as exc:
            raise JupiterProgramTraceError("Jupiter program registry transport or JSON decoding failed") from exc
        if not isinstance(body, dict) or not body:
            raise JupiterProgramTraceError("Jupiter program registry is empty or malformed")
        pairs = []
        for program_id, label in body.items():
            try:
                canonical = str(Pubkey.from_string(program_id))
            except Exception as exc:
                raise JupiterProgramTraceError("Jupiter program registry contains an invalid program ID") from exc
            if canonical != program_id or not isinstance(label, str) or not label:
                raise JupiterProgramTraceError("Jupiter program registry contains a non-canonical ID or empty label")
            pairs.append((program_id, label))
        normalized = tuple(sorted(pairs))
        observed = self._clock_ms()
        digest = hashlib.sha256(
            json.dumps(normalized, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        return JupiterProgramRegistrySnapshot(
            observed_at_epoch_ms=observed,
            program_labels=normalized,
            sha256=digest,
        )


@dataclass(frozen=True, slots=True)
class JupiterRouteProgramPolicy:
    allowed_dex_labels: tuple[str, ...]
    allowed_dex_program_ids: tuple[str, ...]
    allowed_jupiter_program_ids: tuple[str, ...]
    allowed_routers: tuple[str, ...] = ("metis",)
    jupiter_program_labels: tuple[str, ...] = ("Jupiter V6",)
    standard_program_ids: tuple[str, ...] = (
        TOKEN_PROGRAM_ID,
        COMPUTE_BUDGET_PROGRAM_ID,
    )
    max_registry_age_ms: int = 86_400_000
    max_future_skew_ms: int = 1_000

    def __post_init__(self) -> None:
        for field in ("allowed_dex_labels", "allowed_dex_program_ids", "allowed_jupiter_program_ids", "allowed_routers", "jupiter_program_labels", "standard_program_ids"):
            value = getattr(self, field)
            if not isinstance(value, tuple) or not value:
                raise JupiterProgramTraceError(f"{field} must be a non-empty immutable tuple")
            if any(not isinstance(item, str) or not item for item in value):
                raise JupiterProgramTraceError(f"{field} contains an invalid value")
            if len(set(value)) != len(value):
                raise JupiterProgramTraceError(f"{field} must not contain duplicates")
        if set(self.allowed_routers) != {"metis"}:
            raise JupiterProgramTraceError("only Metis is approved until alternate routers have independent semantic decoders")
        for field in ("allowed_dex_program_ids", "allowed_jupiter_program_ids", "standard_program_ids"):
            for program_id in getattr(self, field):
                try:
                    if str(Pubkey.from_string(program_id)) != program_id:
                        raise ValueError("non-canonical")
                except Exception as exc:
                    raise JupiterProgramTraceError(f"{field} contains an invalid public key") from exc
        for program_id in self.standard_program_ids:
            try:
                if str(Pubkey.from_string(program_id)) != program_id:
                    raise ValueError("non-canonical")
            except Exception as exc:
                raise JupiterProgramTraceError("standard program allowlist contains an invalid public key") from exc
        if any(program_id not in {TOKEN_PROGRAM_ID, COMPUTE_BUDGET_PROGRAM_ID} for program_id in self.standard_program_ids):
            raise JupiterProgramTraceError("standard programs are limited to classic SPL Token for inner CPI and Compute Budget for top-level instructions")
        for field in ("max_registry_age_ms", "max_future_skew_ms"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JupiterProgramTraceError(f"{field} must be a non-negative integer")
        if self.max_registry_age_ms == 0:
            raise JupiterProgramTraceError("max_registry_age_ms must be positive")


@dataclass(frozen=True, slots=True)
class JupiterRouteProgramGateResult:
    passed: bool
    message_sha256: str
    router: str
    route_labels: tuple[str, ...]
    dex_program_ids: tuple[str, ...]
    top_level_program_ids: tuple[str, ...]
    inner_program_ids: tuple[str, ...]
    registry_sha256: str


def evaluate_jupiter_route_programs(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    registry: JupiterProgramRegistrySnapshot,
    policy: JupiterRouteProgramPolicy,
    now_epoch_ms: int,
) -> JupiterRouteProgramGateResult:
    """Bind Jupiter's route labels to the exact simulated top-level/CPI program trace."""
    if not isinstance(order, JupiterOrderTransaction):
        raise JupiterProgramTraceError("Jupiter v0 order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise JupiterProgramTraceError("structural transaction validation is required")
    if not isinstance(simulation, SimulationEvidence):
        raise JupiterProgramTraceError("simulation program trace is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise JupiterProgramTraceError("successful SimulationGate approval is required")
    if not isinstance(registry, JupiterProgramRegistrySnapshot):
        raise JupiterProgramTraceError("Jupiter program registry snapshot is required")
    if not isinstance(policy, JupiterRouteProgramPolicy):
        raise JupiterProgramTraceError("Jupiter route program policy is required")
    if isinstance(now_epoch_ms, bool) or not isinstance(now_epoch_ms, int) or now_epoch_ms <= 0:
        raise JupiterProgramTraceError("now_epoch_ms must be a positive integer")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if (
        digest != validation.message_sha256
        or digest != simulation.message_sha256
        or digest != simulation_gate.message_sha256
    ):
        raise JupiterProgramTraceError("order, validator and simulation message digests differ")
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise JupiterProgramTraceError("Jupiter taker is not the transaction fee payer")
    if simulation.recent_blockhash != order.recent_blockhash:
        raise JupiterProgramTraceError("simulation blockhash differs from the Jupiter order")
    if simulation.last_valid_block_height != order.last_valid_block_height:
        raise JupiterProgramTraceError("simulation last-valid block height differs from the Jupiter order")
    if simulation.response_id != simulation_gate.response_id or simulation.provider_id != simulation_gate.provider_id:
        raise JupiterProgramTraceError("simulation evidence and gate result do not share the same provider response")
    if order.router not in policy.allowed_routers:
        raise JupiterProgramTraceError("Jupiter router is outside the local route policy")
    if registry.observed_at_epoch_ms > now_epoch_ms + policy.max_future_skew_ms:
        raise JupiterProgramTraceError("Jupiter program registry timestamp is in the future")
    if now_epoch_ms - registry.observed_at_epoch_ms > policy.max_registry_age_ms:
        raise JupiterProgramTraceError("Jupiter program registry is stale")

    route_labels = tuple(dict.fromkeys(step.label for step in order.route_plan))
    if not route_labels:
        raise JupiterProgramTraceError("Jupiter order has no route labels")
    if any(label not in policy.allowed_dex_labels for label in route_labels):
        raise JupiterProgramTraceError("Jupiter route contains a DEX label outside local policy")

    dex_ids_by_label = {}
    for label in route_labels:
        mapped = set(registry.program_ids_for_label(label))
        pinned = mapped.intersection(policy.allowed_dex_program_ids)
        if not pinned:
            raise JupiterProgramTraceError(f"route DEX label {label} has no locally pinned program ID in the registry")
        dex_ids_by_label[label] = tuple(sorted(pinned))

    for program_id in policy.allowed_dex_program_ids:
        if registry.label_for(program_id) not in policy.allowed_dex_labels:
            raise JupiterProgramTraceError("a pinned DEX program ID is not labelled as an allowed DEX")
    for program_id in policy.allowed_jupiter_program_ids:
        if registry.label_for(program_id) not in policy.jupiter_program_labels:
            raise JupiterProgramTraceError("a pinned Jupiter program ID does not match the expected registry label")

    dex_ids = tuple(sorted(set(policy.allowed_dex_program_ids).intersection(
        program_id for values in dex_ids_by_label.values() for program_id in values
    )))
    jupiter_ids = policy.allowed_jupiter_program_ids
    if not jupiter_ids:
        raise JupiterProgramTraceError("no locally pinned Jupiter aggregator program IDs are configured")
    top_level = simulation.top_level_program_ids
    inner = simulation.inner_program_ids
    if top_level != validation.program_ids:
        raise JupiterProgramTraceError("simulated top-level program trace differs from validated message")
    if len([program_id for program_id in top_level if program_id in jupiter_ids]) != 1:
        raise JupiterProgramTraceError("transaction must invoke exactly one approved Jupiter aggregator instruction")
    if simulation.inner_instruction_count <= 0 or not inner:
        raise JupiterProgramTraceError("simulation did not return the required inner-CPI program trace")

    # Context-specific allowlists prevent a standard program ID from becoming
    # a blanket authorization for arbitrary top-level transfers or account setup.
    top_level_allowed = set(jupiter_ids)
    if COMPUTE_BUDGET_PROGRAM_ID in policy.standard_program_ids:
        top_level_allowed.add(COMPUTE_BUDGET_PROGRAM_ID)
    inner_allowed = set(dex_ids)
    if TOKEN_PROGRAM_ID in policy.standard_program_ids:
        inner_allowed.add(TOKEN_PROGRAM_ID)
    if any(program_id not in top_level_allowed for program_id in top_level):
        raise JupiterProgramTraceError("top-level message invokes a program outside the Jupiter/compute-budget allowlist")
    if any(program_id not in inner_allowed for program_id in inner):
        raise JupiterProgramTraceError("inner CPI trace invokes a program outside the DEX/token allowlist")
    for label, program_ids in dex_ids_by_label.items():
        if not any(program_id in inner for program_id in program_ids):
            raise JupiterProgramTraceError(f"simulated inner CPI trace does not invoke route DEX label {label}")
    if any(program_id not in validation.account_keys for program_id in set(top_level) | set(inner)):
        raise JupiterProgramTraceError("simulated program trace contains a program absent from validated account keys")

    return JupiterRouteProgramGateResult(
        passed=True,
        message_sha256=digest,
        router=order.router,
        route_labels=route_labels,
        dex_program_ids=dex_ids,
        top_level_program_ids=top_level,
        inner_program_ids=inner,
        registry_sha256=registry.sha256,
    )
