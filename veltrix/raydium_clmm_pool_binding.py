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
from veltrix.raydium_clmm_aux_accounts import RaydiumClmmAuxAccountSnapshot
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID
from veltrix.raydium_clmm_bitmap_traversal import (
    RaydiumClmmBitmapTraversalError,
    verify_raydium_clmm_bitmap_traversal,
)
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
    aux_snapshot_slot: int
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT
    execution_ready: bool = False
    traversal_direction: str = ""
    traversal_start_indices: tuple[int, ...] = ()


def evaluate_raydium_clmm_cpi_pool_binding(
    order: JupiterOrderTransaction,
    *,
    cpi: RaydiumClmmCpiResult,
    pool_check: RaydiumClmmPoolRouteCheck,
    pool_evidence: object,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    aux_snapshot: RaydiumClmmAuxAccountSnapshot,
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
    if not isinstance(aux_snapshot, RaydiumClmmAuxAccountSnapshot):
        raise RaydiumClmmPoolBindingError("verified Raydium AMM config and observation snapshot is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmPoolBindingError("simulation evidence is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    pool = pool_check.pool
    if cpi.message_sha256 != digest or pool_evidence.route_message_sha256 != digest:
        raise RaydiumClmmPoolBindingError("CPI and pool evidence must bind to the exact Jupiter order message")
    if (
        vault_snapshot.route_message_sha256 != digest
        or tick_array_snapshot.route_message_sha256 != digest
        or aux_snapshot.route_message_sha256 != digest
        or simulation.message_sha256 != digest
    ):
        raise RaydiumClmmPoolBindingError("pool, vault, tick-array and simulation message digests differ")
    if (
        pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT
        or cpi.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT
        or aux_snapshot.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT
    ):
        raise RaydiumClmmPoolBindingError("Raydium decoder source revision is not the pinned revision")
    if (
        cpi.pool_state != pool.pool_address
        or pool_evidence.simulation_response_id != simulation.response_id
        or vault_snapshot.simulation_response_id != simulation.response_id
        or tick_array_snapshot.simulation_response_id != simulation.response_id
        or aux_snapshot.simulation_response_id != simulation.response_id
    ):
        raise RaydiumClmmPoolBindingError("CPI pool or evidence response identity differs from the approved simulation")
    if (
        pool_check.route_input_mint != order.input_mint
        or pool_check.route_output_mint != order.output_mint
    ):
        raise RaydiumClmmPoolBindingError("decoded pool route mint direction differs from the Jupiter order")

    configs = {item.address: item for item in aux_snapshot.amm_configs}
    observations = {item.address: item for item in aux_snapshot.observations}
    if len(configs) != len(aux_snapshot.amm_configs) or len(observations) != len(aux_snapshot.observations):
        raise RaydiumClmmPoolBindingError("auxiliary snapshot contains duplicate config or observation addresses")
    config = configs.get(pool.amm_config)
    observation = observations.get(pool.observation_key)
    if config is None or observation is None:
        raise RaydiumClmmPoolBindingError("verified AMM config or observation account is missing for the decoded pool")
    if cpi.amm_config != pool.amm_config or cpi.observation_state != pool.observation_key:
        raise RaydiumClmmPoolBindingError("CPI AMM config or observation account role differs from PoolState")
    if config.tick_spacing != pool.tick_spacing:
        raise RaydiumClmmPoolBindingError("AMM config tick spacing differs from PoolState")
    if observation.pool_address != pool.pool_address or observation.initialized is not True:
        raise RaydiumClmmPoolBindingError("observation account is not initialized for the decoded pool")
    if not pool.swaps_enabled:
        raise RaydiumClmmPoolBindingError("Raydium pool swap status is disabled")
    if pool.liquidity <= 0 or pool.sqrt_price_x64 <= 0 or pool.tick_spacing <= 0:
        raise RaydiumClmmPoolBindingError("Raydium pool has invalid liquidity, price or tick spacing")

    pool_accounts = [account for account in pool_evidence.accounts if account.amm_key == pool.pool_address]
    if len(pool_accounts) != 1:
        raise RaydiumClmmPoolBindingError("pool evidence must contain exactly one matching pool account")
    pool_account = pool_accounts[0]
    if (
        pool_account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or pool_account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID
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

    cpi_account_order = tuple(tick_array_snapshot.cpi_account_order)
    cpi_tick_arrays = tuple(cpi.tick_array_accounts)
    snapshot_tick_arrays = tuple(item.address for item in tick_array_snapshot.tick_arrays)
    bitmap_extension = tick_array_snapshot.bitmap_extension_address
    bitmap_digest = tick_array_snapshot.bitmap_extension_sha256
    bitmap_digest_valid = (
        isinstance(bitmap_digest, str)
        and len(bitmap_digest) == 64
        and all(character in "0123456789abcdef" for character in bitmap_digest)
    )
    if (
        not cpi_account_order
        or cpi_account_order != cpi_tick_arrays
        or len(set(cpi_account_order)) != len(cpi_account_order)
        or (bitmap_extension is not None and (bitmap_extension not in cpi_account_order or not bitmap_digest_valid))
        or (bitmap_extension is None and bitmap_digest is not None)
    ):
        raise RaydiumClmmPoolBindingError("verified tick-array snapshot does not preserve the exact CPI remaining-account order and bitmap digest")
    expected_tick_array_order = tuple(
        address for address in cpi_account_order if address != bitmap_extension
    )
    if not expected_tick_array_order or snapshot_tick_arrays != expected_tick_array_order:
        raise RaydiumClmmPoolBindingError("CPI tick-array accounts do not exactly match the decoded traversal arrays in order")
    if any(item.pool_address != pool.pool_address for item in tick_array_snapshot.tick_arrays):
        raise RaydiumClmmPoolBindingError("tick-array snapshot contains an array for a different pool")
    if any(item.tick_spacing != pool.tick_spacing for item in tick_array_snapshot.tick_arrays):
        raise RaydiumClmmPoolBindingError("tick-array spacing differs from the decoded PoolState")
    for item in tick_array_snapshot.tick_arrays:
        if derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=item.start_tick_index
        ) != item.address:
            raise RaydiumClmmPoolBindingError("tick-array address does not match the canonical pool PDA")
    try:
        traversal = verify_raydium_clmm_bitmap_traversal(
            order,
            pool_check=pool_check,
            pool_evidence=pool_evidence,
            tick_array_snapshot=tick_array_snapshot,
        )
    except RaydiumClmmBitmapTraversalError as exc:
        raise RaydiumClmmPoolBindingError(f"Raydium bitmap traversal proof failed: {exc}") from exc

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
        or aux_snapshot.provider_id != simulation.provider_id
        or aux_snapshot.cluster != simulation.cluster
        or aux_snapshot.genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmPoolBindingError("pool, vault, tick-array and simulation provider/cluster provenance differs")
    if (
        pool_evidence.simulation_context_slot != simulation.context_slot
        or vault_snapshot.simulation_context_slot != simulation.context_slot
        or tick_array_snapshot.simulation_context_slot != simulation.context_slot
        or aux_snapshot.simulation_context_slot != simulation.context_slot
    ):
        raise RaydiumClmmPoolBindingError("account snapshots are not bound to the exact simulation context slot")
    if not (
        pool_evidence.context_slot <= vault_snapshot.context_slot
        and vault_snapshot.context_slot <= tick_array_snapshot.context_slot
        and tick_array_snapshot.context_slot <= aux_snapshot.context_slot
    ):
        raise RaydiumClmmPoolBindingError("pool, vault and tick-array snapshot slots are not monotonically fresh")

    return RaydiumClmmPoolBindingResult(
        passed=True,
        message_sha256=digest,
        pool_address=pool.pool_address,
        input_vault=cpi.input_vault,
        output_vault=cpi.output_vault,
        tick_array_addresses=snapshot_tick_arrays,
        pool_account_sha256=pool.account_data_sha256,
        vault_snapshot_slot=vault_snapshot.context_slot,
        tick_array_snapshot_slot=tick_array_snapshot.context_slot,
        aux_snapshot_slot=aux_snapshot.context_slot,
        execution_ready=False,
        traversal_direction="zero_for_one" if traversal.zero_for_one else "one_for_zero",
        traversal_start_indices=traversal.expected_initialized_array_starts,
    )
