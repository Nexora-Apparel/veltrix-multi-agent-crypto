"""Fail-closed Raydium CLMM legacy swap CPI semantic gate.

Pinned to raydium-io/raydium-clmm source commit 6d7a0f82d2f293c553c69aaf943377367c6c4cc6.
Supports one direct classic-SPL legacy `swap` CPI under one Metis/Jupiter route.
The Token-2022-capable `swap_v2` path and multi-hop routes are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.message import from_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    JupiterSwapEffectResult,
    derive_associated_token_address,
)
from veltrix.jupiter_v6_route import (
    ROUTE_DISCRIMINATOR,
    SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR,
    JupiterV6RouteInspection,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolRouteCheck
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_MAX_TICK if False else RAYDIUM_TICK_ARRAY_SEED,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot
from veltrix.raydium_clmm_vaults import (
    RaydiumClmmVaultSnapshot,
    RaydiumClmmVaultAccount,
)
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter
from veltrix.transaction_validator import TransactionValidationResult


RAYDIUM_CLMM_SOURCE_COMMIT = "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"
RAYDIUM_CLMM_MAINNET_PROGRAM_ID = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CLMM_SWAP_DISCRIMINATOR = hashlib.sha256(b"global:swap").digest()[:8]
RAYDIUM_CLMM_SWAP_V2_DISCRIMINATOR = hashlib.sha256(b"global:swap_v2").digest()[:8]
RAYDIUM_CLMM_FIXED_ACCOUNT_COUNT = 10
RAYDIUM_MIN_SQRT_PRICE_X64 = 4_295_048_016
RAYDIUM_MAX_SQRT_PRICE_X64 = 79_226_673_521_066_979_257_578_248_091


class RaydiumClmmCpiError(ValueError):
    """Raised when a Raydium CLMM CPI does not match the approved Jupiter route."""


@dataclass(frozen=True, slots=True)
class DecodedRaydiumClmmSwap:
    amount_raw: int
    other_amount_threshold_raw: int
    sqrt_price_limit_x64: int
    is_base_input: bool


@dataclass(frozen=True, slots=True)
class RaydiumClmmCpiPolicy:
    program_id: str = RAYDIUM_CLMM_MAINNET_PROGRAM_ID
    max_slippage_bps: int = 100

    def __post_init__(self) -> None:
        try:
            if str(Pubkey.from_string(self.program_id)) != self.program_id:
                raise ValueError("non-canonical")
        except Exception as exc:
            raise RaydiumClmmCpiError("Raydium CLMM program ID is invalid") from exc
        if self.program_id != RAYDIUM_CLMM_MAINNET_PROGRAM_ID:
            raise RaydiumClmmCpiError("only the source-pinned Raydium CLMM mainnet program ID is supported")
        if isinstance(self.max_slippage_bps, bool) or not isinstance(self.max_slippage_bps, int) or not 0 <= self.max_slippage_bps <= 10_000:
            raise RaydiumClmmCpiError("max_slippage_bps must be between 0 and 10000")


def decode_raydium_clmm_swap(data: bytes) -> DecodedRaydiumClmmSwap:
    """Decode the pinned Anchor legacy swap args: u64, u64, u128, bool."""
    if not isinstance(data, bytes) or len(data) < 8:
        raise RaydiumClmmCpiError("Raydium CLMM CPI data is missing or truncated")
    if data[:8] == RAYDIUM_CLMM_SWAP_V2_DISCRIMINATOR:
        raise RaydiumClmmCpiError("Raydium CLMM swap_v2 Token-2022-capable instruction is unsupported")
    if data[:8] != RAYDIUM_CLMM_SWAP_DISCRIMINATOR:
        raise RaydiumClmmCpiError("Raydium CLMM CPI discriminator is not the supported swap instruction")
    if len(data) != 41:
        raise RaydiumClmmCpiError("Raydium CLMM swap instruction has an invalid Borsh payload length")
    amount, threshold = struct.unpack("<QQ", data[8:24])
    sqrt_price_limit = int.from_bytes(data[24:40], "little")
    bool_value = data[40]
    if bool_value not in (0, 1):
        raise RaydiumClmmCpiError("Raydium CLMM is_base_input is not a canonical Borsh boolean")
    if amount <= 0 or threshold <= 0:
        raise RaydiumClmmCpiError("Raydium CLMM swap amount and minimum-output threshold must be positive")
    return DecodedRaydiumClmmSwap(
        amount_raw=amount,
        other_amount_threshold_raw=threshold,
        sqrt_price_limit_x64=sqrt_price_limit,
        is_base_input=bool(bool_value),
    )


@dataclass(frozen=True, slots=True)
class RaydiumClmmCpiResult:
    passed: bool
    message_sha256: str
    program_id: str
    outer_instruction_index: int
    cpi_instruction_index: int
    payer: str
    amm_config: str
    pool_state: str
    input_token_account: str
    output_token_account: str
    input_vault: str
    output_vault: str
    observation_state: str
    tick_array: str
    amount_raw: int
    minimum_output_raw: int
    sqrt_price_limit_x64: int
    is_base_input: bool
    stack_height: int
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT


def _find_jupiter_outer_instruction(
    order: JupiterOrderTransaction,
    validation: TransactionValidationResult,
    route_inspection: JupiterV6RouteInspection,
) -> int:
    try:
        message = from_bytes_versioned(order.message_bytes)
        static_keys = tuple(str(key) for key in message.account_keys)
    except Exception as exc:
        raise RaydiumClmmCpiError("validated Jupiter transaction message cannot be decoded") from exc
    if tuple(validation.account_keys[:len(static_keys)]) != static_keys:
        raise RaydiumClmmCpiError("resolved account-key order differs from the serialized Jupiter message")
    discriminator = (
        ROUTE_DISCRIMINATOR
        if route_inspection.instruction_name == "route"
        else SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR
        if route_inspection.instruction_name == "sharedAccountsRoute"
        else None
    )
    if discriminator is None:
        raise RaydiumClmmCpiError("Jupiter route inspection has an unsupported instruction name")
    matches = []
    for index, instruction in enumerate(message.instructions):
        program_index = int(instruction.program_id_index)
        if program_index >= len(validation.account_keys):
            raise RaydiumClmmCpiError("top-level Jupiter program index is outside resolved account keys")
        if (
            validation.account_keys[program_index] == route_inspection.program_id
            and bytes(instruction.data).startswith(discriminator)
        ):
            matches.append(index)
    if len(matches) != 1:
        raise RaydiumClmmCpiError("transaction must contain exactly one inspected Jupiter route instruction")
    return matches[0]


def evaluate_raydium_clmm_cpi(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_inspection: JupiterV6RouteInspection,
    swap_effects: JupiterSwapEffectResult,
    pool_evidence: DexPoolEvidenceSnapshot,
    pool_check: RaydiumClmmPoolRouteCheck,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    policy: RaydiumClmmCpiPolicy,
) -> RaydiumClmmCpiResult:
    """Require one direct Raydium CLMM legacy swap CPI to match all upstream route evidence."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmCpiError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise RaydiumClmmCpiError("structural transaction validation is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmCpiError("simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise RaydiumClmmCpiError("successful SimulationGate approval is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise RaydiumClmmCpiError("successful Jupiter program trace gate is required")
    if not isinstance(route_inspection, JupiterV6RouteInspection):
        raise RaydiumClmmCpiError("Jupiter V6 route inspection is required")
    if not isinstance(swap_effects, JupiterSwapEffectResult) or swap_effects.passed is not True:
        raise RaydiumClmmCpiError("successful Jupiter quote-to-balance effect gate is required")
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmCpiError("provenance-bound DEX pool evidence is required")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumClmmCpiError("successful Raydium CLMM pool-state check is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumClmmCpiError("verified Raydium CLMM vault snapshot is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmCpiError("verified Raydium CLMM tick-array snapshot is required")
    if not isinstance(policy, RaydiumClmmCpiPolicy):
        raise RaydiumClmmCpiError("Raydium CLMM CPI policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    digests = (
        validation.message_sha256,
        simulation.message_sha256,
        simulation_gate.message_sha256,
        route_inspection.message_sha256,
        swap_effects.message_sha256,
        pool_evidence.route_message_sha256,
        vault_snapshot.route_message_sha256,
        tick_array_snapshot.route_message_sha256,
    )
    if any(value != digest for value in digests):
        raise RaydiumClmmCpiError("order, simulation and upstream evidence message digests differ")
    if (
        simulation.provider_id != simulation_gate.provider_id
        or simulation.response_id != simulation_gate.response_id
        or simulation.context_slot != simulation_gate.context_slot
    ):
        raise RaydiumClmmCpiError("simulation evidence and SimulationGate refer to different RPC responses")
    for evidence in (pool_evidence, vault_snapshot, tick_array_snapshot):
        if (
            evidence.simulation_response_id != simulation.response_id
            or evidence.simulation_context_slot != simulation.context_slot
            or evidence.provider_id != simulation.provider_id
            or evidence.cluster != simulation.cluster
            or evidence.genesis_hash != simulation.genesis_hash
        ):
            raise RaydiumClmmCpiError("pool, vault, tick-array and simulation provenance differs")
    if simulation.simulation_error is not None:
        raise RaydiumClmmCpiError("simulation reported an execution error")
    if (
        tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(program_gate.inner_program_ids) != tuple(simulation.inner_program_ids)
    ):
        raise RaydiumClmmCpiError("program trace differs from validated transaction or simulation evidence")
    if not simulation.inner_instructions or len(simulation.inner_instructions) != simulation.inner_instruction_count:
        raise RaydiumClmmCpiError("complete raw inner-CPI instruction evidence is required")
    if tuple(item.program_id for item in simulation.inner_instructions) != simulation.inner_program_ids:
        raise RaydiumClmmCpiError("detailed CPI records differ from the compact simulation program trace")
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise RaydiumClmmCpiError("order taker is not the validated transaction fee payer")
    if order.router != "metis" or route_inspection.program_id not in program_gate.top_level_program_ids:
        raise RaydiumClmmCpiError("route does not use the supported Metis/Jupiter path")
    if len(order.route_plan) != 1 or len(route_inspection.route_steps) != 1:
        raise RaydiumClmmCpiError("only a single-hop route is supported")
    quoted_step = order.route_plan[0]
    inspected_step = route_inspection.route_steps[0]
    if (
        quoted_step.label != "Raydium CLMM"
        or inspected_step.dex_label != "Raydium CLMM"
        or inspected_step.amm_key != quoted_step.amm_key
        or program_gate.route_labels != ("Raydium CLMM",)
        or policy.program_id not in program_gate.dex_program_ids
    ):
        raise RaydiumClmmCpiError("Jupiter route and pinned DEX program trace are not a single Raydium CLMM hop")
    if pool_check.pool.pool_address != quoted_step.amm_key:
        raise RaydiumClmmCpiError("verified Raydium pool state does not match the quoted AMM key")
    pool = pool_check.pool
    if (
        pool_check.route_input_mint != quoted_step.input_mint
        or pool_check.route_output_mint != quoted_step.output_mint
        or {quoted_step.input_mint, quoted_step.output_mint} != {pool.token_mint_0, pool.token_mint_1}
    ):
        raise RaydiumClmmCpiError("Raydium pool mint pair differs from the quoted route hop")
    if route_inspection.input_amount_raw != order.input_amount_raw or route_inspection.quoted_output_amount_raw != order.output_amount_raw:
        raise RaydiumClmmCpiError("Jupiter route inspection amounts differ from the order")
    if route_inspection.slippage_bps > policy.max_slippage_bps:
        raise RaydiumClmmCpiError("Jupiter route slippage exceeds local Raydium CPI policy")
    if swap_effects.simulation_context_slot != simulation.context_slot:
        raise RaydiumClmmCpiError("quote-to-balance evidence refers to a different simulation slot")
    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    if (
        swap_effects.input_token_account != expected_input_ata
        or swap_effects.output_token_account != expected_output_ata
        or swap_effects.input_delta_raw != -order.input_amount_raw
        or swap_effects.output_delta_raw != order.output_amount_raw
    ):
        raise RaydiumClmmCpiError("quote-to-balance evidence differs from the exact taker route amounts")

    input_is_mint_0 = order.input_mint == pool.token_mint_0
    if not input_is_mint_0 and order.input_mint != pool.token_mint_1:
        raise RaydiumClmmCpiError("order input mint is not one of the Raydium pool mints")
    expected_input_vault = pool.token_vault_0 if input_is_mint_0 else pool.token_vault_1
    expected_output_vault = pool.token_vault_1 if input_is_mint_0 else pool.token_vault_0
    expected_tick_array = derive_raydium_clmm_tick_array_address(
        pool_address=pool.pool_address,
        start_tick_index=raydium_current_tick_array_start(pool.tick_current, pool.tick_spacing),
    )
    vault_by_address = {item.address: item for item in vault_snapshot.vaults}
    if len(vault_by_address) != len(vault_snapshot.vaults):
        raise RaydiumClmmCpiError("vault evidence contains duplicate account addresses")
    for address, mint in (
        (pool.token_vault_0, pool.token_mint_0),
        (pool.token_vault_1, pool.token_mint_1),
    ):
        item = vault_by_address.get(address)
        if item is None or item.mint != mint or item.token_authority != pool.pool_address:
            raise RaydiumClmmCpiError("verified vault evidence does not match the Raydium pool state")
        if item.token_program_id != CLASSIC_TOKEN_PROGRAM_ID:
            raise RaydiumClmmCpiError("Raydium route vault is not a classic SPL token account")
    tick_by_address = {item.address: item for item in tick_array_snapshot.tick_arrays}
    if len(tick_by_address) != len(tick_array_snapshot.tick_arrays):
        raise RaydiumClmmCpiError("tick-array evidence contains duplicate accounts")
    tick_array = tick_by_address.get(expected_tick_array)
    if tick_array is None or tick_array.pool_address != pool.pool_address:
        raise RaydiumClmmCpiError("verified current tick array is missing or belongs to another pool")

    outer_index = _find_jupiter_outer_instruction(order, validation, route_inspection)
    cpi_candidates = [
        (index, item)
        for index, item in enumerate(simulation.inner_instructions)
        if item.program_id == policy.program_id
    ]
    if len(cpi_candidates) != 1:
        raise RaydiumClmmCpiError("simulation must contain exactly one Raydium CLMM CPI for the single-hop route")
    cpi_index, cpi = cpi_candidates[0]
    if cpi.outer_instruction_index != outer_index:
        raise RaydiumClmmCpiError("Raydium CLMM CPI is not nested under the inspected Jupiter route instruction")
    if cpi.stack_height != 2:
        raise RaydiumClmmCpiError("Raydium CLMM CPI must be a direct child of the Jupiter route instruction")
    if len(cpi.account_keys) != RAYDIUM_CLMM_FIXED_ACCOUNT_COUNT:
        raise RaydiumClmmCpiError("only the ten-account classic SPL Raydium CLMM swap instruction is supported")
    try:
        for account in cpi.account_keys:
            if str(Pubkey.from_string(account)) != account:
                raise ValueError("non-canonical")
    except Exception as exc:
        raise RaydiumClmmCpiError("Raydium CLMM CPI contains a non-canonical account public key") from exc

    payer, amm_config, pool_state, input_account, output_account, input_vault, output_vault, observation, token_program, cpi_tick_array = cpi.account_keys
    if payer != order.taker:
        raise RaydiumClmmCpiError("Raydium CLMM CPI payer differs from the Jupiter taker")
    if amm_config != pool.amm_config or pool_state != pool.pool_address:
        raise RaydiumClmmCpiError("Raydium CLMM CPI config or pool account differs from verified PoolState")
    if input_account != expected_input_ata or output_account != expected_output_ata:
        raise RaydiumClmmCpiError("Raydium CLMM CPI user token accounts differ from the taker's input/output ATAs")
    if input_vault != expected_input_vault or output_vault != expected_output_vault:
        raise RaydiumClmmCpiError("Raydium CLMM CPI vault order differs from the input/output mint direction")
    if observation != pool.observation_key:
        raise RaydiumClmmCpiError("Raydium CLMM CPI observation account differs from PoolState")
    if token_program != CLASSIC_TOKEN_PROGRAM_ID:
        raise RaydiumClmmCpiError("Raydium CLMM legacy swap CPI must use the classic SPL Token program")
    if cpi_tick_array != expected_tick_array:
        raise RaydiumClmmCpiError("Raydium CLMM CPI tick array differs from the verified current tick array")
    if len(set(cpi.account_keys)) != RAYDIUM_CLMM_FIXED_ACCOUNT_COUNT:
        raise RaydiumClmmCpiError("Raydium CLMM fixed CPI account positions unexpectedly alias")

    decoded = decode_raydium_clmm_swap(cpi.data)
    if not decoded.is_base_input:
        raise RaydiumClmmCpiError("Raydium CLMM CPI must use exact-input mode")
    if decoded.amount_raw != order.input_amount_raw or decoded.amount_raw != quoted_step.input_amount_raw:
        raise RaydiumClmmCpiError("Raydium CLMM CPI input amount differs from the exact Jupiter route amount")
    if decoded.other_amount_threshold_raw < order.minimum_output_amount_raw:
        raise RaydiumClmmCpiError("Raydium CLMM minimum output is weaker than the Jupiter quote threshold")
    if decoded.other_amount_threshold_raw > order.output_amount_raw:
        raise RaydiumClmmCpiError("Raydium CLMM minimum output exceeds the quoted output amount")
    minimum_allowed = (
        order.output_amount_raw * (10_000 - policy.max_slippage_bps) + 9_999
    ) // 10_000
    if decoded.other_amount_threshold_raw < minimum_allowed:
        raise RaydiumClmmCpiError("Raydium CLMM minimum-output threshold allows more slippage than local policy")
    if decoded.sqrt_price_limit_x64 != 0:
        if not RAYDIUM_MIN_SQRT_PRICE_X64 < decoded.sqrt_price_limit_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
            raise RaydiumClmmCpiError("Raydium CLMM sqrt-price limit is outside protocol bounds")
        if input_is_mint_0 and decoded.sqrt_price_limit_x64 >= pool.sqrt_price_x64:
            raise RaydiumClmmCpiError("zero-for-one sqrt-price limit must be below the current pool price")
        if not input_is_mint_0 and decoded.sqrt_price_limit_x64 <= pool.sqrt_price_x64:
            raise RaydiumClmmCpiError("one-for-zero sqrt-price limit must be above the current pool price")

    return RaydiumClmmCpiResult(
        passed=True,
        message_sha256=digest,
        program_id=policy.program_id,
        outer_instruction_index=outer_index,
        cpi_instruction_index=cpi_index,
        payer=payer,
        amm_config=amm_config,
        pool_state=pool_state,
        input_token_account=input_account,
        output_token_account=output_account,
        input_vault=input_vault,
        output_vault=output_vault,
        observation_state=observation,
        tick_array=cpi_tick_array,
        amount_raw=decoded.amount_raw,
        minimum_output_raw=decoded.other_amount_threshold_raw,
        sqrt_price_limit_x64=decoded.sqrt_price_limit_x64,
        is_base_input=decoded.is_base_input,
        stack_height=cpi.stack_height or 0,
    )
