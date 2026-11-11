"""Provenance-bound owner evidence for Jupiter route AMM pool accounts.

This stage confirms that each quoted AMM account exists, is non-executable,
has data, and is owned by the locally pinned DEX program at a slot no older
than the simulation. It does not decode the DEX pool state or account layout.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_program_trace import (
    JupiterProgramRegistrySnapshot,
    JupiterRouteProgramPolicy,
)
from veltrix.jupiter_v6_route import JupiterV6RouteInspection
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


class DexPoolEvidenceError(ValueError):
    """Raised when AMM pool account owner/provenance evidence is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class DexPoolEvidencePolicy:
    min_account_data_bytes: int = 8
    max_account_data_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        for field in ("min_account_data_bytes", "max_account_data_bytes"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DexPoolEvidenceError(f"{field} must be a positive integer")
        if self.min_account_data_bytes > self.max_account_data_bytes:
            raise DexPoolEvidenceError("minimum pool account data size exceeds maximum")


@dataclass(frozen=True, slots=True)
class DexPoolAccountEvidence:
    amm_key: str
    dex_label: str
    expected_program_id: str
    owner_program_id: str
    lamports: int
    executable: bool
    data_length: int
    data_sha256: str
    data_bytes: bytes


@dataclass(frozen=True, slots=True)
class DexPoolEvidenceSnapshot:
    route_message_sha256: str
    registry_sha256: str
    provider_id: str
    cluster: str
    genesis_hash: str
    context_slot: int
    accounts: tuple[DexPoolAccountEvidence, ...]
    execution_ready: bool = False


def fetch_dex_pool_owner_evidence(
    order: JupiterOrderTransaction,
    *,
    route_inspection: JupiterV6RouteInspection,
    registry: JupiterProgramRegistrySnapshot,
    route_policy: JupiterRouteProgramPolicy,
    rpc_adapter: SolanaRpcSimulationAdapter,
    min_context_slot: int,
    policy: DexPoolEvidencePolicy | None = None,
) -> DexPoolEvidenceSnapshot:
    """Fetch and verify every routePlan AMM account against the pinned DEX program owner."""
    if not isinstance(order, JupiterOrderTransaction):
        raise DexPoolEvidenceError("Jupiter order is required")
    if not isinstance(route_inspection, JupiterV6RouteInspection):
        raise DexPoolEvidenceError("Jupiter V6 route inspection is required")
    if not isinstance(registry, JupiterProgramRegistrySnapshot):
        raise DexPoolEvidenceError("verified Jupiter program registry snapshot is required")
    if not isinstance(route_policy, JupiterRouteProgramPolicy):
        raise DexPoolEvidenceError("locally pinned Jupiter route program policy is required")
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise DexPoolEvidenceError("provider-pinned Solana RPC adapter is required")
    policy = policy or DexPoolEvidencePolicy()
    if not isinstance(policy, DexPoolEvidencePolicy):
        raise DexPoolEvidenceError("DexPoolEvidencePolicy is required")
    if isinstance(min_context_slot, bool) or not isinstance(min_context_slot, int) or min_context_slot < 0:
        raise DexPoolEvidenceError("min_context_slot must be a non-negative integer")

    message_digest = hashlib.sha256(order.message_bytes).hexdigest()
    if route_inspection.message_sha256 != message_digest:
        raise DexPoolEvidenceError("route inspection is not bound to the exact order message")
    if route_inspection.program_id not in route_policy.allowed_jupiter_program_ids:
        raise DexPoolEvidenceError("route inspection Jupiter program is not locally pinned")
    if registry.sha256 == "" or registry.program_labels is None:
        raise DexPoolEvidenceError("verified program registry evidence is missing")

    expected_by_amm: dict[str, tuple[str, str]] = {}
    allowed_programs = set(route_policy.allowed_dex_program_ids)
    for step in route_inspection.route_steps:
        try:
            if str(Pubkey.from_string(step.amm_key)) != step.amm_key:
                raise ValueError("non-canonical")
        except Exception as exc:
            raise DexPoolEvidenceError("route inspection contains an invalid AMM public key") from exc
        candidates = set(registry.program_ids_for_label(step.dex_label)).intersection(allowed_programs)
        if len(candidates) != 1:
            raise DexPoolEvidenceError(
                f"DEX label {step.dex_label} does not resolve to exactly one locally pinned program ID"
            )
        expected_program = next(iter(candidates))
        prior = expected_by_amm.get(step.amm_key)
        if prior is not None and prior != (step.dex_label, expected_program):
            raise DexPoolEvidenceError("one AMM key is assigned to conflicting DEX program roles")
        expected_by_amm[step.amm_key] = (step.dex_label, expected_program)

    if not expected_by_amm:
        raise DexPoolEvidenceError("route inspection contains no AMM pool accounts")

    snapshot = rpc_adapter.fetch_account_snapshot(
        tuple(sorted(expected_by_amm)),
        min_context_slot=min_context_slot,
    )
    if snapshot.genesis_hash != rpc_adapter.expected_genesis_hash:
        raise DexPoolEvidenceError("account snapshot belongs to a different Solana cluster")
    if snapshot.context_slot < min_context_slot:
        raise DexPoolEvidenceError("account snapshot is older than the simulation context slot")
    if len(snapshot.accounts) != len(expected_by_amm):
        raise DexPoolEvidenceError("account snapshot count differs from the quoted AMM key set")

    evidence = []
    seen: set[str] = set()
    for account in snapshot.accounts:
        if account.address in seen:
            raise DexPoolEvidenceError("account snapshot contains duplicate AMM keys")
        seen.add(account.address)
        expected = expected_by_amm.get(account.address)
        if expected is None:
            raise DexPoolEvidenceError("RPC returned an account not present in the quoted routePlan")
        dex_label, expected_program = expected
        if account.owner != expected_program:
            raise DexPoolEvidenceError("AMM pool account owner differs from the locally pinned DEX program")
        if account.executable:
            raise DexPoolEvidenceError("AMM pool account must not be executable")
        if isinstance(account.lamports, bool) or not isinstance(account.lamports, int) or account.lamports <= 0:
            raise DexPoolEvidenceError("AMM pool account must have positive lamports")
        if not isinstance(account.data, bytes) or not policy.min_account_data_bytes <= len(account.data) <= policy.max_account_data_bytes:
            raise DexPoolEvidenceError("AMM pool account data length is outside policy")
        evidence.append(DexPoolAccountEvidence(
            amm_key=account.address,
            dex_label=dex_label,
            expected_program_id=expected_program,
            owner_program_id=account.owner,
            lamports=account.lamports,
            executable=account.executable,
            data_length=len(account.data),
            data_sha256=hashlib.sha256(account.data).hexdigest(),
            data_bytes=account.data,
        ))

    if seen != set(expected_by_amm):
        raise DexPoolEvidenceError("account snapshot omitted one or more quoted AMM pool accounts")

    return DexPoolEvidenceSnapshot(
        route_message_sha256=message_digest,
        registry_sha256=registry.sha256,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        accounts=tuple(evidence),
        execution_ready=False,
    )
