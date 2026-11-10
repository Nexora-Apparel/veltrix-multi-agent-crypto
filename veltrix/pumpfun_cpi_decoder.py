"""Pump.fun bonding-curve CPI decoder for Jupiter PumpdotfunWrapped routes.

IDL source: pump-fun/pump-public-docs/idl/pump.json at commit
639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22. Only reviewed buy/sell variants
and their v1/v2/v3 exact-in/exact-out forms are accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey

from veltrix.jupiter_instruction_decoder import JupiterSwapInstructionResult
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_swap_effects import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    CLASSIC_TOKEN_PROGRAM_ID,
    derive_associated_token_address,
)
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.simulation_gate import SimulatedAccountSnapshot, SimulationEvidence
from veltrix.transaction_validator import TransactionValidationResult


PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PUMP_GLOBAL_DISCRIMINATOR = bytes([167, 232, 232, 177, 200, 108, 114, 127])
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

    def __post_init__(self) -> None:
        if isinstance(self.max_slippage_bps, bool) or not isinstance(self.max_slippage_bps, int) or not 0 <= self.max_slippage_bps <= 10_000:
            raise PumpFunCpiDecodeError("max_slippage_bps must be between 0 and 10000")
        if isinstance(self.max_stack_height, bool) or not isinstance(self.max_stack_height, int) or not 1 <= self.max_stack_height <= 64:
            raise PumpFunCpiDecodeError("max_stack_height must be between 1 and 64")
        if not isinstance(self.require_partial_fill_disabled, bool):
            raise PumpFunCpiDecodeError("require_partial_fill_disabled must be a boolean")


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
    global_config_sha256: str
    global_config_slot: int


@dataclass(frozen=True, slots=True)
class PumpFunCpiGateResult:
    passed: bool
    message_sha256: str
    decoded_instructions: tuple[PumpFunCpiResult, ...]
    global_config_sha256: str
    global_config_slot: int


class _GlobalReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 8

    def read(self, size: int) -> bytes:
        if self.offset + size > len(self.data):
            raise PumpFunCpiDecodeError("Pump.fun Global account data is truncated")
        value = self.data[self.offset:self.offset + size]
        self.offset += size
        return value

    def boolean(self) -> bool:
        value = self.read(1)[0]
        if value not in (0, 1):
            raise PumpFunCpiDecodeError("Pump.fun Global boolean field is malformed")
        return bool(value)

    def u64(self) -> int:
        return int.from_bytes(self.read(8), "little")

    def pubkey(self) -> str:
        raw = self.read(32)
        number = int.from_bytes(raw, "big")
        encoded = ""
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        while number:
            number, remainder = divmod(number, 58)
            encoded = alphabet[remainder] + encoded
        leading_zeroes = len(raw) - len(raw.lstrip(b"\\x00"))
        address = "1" * leading_zeroes + (encoded or "")
        try:
            return str(Pubkey.from_string(address))
        except Exception as exc:
            raise PumpFunCpiDecodeError("Pump.fun Global contains an invalid public key") from exc

    def skip(self, size: int) -> None:
        self.read(size)


@dataclass(frozen=True, slots=True)
class PumpFunGlobalConfigSnapshot:
    address: str
    owner: str
    context_slot: int
    data_sha256: str
    fee_recipient: str
    fee_recipients: tuple[str, ...]
    reserved_fee_recipient: str
    reserved_fee_recipients: tuple[str, ...]
    buyback_fee_recipients: tuple[str, ...]

    @property
    def fee_recipient_allowlist(self) -> tuple[str, ...]:
        default = str(Pubkey.default())
        return tuple(sorted({
            key for key in (self.fee_recipient, *self.fee_recipients, self.reserved_fee_recipient, *self.reserved_fee_recipients)
            if key != default
        }))

    @property
    def buyback_fee_recipient_allowlist(self) -> tuple[str, ...]:
        default = str(Pubkey.default())
        return tuple(sorted({key for key in self.buyback_fee_recipients if key != default}))


def decode_pumpfun_global_config(
    snapshot: SimulatedAccountSnapshot,
    *,
    expected_context_slot: int,
) -> PumpFunGlobalConfigSnapshot:
    if not isinstance(snapshot, SimulatedAccountSnapshot):
        raise PumpFunCpiDecodeError("same-slot Pump.fun Global account snapshot is required")
    global_address = _pda((b"global",), PUMP_PROGRAM_ID)
    if snapshot.address != global_address:
        raise PumpFunCpiDecodeError("Pump.fun Global snapshot address is not the pinned Global PDA")
    if snapshot.owner != PUMP_PROGRAM_ID or snapshot.executable:
        raise PumpFunCpiDecodeError("Pump.fun Global snapshot owner or executable status is invalid")
    if snapshot.context_slot != expected_context_slot:
        raise PumpFunCpiDecodeError("Pump.fun Global snapshot is not from the simulation context slot")
    if snapshot.lamports <= 0 or not snapshot.data.startswith(PUMP_GLOBAL_DISCRIMINATOR):
        raise PumpFunCpiDecodeError("Pump.fun Global account is absent or has the wrong Anchor discriminator")
    reader = _GlobalReader(snapshot.data)
    initialized = reader.boolean()
    if not initialized:
        raise PumpFunCpiDecodeError("Pump.fun Global account is not initialized")
    reader.skip(32)  # authority
    fee_recipient = reader.pubkey()
    reader.skip(5 * 8)  # initial reserves, supply and fee basis points
    reader.skip(32)  # withdraw authority
    reader.boolean()  # enable_migrate
    reader.u64()  # pool_migration_fee
    reader.u64()  # creator_fee_basis_points
    fee_recipients = tuple(reader.pubkey() for _ in range(7))
    reader.skip(32)  # set_creator_authority
    reader.skip(32)  # admin_set_creator_authority
    reader.boolean()  # create_v2_enabled
    reader.skip(32)  # whitelist_pda
    reserved_fee_recipient = reader.pubkey()
    reader.boolean()  # mayhem_mode_enabled
    reserved_fee_recipients = tuple(reader.pubkey() for _ in range(7))
    reader.boolean()  # cashback enabled
    buyback_fee_recipients = tuple(reader.pubkey() for _ in range(8))
    reader.u64()  # buyback basis points
    reader.u64()  # initial virtual quote reserves
    reader.skip(32)  # whitelisted quote mints
    reader.boolean()  # creator fee configurable
    reader.u64()  # max configurable creator fee
    reader.skip(32)  # holder reward claim authority
    reader.boolean()  # holder reward enabled
    reader.read(1)  # max curve depth
    if reader.offset != len(reader.data):
        raise PumpFunCpiDecodeError("Pump.fun Global account has an unexpected trailing layout")
    result = PumpFunGlobalConfigSnapshot(
        address=snapshot.address,
        owner=snapshot.owner,
        context_slot=snapshot.context_slot,
        data_sha256=hashlib.sha256(snapshot.data).hexdigest(),
        fee_recipient=fee_recipient,
        fee_recipients=fee_recipients,
        reserved_fee_recipient=reserved_fee_recipient,
        reserved_fee_recipients=reserved_fee_recipients,
        buyback_fee_recipients=buyback_fee_recipients,
    )
    if not result.fee_recipient_allowlist:
        raise PumpFunCpiDecodeError("Pump.fun Global account contains no active fee recipients")
    return result


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


def _pda(seeds: tuple[bytes, ...], program_id: str) -> str:
    try:
        address, _ = Pubkey.find_program_address(list(seeds), Pubkey.from_string(program_id))
    except Exception as exc:
        raise PumpFunCpiDecodeError("Pump.fun PDA derivation failed") from exc
    return str(address)


def _account(
    inner_accounts: tuple[str, ...],
    validation: TransactionValidationResult,
    position: int,
    expected: str,
    *,
    signer: bool = False,
    writable: bool | None = None,
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
    if writable is True and not validation.account_writable[global_index]:
        raise PumpFunCpiDecodeError(f"{role} must be writable in the validated message")
    if writable is False and validation.account_writable[global_index]:
        raise PumpFunCpiDecodeError(f"{role} must be readonly in the validated message")


def _fee_recipient(
    inner_accounts: tuple[str, ...],
    validation: TransactionValidationResult,
    position: int,
    allowed_addresses: tuple[str, ...],
    *,
    role: str,
) -> str:
    if position < 0 or position >= len(inner_accounts):
        raise PumpFunCpiDecodeError(f"{role} account position is missing")
    address = inner_accounts[position]
    if address not in allowed_addresses:
        raise PumpFunCpiDecodeError(f"{role} is not in the independently verified fee-recipient allowlist")
    _account(inner_accounts, validation, position, address, writable=True, role=role)
    return address


def _decode_pump_accounts(
    inner_accounts: tuple[str, ...],
    validation: TransactionValidationResult,
    *,
    name: str,
    version: str,
    order: JupiterOrderTransaction,
    amm_key: str,
    policy: PumpFunCpiPolicy,
    global_config: PumpFunGlobalConfigSnapshot,
) -> tuple[str, str]:
    direction = "buy" if name.startswith("buy") else "sell"
    global_pda = _pda((b"global",), PUMP_PROGRAM_ID)
    event_pda = _pda((b"__event_authority",), PUMP_PROGRAM_ID)
    global_volume_pda = _pda((b"global_volume_accumulator",), PUMP_PROGRAM_ID)
    user_volume_pda = _pda(
        (b"user_volume_accumulator", bytes(Pubkey.from_string(order.taker))),
        PUMP_PROGRAM_ID,
    )
    fee_config_pda = _pda(
        (b"fee_config", bytes(Pubkey.from_string(PUMP_PROGRAM_ID))),
        PUMP_FEE_PROGRAM_ID,
    )

    if version == "v1":
        expected_count = 16 if direction == "buy" else 14
        if len(inner_accounts) != expected_count:
            raise PumpFunCpiDecodeError(f"Pump.fun {name} requires exactly {expected_count} accounts")
        base_mint = order.output_mint if direction == "buy" else order.input_mint
        quote_mint = WRAPPED_SOL_MINT
        if (direction == "buy" and order.input_mint != WRAPPED_SOL_MINT) or (direction == "sell" and order.output_mint != WRAPPED_SOL_MINT):
            raise PumpFunCpiDecodeError("Pump.fun v1 bonding-curve routes require wrapped SOL as the quote mint")
        _account(inner_accounts, validation, 0, global_pda, writable=False, role="global config")
        fee_recipient = _fee_recipient(inner_accounts, validation, 1, global_config.fee_recipient_allowlist, role="fee recipient")
        _account(inner_accounts, validation, 2, base_mint, writable=False, role="mint")
        _account(inner_accounts, validation, 3, amm_key, writable=True, role="bonding curve")
        _account(inner_accounts, validation, 4, derive_associated_token_address(amm_key, base_mint), writable=True, role="associated bonding-curve token account")
        _account(inner_accounts, validation, 5, derive_associated_token_address(order.taker, base_mint), writable=True, role="user base token account")
        _account(inner_accounts, validation, 6, order.taker, signer=True, writable=True, role="user")
        _account(inner_accounts, validation, 7, SYSTEM_PROGRAM_ID, writable=False, role="system program")
        _account(inner_accounts, validation, 8 if direction == "buy" else 9, CLASSIC_TOKEN_PROGRAM_ID, writable=False, role="token program")
        _account(inner_accounts, validation, 9 if direction == "buy" else 8, inner_accounts[9 if direction == "buy" else 8], writable=True, role="creator vault")
        _account(inner_accounts, validation, 10, event_pda, writable=False, role="event authority")
        _account(inner_accounts, validation, 11, PUMP_PROGRAM_ID, writable=False, role="Pump.fun program")
        if direction == "buy":
            _account(inner_accounts, validation, 12, global_volume_pda, writable=False, role="global volume accumulator")
            _account(inner_accounts, validation, 13, user_volume_pda, writable=True, role="user volume accumulator")
            _account(inner_accounts, validation, 14, fee_config_pda, writable=False, role="fee config")
            _account(inner_accounts, validation, 15, PUMP_FEE_PROGRAM_ID, writable=False, role="fee program")
        else:
            _account(inner_accounts, validation, 12, fee_config_pda, writable=False, role="fee config")
            _account(inner_accounts, validation, 13, PUMP_FEE_PROGRAM_ID, writable=False, role="fee program")
    elif version == "v2":
        expected_count = 27 if direction == "buy" else 26
        if len(inner_accounts) != expected_count:
            raise PumpFunCpiDecodeError(f"Pump.fun {name} requires exactly {expected_count} accounts")
        base_mint = order.output_mint if direction == "buy" else order.input_mint
        quote_mint = order.input_mint if direction == "buy" else order.output_mint
        _account(inner_accounts, validation, 0, global_pda, writable=False, role="global config")
        _account(inner_accounts, validation, 1, base_mint, writable=False, role="base mint")
        _account(inner_accounts, validation, 2, quote_mint, writable=False, role="quote mint")
        _account(inner_accounts, validation, 3, CLASSIC_TOKEN_PROGRAM_ID, writable=False, role="base token program")
        _account(inner_accounts, validation, 4, CLASSIC_TOKEN_PROGRAM_ID, writable=False, role="quote token program")
        _account(inner_accounts, validation, 5, ASSOCIATED_TOKEN_PROGRAM_ID, writable=False, role="associated token program")
        fee_recipient = _fee_recipient(inner_accounts, validation, 6, global_config.fee_recipient_allowlist, role="fee recipient")
        buyback_recipient = _fee_recipient(inner_accounts, validation, 8, global_config.buyback_fee_recipient_allowlist, role="buyback fee recipient")
        _account(inner_accounts, validation, 7, derive_associated_token_address(fee_recipient, quote_mint), writable=True, role="associated quote fee recipient")
        _account(inner_accounts, validation, 9, derive_associated_token_address(buyback_recipient, quote_mint), writable=True, role="associated quote buyback fee recipient")
        _account(inner_accounts, validation, 10, amm_key, writable=True, role="bonding curve")
        _account(inner_accounts, validation, 11, derive_associated_token_address(amm_key, base_mint), writable=True, role="associated base bonding-curve account")
        _account(inner_accounts, validation, 12, derive_associated_token_address(amm_key, quote_mint), writable=True, role="associated quote bonding-curve account")
        _account(inner_accounts, validation, 13, order.taker, signer=True, writable=True, role="user")
        _account(inner_accounts, validation, 14, derive_associated_token_address(order.taker, base_mint), writable=True, role="user base token account")
        _account(inner_accounts, validation, 15, derive_associated_token_address(order.taker, quote_mint), writable=True, role="user quote token account")
        _account(inner_accounts, validation, 16, inner_accounts[16], writable=True, role="creator vault")
        _account(inner_accounts, validation, 17, derive_associated_token_address(inner_accounts[16], quote_mint), writable=True, role="associated creator vault")
        sharing_config = _pda((b"sharing-config", bytes(Pubkey.from_string(base_mint))), PUMP_FEE_PROGRAM_ID)
        _account(inner_accounts, validation, 18, sharing_config, writable=False, role="sharing config")
        volume_position = 20 if direction == "buy" else 19
        associated_volume_position = 21 if direction == "buy" else 20
        if direction == "buy":
            _account(inner_accounts, validation, 19, global_volume_pda, writable=False, role="global volume accumulator")
        _account(inner_accounts, validation, volume_position, user_volume_pda, writable=True, role="user volume accumulator")
        _account(inner_accounts, validation, associated_volume_position, derive_associated_token_address(user_volume_pda, quote_mint), writable=True, role="associated user volume accumulator")
        fee_config_position = 22 if direction == "buy" else 21
        fee_program_position = 23 if direction == "buy" else 22
        system_position = 24 if direction == "buy" else 23
        event_position = 25 if direction == "buy" else 24
        program_position = 26 if direction == "buy" else 25
        _account(inner_accounts, validation, fee_config_position, fee_config_pda, writable=False, role="fee config")
        _account(inner_accounts, validation, fee_program_position, PUMP_FEE_PROGRAM_ID, writable=False, role="fee program")
        _account(inner_accounts, validation, system_position, SYSTEM_PROGRAM_ID, writable=False, role="system program")
        _account(inner_accounts, validation, event_position, event_pda, writable=False, role="event authority")
        _account(inner_accounts, validation, program_position, PUMP_PROGRAM_ID, writable=False, role="Pump.fun program")
    elif version == "v3":
        if len(inner_accounts) != 17:
            raise PumpFunCpiDecodeError(f"Pump.fun {name} requires exactly 17 accounts")
        base_mint = order.output_mint if direction == "buy" else order.input_mint
        quote_mint = order.input_mint if direction == "buy" else order.output_mint
        _account(inner_accounts, validation, 0, global_pda, writable=False, role="global config")
        _account(inner_accounts, validation, 1, base_mint, writable=False, role="base mint")
        _account(inner_accounts, validation, 2, quote_mint, writable=False, role="quote mint")
        _account(inner_accounts, validation, 3, CLASSIC_TOKEN_PROGRAM_ID, writable=False, role="base token program")
        _account(inner_accounts, validation, 4, CLASSIC_TOKEN_PROGRAM_ID, writable=False, role="quote token program")
        _account(inner_accounts, validation, 5, amm_key, writable=True, role="bonding curve")
        _account(inner_accounts, validation, 6, derive_associated_token_address(amm_key, base_mint), writable=True, role="associated base bonding-curve account")
        _account(inner_accounts, validation, 7, derive_associated_token_address(amm_key, quote_mint), writable=True, role="associated quote bonding-curve account")
        _account(inner_accounts, validation, 8, order.taker, signer=True, writable=True, role="user")
        _account(inner_accounts, validation, 9, derive_associated_token_address(order.taker, base_mint), writable=True, role="user base token account")
        _account(inner_accounts, validation, 10, derive_associated_token_address(order.taker, quote_mint), writable=True, role="user quote token account")
        _account(inner_accounts, validation, 11, user_volume_pda, writable=True, role="user volume accumulator")
        _account(inner_accounts, validation, 12, fee_config_pda, writable=False, role="fee config")
        _fee_recipient(inner_accounts, validation, 13, global_config.buyback_fee_recipient_allowlist, role="buyback fee recipient")
        _account(inner_accounts, validation, 14, SYSTEM_PROGRAM_ID, writable=False, role="system program")
        _account(inner_accounts, validation, 15, event_pda, writable=False, role="event authority")
        _account(inner_accounts, validation, 16, PUMP_PROGRAM_ID, writable=False, role="Pump.fun program")
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
    global_address = _pda((b"global",), PUMP_PROGRAM_ID)
    global_snapshots = [item for item in simulation.account_snapshots if item.address == global_address]
    if len(global_snapshots) != 1:
        raise PumpFunCpiDecodeError("exactly one same-slot Pump.fun Global account snapshot is required")
    global_config = decode_pumpfun_global_config(global_snapshots[0], expected_context_slot=simulation.context_slot)
    if jupiter_instruction.top_level_instruction_index != route_gate.parent_instruction_index:
        raise PumpFunCpiDecodeError("Jupiter route parent instruction differs from route-gate evidence")
    if len(jupiter_instruction.route_plan) != len(order.route_plan):
        raise PumpFunCpiDecodeError("decoded Jupiter route plan differs in length from the API order")
    slippage_bps = order.slippage_bps if order.slippage_bps is not None else jupiter_instruction.slippage_bps
    if slippage_bps > policy.max_slippage_bps:
        raise PumpFunCpiDecodeError("Jupiter slippage exceeds the Pump.fun CPI policy")

    decoded_results = []
    pump_steps = [
        (route, decoded)
        for route, decoded in zip(order.route_plan, jupiter_instruction.route_plan)
        if route.label == "Pump.Fun"
    ]
    if not pump_steps:
        raise PumpFunCpiDecodeError("Pump.fun CPI decoder is not applicable to this DEX route")
    pump_instructions = [item for item in simulation.inner_instructions if item.program_id == PUMP_PROGRAM_ID]
    if len(pump_instructions) != len(pump_steps):
        raise PumpFunCpiDecodeError("simulated Pump.fun CPI count differs from the Jupiter Pump.fun route steps")
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
            global_config=global_config,
        )
        if policy.require_partial_fill_disabled and partial_fill:
            raise PumpFunCpiDecodeError("Pump.fun partial fills are disabled by local policy")
        if amount_mode == "buy_exact_out":
            if base_amount != order.output_amount_raw:
                raise PumpFunCpiDecodeError("Pump.fun exact-output base amount differs from Jupiter quoted output")
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
            global_config_sha256=global_config.data_sha256,
            global_config_slot=global_config.context_slot,
        ))

    return PumpFunCpiGateResult(
        passed=True,
        message_sha256=digest,
        decoded_instructions=tuple(decoded_results),
        global_config_sha256=global_config.data_sha256,
        global_config_slot=global_config.context_slot,
    )
