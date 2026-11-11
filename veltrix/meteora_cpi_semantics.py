"""Narrow semantic gate for Jupiter-invoked Meteora DLMM swap2 CPIs.

Supports only classic SPL-token exact-input swap2 instructions with an empty
remaining-accounts-info vector. Token-2022 transfer hooks and exact-output are
rejected. No signing or submission is implemented.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import derive_associated_token_address
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


METEORA_DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
METEORA_DLMM_IDL_REPOSITORY = "MeteoraAg/dlmm-sdk"
METEORA_DLMM_IDL_COMMIT = "1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1"
METEORA_SWAP2_DISCRIMINATOR = bytes((65, 75, 63, 76, 235, 91, 91, 136))
CLASSIC_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
METEORA_SWAP2_FIXED_ACCOUNT_COUNT = 16
MAX_METEORA_BIN_ARRAYS = 8


class MeteoraCpiSemanticError(ValueError):
    """Raised when Meteora DLMM CPI bytes or accounts differ from the approved route."""


@dataclass(frozen=True, slots=True)
class DecodedMeteoraSwap2:
    amount_in_raw: int
    minimum_output_raw: int
    remaining_accounts_info_count: int


@dataclass(frozen=True, slots=True)
class MeteoraCpiPolicy:
    allowed_program_ids: tuple[str, ...] = (METEORA_DLMM_PROGRAM_ID,)
    allowed_auxiliary_program_ids: tuple[str, ...] = (CLASSIC_TOKEN_PROGRAM_ID,)
    max_cpi_count: int = 12
    max_bin_arrays: int = MAX_METEORA_BIN_ARRAYS

    def __post_init__(self) -> None:
        for field in ("allowed_program_ids", "allowed_auxiliary_program_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or not values:
                raise MeteoraCpiSemanticError(f"{field} must be a non-empty immutable tuple")
            for value in values:
                try:
                    if str(Pubkey.from_string(value)) != value:
                        raise ValueError("non-canonical")
                except Exception as exc:
                    raise MeteoraCpiSemanticError(f"{field} contains an invalid public key") from exc
            if len(set(values)) != len(values):
                raise MeteoraCpiSemanticError(f"{field} must not contain duplicates")
        if set(self.allowed_program_ids) & set(self.allowed_auxiliary_program_ids):
            raise MeteoraCpiSemanticError("DEX and auxiliary program allowlists must not overlap")
        for field, low, high in (
            ("max_cpi_count", 1, 64),
            ("max_bin_arrays", 1, MAX_METEORA_BIN_ARRAYS),
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise MeteoraCpiSemanticError(f"{field} is outside its permitted range")


@dataclass(frozen=True, slots=True)
class MeteoraCpiSemanticResult:
    passed: bool
    message_sha256: str
    outer_instruction_index: int
    cpi_count: int
    lb_pair_ids: tuple[str, ...]
    input_amounts_raw: tuple[int, ...]
    minimum_output_amounts_raw: tuple[int, ...]
    idl_commit: str = METEORA_DLMM_IDL_COMMIT


def decode_meteora_swap2(data: bytes) -> DecodedMeteoraSwap2:
    if not isinstance(data, bytes) or len(data) != 28:
        raise MeteoraCpiSemanticError("Meteora swap2 data must be exactly 28 bytes")
    if data[:8] != METEORA_SWAP2_DISCRIMINATOR:
        raise MeteoraCpiSemanticError("Meteora CPI is not the pinned swap2 instruction")
    amount_in, minimum_output = struct.unpack("<QQ", data[8:24])
    remaining_count = struct.unpack("<I", data[24:28])[0]
    if amount_in <= 0 or minimum_output <= 0:
        raise MeteoraCpiSemanticError("Meteora input amount and minimum output must be positive")
    if remaining_count != 0:
        raise MeteoraCpiSemanticError("Meteora remaining-account slices (transfer hooks) are not supported")
    return DecodedMeteoraSwap2(amount_in, minimum_output, remaining_count)


def _canonical_account(value: str, field: str) -> str:
    try:
        canonical = str(Pubkey.from_string(value))
    except Exception as exc:
        raise MeteoraCpiSemanticError(f"{field} is not a valid public key") from exc
    if canonical != value:
        raise MeteoraCpiSemanticError(f"{field} is not canonical")
    return canonical


def _event_authority(program_id: str) -> str:
    try:
        address, _ = Pubkey.find_program_address(
            [b"__event_authority"],
            Pubkey.from_string(program_id),
        )
        return str(address)
    except Exception as exc:
        raise MeteoraCpiSemanticError("could not derive Meteora event authority PDA") from exc


def evaluate_meteora_dlmm_cpis(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    policy: MeteoraCpiPolicy,
) -> MeteoraCpiSemanticResult:
    """Decode every Meteora DLMM swap2 CPI and bind it to the Jupiter quote."""
    if not isinstance(order, JupiterOrderTransaction):
        raise MeteoraCpiSemanticError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise MeteoraCpiSemanticError("validated transaction is required")
    if not isinstance(simulation, SimulationEvidence):
        raise MeteoraCpiSemanticError("detailed simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise MeteoraCpiSemanticError("successful simulation gate is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise MeteoraCpiSemanticError("successful Jupiter program trace gate is required")
    if not isinstance(route_semantics, JupiterRouteSemanticResult) or route_semantics.passed is not True:
        raise MeteoraCpiSemanticError("successful Jupiter route semantic gate is required")
    if not isinstance(policy, MeteoraCpiPolicy):
        raise MeteoraCpiSemanticError("Meteora CPI policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256, simulation.message_sha256, simulation_gate.message_sha256,
        program_gate.message_sha256, route_semantics.message_sha256,
    )):
        raise MeteoraCpiSemanticError("order, validation, simulation and semantic evidence digests differ")
    if simulation.simulation_error is not None:
        raise MeteoraCpiSemanticError("simulation reported an execution error")
    if (
        simulation.recent_blockhash != order.recent_blockhash
        or simulation.last_valid_block_height != order.last_valid_block_height
    ):
        raise MeteoraCpiSemanticError("simulation blockhash or last-valid height differs from the Jupiter order")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise MeteoraCpiSemanticError("simulation gate and detailed evidence refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise MeteoraCpiSemanticError("simulation context slots differ")
    if (
        tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
    ):
        raise MeteoraCpiSemanticError("top-level program trace differs from the validated transaction")
    if tuple(item.program_id for item in simulation.inner_instructions) != simulation.inner_program_ids:
        raise MeteoraCpiSemanticError("detailed inner CPI evidence is missing or inconsistent")
    if tuple(program_gate.inner_program_ids) != simulation.inner_program_ids:
        raise MeteoraCpiSemanticError("Jupiter program trace differs from detailed simulation CPI evidence")
    if tuple(program_gate.route_labels) != tuple(dict.fromkeys(step.label for step in order.route_plan)):
        raise MeteoraCpiSemanticError("Jupiter program trace labels differ from the quote route plan")
    if not set(policy.allowed_program_ids).intersection(program_gate.dex_program_ids):
        raise MeteoraCpiSemanticError("Meteora program is not present in the approved DEX program trace")
    if len(simulation.inner_instructions) != simulation.inner_instruction_count or not simulation.inner_instructions:
        raise MeteoraCpiSemanticError("complete detailed inner CPI instruction evidence is required")
    if not order.route_plan or len(order.route_plan) > policy.max_cpi_count:
        raise MeteoraCpiSemanticError("Jupiter route plan is empty or exceeds the CPI count policy")
    if len(route_semantics.route_steps) != len(order.route_plan):
        raise MeteoraCpiSemanticError("decoded Jupiter route-step count differs from the quote")
    if any(step.label != "Meteora DLMM" for step in order.route_plan):
        raise MeteoraCpiSemanticError("this DEX semantic gate supports only all-Meteora-DLMM routes")
    if any(step.swap_variant_index != 38 for step in route_semantics.route_steps):
        raise MeteoraCpiSemanticError("decoded Jupiter route contains a non-Meteora-DLMM swap enum")

    dex_cpis = []
    for inner in simulation.inner_instructions:
        if inner.outer_instruction_index != route_semantics.instruction_index:
            continue
        if inner.program_id in policy.allowed_program_ids:
            dex_cpis.append(inner)
        elif inner.program_id not in policy.allowed_auxiliary_program_ids:
            raise MeteoraCpiSemanticError("simulation contains an unapproved inner CPI program")
    if len(dex_cpis) != len(order.route_plan):
        raise MeteoraCpiSemanticError("Meteora DLMM CPI count differs from the quoted route step count")

    event_authorities = {_event_authority(program_id) for program_id in policy.allowed_program_ids}
    lb_pairs = []
    amounts = []
    minimum_outputs = []
    seen_pairs = set()
    token_index_accounts: dict[int, str] = {}
    for index, (step, inner, decoded_route_step) in enumerate(zip(order.route_plan, dex_cpis, route_semantics.route_steps)):
        if inner.program_id not in policy.allowed_program_ids:
            raise MeteoraCpiSemanticError("Meteora CPI program ID is not locally pinned")
        if inner.stack_height != 2:
            raise MeteoraCpiSemanticError("Meteora CPI must be a direct depth-2 child of the Jupiter route instruction")
        if len(inner.account_keys) <= METEORA_SWAP2_FIXED_ACCOUNT_COUNT:
            raise MeteoraCpiSemanticError("Meteora swap2 CPI is missing required bin-array remaining accounts")
        if len(inner.account_keys) > METEORA_SWAP2_FIXED_ACCOUNT_COUNT + policy.max_bin_arrays:
            raise MeteoraCpiSemanticError("Meteora swap2 CPI has too many remaining bin-array accounts")
        accounts = tuple(_canonical_account(value, f"Meteora CPI account {position}") for position, value in enumerate(inner.account_keys))
        fixed_unique = (accounts[0], accounts[2], accounts[3], accounts[4], accounts[5], accounts[6], accounts[7], accounts[8], accounts[10], accounts[13], accounts[14], accounts[15])
        if len(set(fixed_unique)) != len(fixed_unique):
            raise MeteoraCpiSemanticError("Meteora swap2 CPI contains aliased fixed accounts")
        if accounts[1] != inner.program_id and accounts[1] in fixed_unique:
            raise MeteoraCpiSemanticError("Meteora bitmap-extension account aliases a required account")
        bin_arrays = accounts[METEORA_SWAP2_FIXED_ACCOUNT_COUNT:]
        if len(set(bin_arrays)) != len(bin_arrays) or set(bin_arrays) & set(fixed_unique):
            raise MeteoraCpiSemanticError("Meteora bin-array remaining accounts contain aliases")
        if accounts[0] != step.amm_key:
            raise MeteoraCpiSemanticError("Meteora LB pair differs from the quoted AMM key")
        if accounts[1] == accounts[9] and accounts[1] != inner.program_id:
            raise MeteoraCpiSemanticError("Meteora optional bitmap-extension account unexpectedly aliases the host-fee account")
        if accounts[2] == accounts[3] or accounts[4] == accounts[5]:
            raise MeteoraCpiSemanticError("Meteora reserves or user token accounts unexpectedly alias")
        if accounts[9] != inner.program_id:
            raise MeteoraCpiSemanticError("Meteora host-fee account must be omitted using the program-ID sentinel")
        if accounts[10] != order.taker:
            raise MeteoraCpiSemanticError("Meteora user signer differs from the Jupiter taker")
        if accounts[11] != CLASSIC_TOKEN_PROGRAM_ID or accounts[12] != CLASSIC_TOKEN_PROGRAM_ID:
            raise MeteoraCpiSemanticError("Meteora route must use classic SPL Token for both assets")
        if accounts[13] != MEMO_PROGRAM_ID:
            raise MeteoraCpiSemanticError("Meteora swap2 memo program differs from the pinned IDL")
        if accounts[14] not in event_authorities:
            raise MeteoraCpiSemanticError("Meteora event-authority PDA is invalid")
        if accounts[15] != inner.program_id:
            raise MeteoraCpiSemanticError("Meteora program account differs from the CPI program ID")
        if {accounts[6], accounts[7]} != {step.input_mint, step.output_mint}:
            raise MeteoraCpiSemanticError("Meteora pair mint accounts differ from the quoted route-step mints")
        decoded = decode_meteora_swap2(inner.data)
        if decoded.amount_in_raw != step.input_amount_raw:
            raise MeteoraCpiSemanticError("Meteora input amount differs from the quoted route-step amount")
        if decoded.minimum_output_raw > step.output_amount_raw:
            raise MeteoraCpiSemanticError("Meteora minimum output threshold exceeds the quoted route-step output")
        for token_index, account in (
            (decoded_route_step.input_index, accounts[4]),
            (decoded_route_step.output_index, accounts[5]),
        ):
            previous_account = token_index_accounts.setdefault(token_index, account)
            if previous_account != account:
                raise MeteoraCpiSemanticError("Meteora CPI token accounts do not preserve Jupiter route token-index flow")
        if accounts[0] in seen_pairs:
            raise MeteoraCpiSemanticError("duplicate Meteora LB pair appears in the same route")
        seen_pairs.add(accounts[0])
        lb_pairs.append(accounts[0])
        amounts.append(decoded.amount_in_raw)
        minimum_outputs.append(decoded.minimum_output_raw)

    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    output_indexes = tuple(index for index, mint in route_semantics.token_index_mints if mint == order.output_mint)
    if len(output_indexes) != 1:
        raise MeteoraCpiSemanticError("decoded Jupiter route does not identify exactly one final output token index")
    if token_index_accounts.get(0) != expected_input_ata:
        raise MeteoraCpiSemanticError("Meteora route input token account is not the taker's classic SPL input ATA")
    if token_index_accounts.get(output_indexes[0]) != expected_output_ata:
        raise MeteoraCpiSemanticError("Meteora route output token account is not the taker's classic SPL output ATA")

    return MeteoraCpiSemanticResult(
        passed=True,
        message_sha256=digest,
        outer_instruction_index=route_semantics.instruction_index,
        cpi_count=len(dex_cpis),
        lb_pair_ids=tuple(lb_pairs),
        input_amounts_raw=tuple(amounts),
        minimum_output_amounts_raw=tuple(minimum_outputs),
    )
