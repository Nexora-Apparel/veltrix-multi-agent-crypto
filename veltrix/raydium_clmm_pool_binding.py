"""Cross-bind Raydium CLMM CPI accounts to independently fetched pool evidence.

This gate is intentionally evidence-only. It verifies that the exact simulated
CPI references the pool, vaults, and current tick arrays decoded by the
source-pinned read-only account decoders. It never authorizes signing or swaps.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_cpi import RaydiumClmmCpiResult
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArraySnapshot,
    derive_raydium_clmm_tick_array_address,
    raydium_current_tick_array_start,
)
from veltrix.simulation_gate import SimulationEvidence


class RaydiumClmmPoolBindingError(ValueError):
    """Raised when simulated CPI accounts do not match independent pool evidence."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmPoolBindingResult:
    passed: bool
    message_sha256: str
    pool_address: str
    input_vault: str
    output_vault: str
    tick_array_addresses: tuple[str, ...]
    pool_account_sha256: str
    vault_snapshot_slot: int
    tick_array_snapshot_slot: int
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT
    execution_ready: bool = False


def evaluate_raydium_clmm_cpi_pool_binding(
    order: JupiterOrderTransaction,
    *,
    cpi: RaydiumClmmCpiResult,
    pool_check: RaydiumClmmPoolRouteCheck,
    pool_evidence: object,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    simulation: SimulationEvidence,
) -> RaydiumClmmPoolBindingResult:
    """Bind CPI pool/vault/tick accounts to same-order, same-provider snapshots."""
    from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot

    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmPoolBindingError("Jupiter order is required")
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumClmmPoolBindingError("successful Raydium CLMM CPI evidence is required")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumClmmPoolBindingError("successful Raydium CLMM PoolState route check is required")
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmPoolBindingError("verified DEX pool account snapshot is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumClmmPoolBindingError("verified Raydium vault snapshot is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmPoolBindingError("verified Raydium tick-array snapshot is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmPoolBindingError("simulation evidence is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    pool = pool_check.pool
    if cpi.message_sha256 != digest or pool_evidence.route_message_sha256 != digest:
        raise RaydiumClmmPoolBindingError("CPI and pool evidence must bind to the exact Jupiter order message")
    if (
        vault_snapshot.route_message_sha256 != digest
        or tick_array_snapshot.route_message_sha256 != digest
        or simulation.message_sha256 != digest
    ):
        raise RaydiumClmmPoolBindingError("pool, vault, tick-array and simulation message digests differ")
    if (
        pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT
        or cpi.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT
    ):
        raise RaydiumClmmPoolBindingError("Raydium decoder source revision is not the pinned revision")
    if (
        cpi.pool_state != pool.pool_address
        or pool_evidence.simulation_response_id != simulation.response_id
        or vault_snapshot.simulation_response_id != simulation.response_id
        or tick_array_snapshot.simulation_response_id != simulation.response_id
    ):
        raise RaydiumClmmPoolBindingError("CPI pool or evidence response identity differs from the approved simulation")
    if (
        pool_check.route_input_mint != order.input_mint
        or pool_check.route_output_mint != order.output_mint
    ):
        raise RaydiumClmmPoolBindingError("decoded pool route mint direction differs from the Jupiter order")
    if not pool.swaps_enabled:
        raise RaydiumClmmPoolBindingError("Raydium pool swap status is disabled")
    if pool.liquidity <= 0 or pool.sqrt_price_x64 <= 0 or pool.tick_spacing <= 0:
        raise RaydiumClmmPoolBindingError("Raydium pool has invalid liquidity, price or tick spacing")

    pool_accounts = [account for account in pool_evidence.accounts if account.amm_key == pool.pool_address]
    if len(pool_accounts) != 1:
        raise RaydiumClmmPoolBindingError("pool evidence must contain exactly one matching pool account")
    pool_account = pool_accounts[0]
    if (
        pool_account.owner_program_id != pool_account.expected_program_id
        or pool_account.data_sha256 != pool.account_data_sha256
        or hashlib.sha256(pool_account.data_bytes).hexdigest() != pool.account_data_sha256
        or pool_account.data_length != len(pool_account.data_bytes)
        or pool_account.executable is not False
        or pool_account.lamports <= 0
        or pool_account.dex_label != "Raydium CLMM"
    ):
        raise RaydiumClmmPoolBindingError("decoded PoolState hash or owner does not match fetched pool evidence")

    expected_vault_by_mint = {
        pool.token_mint_0: pool.token_vault_0,
        pool.token_mint_1: pool.token_vault_1,
    }
    if len(expected_vault_by_mint) != 2 or order.input_mint not in expected_vault_by_mint or order.output_mint not in expected_vault_by_mint:
        raise RaydiumClmmPoolBindingError("Jupiter input/output mints do not match the two pool mints")
    if (
        cpi.input_vault != expected_vault_by_mint[order.input_mint]
        or cpi.output_vault != expected_vault_by_mint[order.output_mint]
    ):
        raise RaydiumClmmPoolBindingError("Raydium CPI input/output vault order differs from the decoded pool mint direction")

    expected_vaults = {
        (pool.token_vault_0, pool.token_mint_0),
        (pool.token_vault_1, pool.token_mint_1),
    }
    observed_vaults = set()
    for vault in vault_snapshot.vaults:
        if vault.pool_address != pool.pool_address:
            raise RaydiumClmmPoolBindingError("vault snapshot contains a vault for a different pool")
        if (
            vault.token_authority != pool.pool_address
            or vault.token_program_id != CLASSIC_TOKEN_PROGRAM_ID
            or vault.state != 1
            or vault.lamports <= 0
        ):
            raise RaydiumClmmPoolBindingError("vault authority, token program, state or lamports are invalid")
        observed_vaults.add((vault.address, vault.mint))
    if observed_vaults != expected_vaults or len(vault_snapshot.vaults) != 2:
        raise RaydiumClmmPoolBindingError("vault snapshot does not exactly cover the pool's two ordered mint/vault pairs")

    cpi_tick_arrays = tuple(cpi.tick_array_accounts)
    snapshot_tick_arrays = tuple(item.address for item in tick_array_snapshot.tick_arrays)
    if (
        not cpi_tick_arrays
        or len(set(cpi_tick_arrays)) != len(cpi_tick_arrays)
        or set(cpi_tick_arrays) != set(snapshot_tick_arrays)
        or len(snapshot_tick_arrays) != len(cpi_tick_arrays)
    ):
        raise RaydiumClmmPoolBindingError("CPI tick-array accounts do not exactly match the verified tick-array snapshot")
    if any(item.pool_address != pool.pool_address for item in tick_array_snapshot.tick_arrays):
        raise RaydiumClmmPoolBindingError("tick-array snapshot contains an array for a different pool")
    if any(item.tick_spacing != pool.tick_spacing for item in tick_array_snapshot.tick_arrays):
        raise RaydiumClmmPoolBindingError("tick-array spacing differs from the decoded PoolState")
    expected_current_start = raydium_current_tick_array_start(pool.tick_current, pool.tick_spacing)
    for item in tick_array_snapshot.tick_arrays:
        if item.start_tick_index != expected_current_start:
            raise RaydiumClmmPoolBindingError("tick-array start index is not the pool's current tick array")
        if derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=item.start_tick_index
        ) != item.address:
            raise RaydiumClmmPoolBindingError("tick-array address does not match the canonical pool PDA")

    if (
        pool_evidence.provider_id != simulation.provider_id
        or vault_snapshot.provider_id != simulation.provider_id
        or tick_array_snapshot.provider_id != simulation.provider_id
        or pool_evidence.cluster != simulation.cluster
        or vault_snapshot.cluster != simulation.cluster
        or tick_array_snapshot.cluster != simulation.cluster
        or pool_evidence.genesis_hash != simulation.genesis_hash
        or vault_snapshot.genesis_hash != simulation.genesis_hash
        or tick_array_snapshot.genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmPoolBindingError("pool, vault, tick-array and simulation provider/cluster provenance differs")
    if (
        pool_evidence.simulation_context_slot != simulation.context_slot
        or vault_snapshot.simulation_context_slot != simulation.context_slot
        or tick_array_snapshot.simulation_context_slot != simulation.context_slot
    ):
        raise RaydiumClmmPoolBindingError("account snapshots are not bound to the exact simulation context slot")
    if not (
        pool_evidence.context_slot <= vault_snapshot.context_slot
        and vault_snapshot.context_slot <= tick_array_snapshot.context_slot
    ):
        raise RaydiumClmmPoolBindingError("pool, vault and tick-array snapshot slots are not monotonically fresh")

    return RaydiumClmmPoolBindingResult(
        passed=True,
        message_sha256=digest,
        pool_address=pool.pool_address,
        input_vault=cpi.input_vault,
        output_vault=cpi.output_vault,
        tick_array_addresses=cpi_tick_arrays,
        pool_account_sha256=pool.account_data_sha256,
        vault_snapshot_slot=vault_snapshot.context_slot,
        tick_array_snapshot_slot=tick_array_snapshot.context_slot,
        execution_ready=False,
    )
