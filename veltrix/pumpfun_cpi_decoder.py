"""Pump.fun bonding-curve CPI decoder for Jupiter PumpdotfunWrapped routes.

IDL source: pump-fun/pump-public-docs/idl/pump.json at commit
639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22. Only reviewed buy/sell variants
and their v1/v2/v3 exact-in/exact-out forms are accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from veltrix.jupiter_instruction_decoder import JupiterSwapInstructionResult
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_swap_effects import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    CLASSIC_TOKEN_PROGRAM_ID,
    derive_associated_token_address,
)
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.simulation_gate import SimulationEvidence
from veltrix.transaction_validator import TransactionValidationResult


PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"

PUMP_INSTRUCTION_DISCRIMINATORS = {
    bytes([102, 6, 61, 18, 1, 218, 235, 234]): ("buy", "v1", "buy_exact_out"),
    bytes([51, 230, 133, 164, 1, 127, 131, 173]): ("sell", "v1", "sell_exact_in"),
    bytes([56, 252, 116, 8, 158, 223, 205, 95]): ("buy_exact_sol_in", "v1", "buy_exact_in"),
    bytes([184, 23, 238, 97, 103, 197, 211, 61]): ("buy_v2", "v2", "buy_exact_out"),
    bytes([194, 171, 28, 70, 104, 77, 91, 47]): ("buy_exact_quote_in_v2", "v2", "buy_exact_in"),
    bytes([93, 246, 130, 60, 231, 233, 64, 178]): ("sell_v2", "v2", "sell_exact_in"),
    bytes([7, 5, 29, 196, 245, 23, 101, 80]): ("buy_v3", "v3", "buy_exact_out"),
    bytes([225, 247, 80, 30, 213, 179, 132, 136]): ("buy_exact_quote_in_v3", "v3", "buy_exact_in"),
    bytes([28, 146, 222, 119, 38, 196, 105, 213]): ("sell_v3", "v3", "sell_exact_in"),
}


class PumpFunCpiDecodeError(ValueError):
    """Raised when Pump.fun CPI data or accounts disagree with the Jupiter order."""


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        if self.offset + size > len(self.data):
            raise PumpFunCpiDecodeError("Pump.fun CPI data is truncated")
        result = self.data[self.offset:self.offset + size]
        self.offset += size
        return result

    def u64(self) -> int:
        return int.from_bytes(self.read(8), "little")

    def boolean(self) -> bool:
        value = self.read(1)[0]
        if value not in (0, 1):
            raise PumpFunCpiDecodeError("Pump.fun OptionBool is not encoded as 0 or 1")
        return bool(value)


@dataclass(frozen=True, slots=True)
class PumpFunCpiPolicy:
    max_slippage_bps: int = 500
    max_stack_height: int = 8
    require_partial_fill_disabled: bool = True
    require_classic_token_program: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.max_slippage_bps, bool) or not isinstance(self.max_slippage_bps, int) or not 0 <= self.max_slippage_bps <= 10_000:
            raise PumpFunCpiDecodeError("max_slippage_bps must be between 0 and 10000")
        if isinstance(self.max_stack_height, bool) or not isinstance(self.max_stack_height, int) or not 1 <= self.max_stack_height <= 64:
            raise PumpFunCpiDecodeError("max_stack_height must be between 1 and 64")
        if not isinstance(self.require_partial_fill_disabled, bool) or not isinstance(self.require_classic_token_program, bool):
            raise PumpFunCpiDecodeError("Pump.fun policy switches must be booleans")


@dataclass(frozen=True, slots=True)
class PumpFunCpiResult:
    message_sha256: str
    instruction_name: str
    version: str
    direction: str
    amount_mode: str
    bonding_curve: str
    user: str
    base_mint: str
    quote_mint: str
    base_amount_raw: int
    quote_limit_raw: int
    quote_limit_kind: str
    partial_fill: bool
    track_volume: bool | None
    stack_height: int | None
    instruction_data_sha256: str


@dataclass(frozen=True, slots=True)
class PumpFunCpiGateResult:
    passed: bool
    message_sha256: str
    decoded_instructions: tuple[PumpFunCpiResult, ...]


def _parse_data(data: bytes) -> tuple[str, str, str, int, int, bool | None, bool]:
    if not isinstance(data, bytes) or len(data) < 8:
        raise PumpFunCpiDecodeError("Pump.fun CPI instruction data is missing")
    decoded = PUMP_INSTRUCTION_DISCRIMINATORS.get(data[:8])
    if decoded is None:
        raise PumpFunCpiDecodeError("Pump.fun CPI discriminator is not allowlisted")
    name, version, mode = decoded
    reader = _Reader(data[8:])
    track_volume: bool | None = None
    partial_fill = False
    if name == "buy":
        base_amount = reader.u64()
        quote_limit = reader.u64()
        track_volume = reader.boolean()
        partial_fill = reader.boolean()
    elif name == "buy_exact_sol_in":
        quote_limit = reader.u64()
        base_amount = reader.u64()
        track_volume = reader.boolean()
        partial_fill = reader.boolean()
    elif name in {"buy_v2", "buy_v3"}:
        base_amount = reader.u64()
        quote_limit = reader.u64()
        partial_fill = reader.boolean()
    elif name in {"buy_exact_quote_in_v2", "buy_exact_quote_in_v3"}:
        quote_limit = reader.u64()
        base_amount = reader.u64()
        partial_fill = reader.boolean()
    elif name in {"sell", "sell_v2", "sell_v3"}:
        base_amount = reader.u64()
        quote_limit = reader.u64()
    else:
        raise PumpFunCpiDecodeError("Pump.fun CPI instruction layout is not implemented")
    if reader.offset != len(reader.data):
        raise PumpFunCpiDecodeError("Pump.fun CPI instruction contains trailing or unknown data")
    if base_amount <= 0 or quote_limit <= 0:
        raise PumpFunCpiDecodeError("Pump.fun CPI amount or quote limit must be positive")
    return name, version, mode, base_amount, quote_limit, track_volume, partial_fill


def _account(
    inner_accounts: tuple[str, ...],
    validation: TransactionValidationResult,
    position: int,
    expected: str,
    *,
    signer: bool = False,
    writable: bool = False,
    role: str,
) -> None:
    if position < 0 or position >= len(inner_accounts):
        raise PumpFunCpiDecodeError(f"{role} account position is missing")
    actual = inner_accounts[position]
    if actual != expected:
        raise PumpFunCpiDecodeError(f"{role} account key differs from the approved route")
    try:
        global_index = validation.account_keys.index(actual)
    except ValueError as exc:
        raise PumpFunCpiDecodeError(f"{role} account is absent from the validated message") from exc
    if len(validation.account_signers) != len(validation.account_keys) or len(validation.account_writable) != len(validation.account_keys):
        raise PumpFunCpiDecodeError("validated account privilege evidence is incomplete")
    if validation.account_signers[global_index] is not signer:
        raise PumpFunCpiDecodeError(f"{role} signer privilege differs from the Pump.fun IDL")
    if writable and not validation.account_writable[global_index]:
        raise PumpFunCpiDecodeError(f"{role} must be writable in the validated message")
    if not signer and role in {"mint", "system program", "token program", "associated token program", "Pump.fun program", "fee program"} and validation.account_writable[global_index]:
        raise PumpFunCpiDecodeError(f"{role} must remain readonly in the validated message")


def _decode_pump_accounts(
    inner_accounts: tuple[str, ...],
    validation: TransactionValidationResult,
    *,
    name: str,
    version: str,
    order: JupiterOrderTransaction,
    amm_key: str,
    policy: PumpFunCpiPolicy,
) -> tuple[str, str]:
    direction = "buy" if name.startswith("buy") else "sell"
    if version == "v1":
        expected_count = 16 if name in {"buy", "buy_exact_sol_in"} else 14
        if len(inner_accounts) != expected_count:
            raise PumpFunCpiDecodeError(f"Pump.fun {name} requires exactly {expected_count} accounts")
        base_mint = order.output_mint if direction == "buy" else order.input_mint
        quote_mint = WRAPPED_SOL_MINT
        if (direction == "buy" and order.input_mint != WRAPPED_SOL_MINT) or (direction == "sell" and order.output_mint != WRAPPED_SOL_MINT):
            raise PumpFunCpiDecodeError("Pump.fun v1 bonding-curve routes require wrapped SOL as the quote mint")
        user_position = 6
        mint_position = 2
        bonding_curve_position = 3
        associated_curve_position = 4
        associated_user_position = 5
        token_program_position = 8 if direction == "buy" else 9
        program_position = 11
        fee_program_position = 15 if direction == "buy" else 13
        creator_vault_position = 9 if direction == "buy" else 8
        fee_recipient_position = 1
        _account(inner_accounts, validation, mint_position, base_mint, role="mint")
        _account(inner_accounts, validation, associated_curve_position, derive_associated_token_address(amm_key, base_mint), writable=True, role="associated bonding-curve token account")
        _account(inner_accounts, validation, associated_user_position, derive_associated_token_address(order.taker, base_mint), writable=True, role="user base token account")
        _account(inner_accounts, validation, 7, SYSTEM_PROGRAM_ID, role="system program")
        _account(inner_accounts, validation, token_program_position, CLASSIC_TOKEN_PROGRAM_ID, role="token program")
        _account(inner_accounts, validation, program_position, PUMP_PROGRAM_ID, role="Pump.fun program")
        _account(inner_accounts, validation, fee_program_position, PUMP_FEE_PROGRAM_ID, role="fee program")
        _account(inner_accounts, validation, 10, inner_accounts[10], role="event authority")
        _account(inner_accounts, validation, fee_recipient_position, inner_accounts[fee_recipient_position], writable=True, role="fee recipient")
        _account(inner_accounts, validation, creator_vault_position, inner_accounts[creator_vault_position], writable=True, role="creator vault")
        _account(inner_accounts, validation, bonding_curve_position, amm_key, writable=True, role="bonding curve")
        _account(inner_accounts, validation, user_position, order.taker, signer=True, writable=True, role="user")
        if direction == "buy":
            _account(inner_accounts, validation, 12, inner_accounts[12], role="global volume accumulator")
            _account(inner_accounts, validation, 13, inner_accounts[13], writable=True, role="user volume accumulator")
    elif version == "v2":
        expected_count = 27 if direction == "buy" else 26
        if len(inner_accounts) != expected_count:
            raise PumpFunCpiDecodeError(f"Pump.fun {name} requires exactly {expected_count} accounts")
        base_mint = order.output_mint if direction == "buy" else order.input_mint
        quote_mint = order.input_mint if direction == "buy" else order.output_mint
        _account(inner_accounts, validation, 1, base_mint, role="base mint")
        _account(inner_accounts, validation, 2, quote_mint, role="quote mint")
        _account(inner_accounts, validation, 3, CLASSIC_TOKEN_PROGRAM_ID, role="base token program")
        _account(inner_accounts, validation, 4, CLASSIC_TOKEN_PROGRAM_ID, role="quote token program")
        _account(inner_accounts, validation, 5, ASSOCIATED_TOKEN_PROGRAM_ID, role="associated token program")
        _account(inner_accounts, validation, 10, amm_key, writable=True, role="bonding curve")
        _account(inner_accounts, validation, 11, derive_associated_token_address(amm_key, base_mint), writable=True, role="associated base bonding-curve account")
        _account(inner_accounts, validation, 12, derive_associated_token_address(amm_key, quote_mint), writable=True, role="associated quote bonding-curve account")
        _account(inner_accounts, validation, 13, order.taker, signer=True, writable=True, role="user")
        _account(inner_accounts, validation, 14, derive_associated_token_address(order.taker, base_mint), writable=True, role="user base token account")
        _account(inner_accounts, validation, 15, derive_associated_token_address(order.taker, quote_mint), writable=True, role="user quote token account")
        _account(inner_accounts, validation, 23 if direction == "buy" else 22, PUMP_FEE_PROGRAM_ID, role="fee program")
        _account(inner_accounts, validation, 24 if direction == "buy" else 23, SYSTEM_PROGRAM_ID, role="system program")
        _account(inner_accounts, validation, 26 if direction == "buy" else 25, PUMP_PROGRAM_ID, role="Pump.fun program")
    elif version == "v3":
        if len(inner_accounts) != 17:
            raise PumpFunCpiDecodeError(f"Pump.fun {name} requires exactly 17 accounts")
        base_mint = order.output_mint if direction == "buy" else order.input_mint
        quote_mint = order.input_mint if direction == "buy" else order.output_mint
        _account(inner_accounts, validation, 1, base_mint, role="base mint")
        _account(inner_accounts, validation, 2, quote_mint, role="quote mint")
        _account(inner_accounts, validation, 3, CLASSIC_TOKEN_PROGRAM_ID, role="base token program")
        _account(inner_accounts, validation, 4, CLASSIC_TOKEN_PROGRAM_ID, role="quote token program")
        _account(inner_accounts, validation, 5, amm_key, writable=True, role="bonding curve")
        _account(inner_accounts, validation, 6, derive_associated_token_address(amm_key, base_mint), writable=True, role="associated base bonding-curve account")
        _account(inner_accounts, validation, 7, derive_associated_token_address(amm_key, quote_mint), writable=True, role="associated quote bonding-curve account")
        _account(inner_accounts, validation, 8, order.taker, signer=True, writable=True, role="user")
        _account(inner_accounts, validation, 9, derive_associated_token_address(order.taker, base_mint), writable=True, role="user base token account")
        _account(inner_accounts, validation, 10, derive_associated_token_address(order.taker, quote_mint), writable=True, role="user quote token account")
        _account(inner_accounts, validation, 14, SYSTEM_PROGRAM_ID, role="system program")
        _account(inner_accounts, validation, 16, PUMP_PROGRAM_ID, role="Pump.fun program")
    else:
        raise PumpFunCpiDecodeError("Pump.fun CPI version is not supported")
    return base_mint, quote_mint


def decode_pumpfun_route_cpis(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    jupiter_instruction: JupiterSwapInstructionResult,
    simulation: SimulationEvidence,
    route_gate: JupiterRouteProgramGateResult,
    policy: PumpFunCpiPolicy,
) -> PumpFunCpiGateResult:
    """Decode every Pump.fun CPI in a previously approved Jupiter route trace."""
    if not isinstance(order, JupiterOrderTransaction):
        raise PumpFunCpiDecodeError("Jupiter v0 order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise PumpFunCpiDecodeError("validated transaction evidence is required")
    if not isinstance(jupiter_instruction, JupiterSwapInstructionResult):
        raise PumpFunCpiDecodeError("decoded Jupiter route instruction is required")
    if not isinstance(simulation, SimulationEvidence):
        raise PumpFunCpiDecodeError("simulated inner-CPI evidence is required")
    if not isinstance(route_gate, JupiterRouteProgramGateResult) or route_gate.passed is not True:
        raise PumpFunCpiDecodeError("successful Jupiter route/CPI gate is required")
    if not isinstance(policy, PumpFunCpiPolicy):
        raise PumpFunCpiDecodeError("Pump.fun CPI policy is required")
    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if digest != validation.message_sha256 or digest != jupiter_instruction.message_sha256 or digest != simulation.message_sha256 or digest != route_gate.message_sha256:
        raise PumpFunCpiDecodeError("Pump.fun decoder inputs refer to different transaction messages")
    if PUMP_PROGRAM_ID not in route_gate.dex_program_ids:
        raise PumpFunCpiDecodeError("Pump.fun program is not part of the locally pinned Jupiter route")
    if jupiter_instruction.top_level_instruction_index != route_gate.parent_instruction_index:
        raise PumpFunCpiDecodeError("Jupiter route parent instruction differs from route-gate evidence")

    decoded_results = []
    pump_steps = [
        (route, decoded)
        for route, decoded in zip(order.route_plan, jupiter_instruction.route_plan)
        if route.label == "Pump.Fun"
    ]
    if not pump_steps:
        raise PumpFunCpiDecodeError("Pump.fun CPI decoder is not applicable to this DEX route")
    for route, decoded_route in pump_steps:
        expected_direction = "buy" if decoded_route.swap_variant == "PumpdotfunWrappedBuy" else "sell" if decoded_route.swap_variant == "PumpdotfunWrappedSell" else None
        if expected_direction is None:
            raise PumpFunCpiDecodeError("Pump.fun route label is paired with an unsupported Jupiter swap variant")
        candidates = [
            item for item in simulation.inner_instructions
            if item.program_id == PUMP_PROGRAM_ID and route.amm_key in item.account_keys
        ]
        if len(candidates) != 1:
            raise PumpFunCpiDecodeError("Pump.fun route pool must map to exactly one simulated Pump.fun CPI")
        inner = candidates[0]
        if inner.top_level_instruction_index != jupiter_instruction.top_level_instruction_index:
            raise PumpFunCpiDecodeError("Pump.fun CPI is nested under a different top-level instruction")
        if inner.stack_height is not None and inner.stack_height > policy.max_stack_height:
            raise PumpFunCpiDecodeError("Pump.fun CPI stack height exceeds local policy")
        name, version, amount_mode, base_amount, quote_limit, track_volume, partial_fill = _parse_data(inner.data)
        direction = "buy" if name.startswith("buy") else "sell"
        if direction != expected_direction:
            raise PumpFunCpiDecodeError("Pump.fun CPI direction differs from Jupiter route variant")
        base_mint, quote_mint = _decode_pump_accounts(
            inner.account_keys,
            validation,
            name=name,
            version=version,
            order=order,
            amm_key=route.amm_key,
            policy=policy,
        )
        if policy.require_partial_fill_disabled and partial_fill:
            raise PumpFunCpiDecodeError("Pump.fun partial fills are disabled by local policy")
        if amount_mode == "buy_exact_out":
            if base_amount != order.output_amount_raw:
                raise PumpFunCpiDecodeError("Pump.fun exact-output base amount differs from Jupiter quoted output")
            slippage_bps = order.slippage_bps if order.slippage_bps is not None else jupiter_instruction.slippage_bps
            max_allowed_quote = (order.input_amount_raw * (10_000 + slippage_bps) + 9_999) // 10_000
            if quote_limit > max_allowed_quote:
                raise PumpFunCpiDecodeError("Pump.fun max quote spend exceeds Jupiter input plus slippage ceiling")
            quote_limit_kind = "max_input"
        elif amount_mode == "buy_exact_in":
            if quote_limit != order.input_amount_raw:
                raise PumpFunCpiDecodeError("Pump.fun exact-input quote amount differs from Jupiter input")
            if base_amount < order.minimum_output_amount_raw:
                raise PumpFunCpiDecodeError("Pump.fun minimum base output is below Jupiter minimum output")
            quote_limit_kind = "exact_input"
        elif amount_mode == "sell_exact_in":
            if base_amount != order.input_amount_raw:
                raise PumpFunCpiDecodeError("Pump.fun exact-input base amount differs from Jupiter input")
            if quote_limit < order.minimum_output_amount_raw:
                raise PumpFunCpiDecodeError("Pump.fun minimum quote output is below Jupiter minimum output")
            quote_limit_kind = "min_output"
        else:
            raise PumpFunCpiDecodeError("Pump.fun amount mode is not supported")

        decoded_results.append(PumpFunCpiResult(
            message_sha256=digest,
            instruction_name=name,
            version=version,
            direction=direction,
            amount_mode=amount_mode,
            bonding_curve=route.amm_key,
            user=order.taker,
            base_mint=base_mint,
            quote_mint=quote_mint,
            base_amount_raw=base_amount,
            quote_limit_raw=quote_limit,
            quote_limit_kind=quote_limit_kind,
            partial_fill=partial_fill,
            track_volume=track_volume,
            stack_height=inner.stack_height,
            instruction_data_sha256=hashlib.sha256(inner.data).hexdigest(),
        ))

    return PumpFunCpiGateResult(
        passed=True,
        message_sha256=digest,
        decoded_instructions=tuple(decoded_results),
    )
