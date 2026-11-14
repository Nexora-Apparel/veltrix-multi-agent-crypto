"""Strict structural decoder for Jupiter V6 route instruction payloads.

This module decodes the Jupiter-owned instruction data only. It does NOT
interpret nested DEX instructions, prove account metas, derive token effects,
or authorize execution. Callers must keep execution blocked until those
independent semantic adapters and account-state proofs exist.
"""
from __future__ import annotations

from dataclasses import dataclass


JUPITER_V6_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

# Anchor global:<instruction> discriminators for the V6 route families.
# The decoder intentionally accepts only these four families.
_ROUTE_DISCRIMINATORS = {
    bytes.fromhex("e517cb977ae3ad2a"): ("route", False, False),
    bytes.fromhex("c1209b3341d69c81"): ("shared_accounts_route", True, False),
    bytes.fromhex("d033ef977b2bed5c"): ("exact_out_route", False, True),
    bytes.fromhex("b0d169a89a7d453e"): ("shared_accounts_exact_out_route", True, True),
}

# Variant ordinals are pinned to the Swap enum in
# jup-ag/instruction-parser/src/idl/jupiter.ts (blob
# 4273e9f0f34a3d4d29e035fe37e88d6c80b634b5). Unlisted variants have no
# serialized fields in that IDL. This describes wire shape, not DEX support.
_SWAP_VARIANT_FIELDS = {
    "Crema": ("bool",),
    "Serum": ("side",),
    "Aldrin": ("side",),
    "AldrinV2": ("side",),
    "Whirlpool": ("bool",),
    "Invariant": ("bool",),
    "DeltaFi": ("bool",),
    "MarcoPolo": ("bool",),
    "Dradex": ("side",),
    "Openbook": ("side",),
    "Phoenix": ("side",),
    "Symmetry": ("u64", "u64"),
    "StakeDexSwapViaStake": ("u32",),
    "OpenBookV2": ("side",),
    "StakeDexPrefundWithdrawStakeAndDepositStake": ("u32",),
    "Clone": ("u8", "bool", "bool"),
    "SanctumS": ("u8", "u8", "u32", "u32"),
    "SanctumSAddLiquidity": ("u8", "u32"),
    "SanctumSRemoveLiquidity": ("u8", "u32"),
    "WhirlpoolSwapV2": ("bool", "remaining_accounts_info"),
    "Obric": ("bool",),
    "FoxClaimPartial": ("bool",),
    "SolFi": ("bool",),
}
_SWAP_VARIANT_NAMES = (
    "Saber", "SaberAddDecimalsDeposit", "SaberAddDecimalsWithdraw",
    "TokenSwap", "Sencha", "Step", "Cropper", "Raydium", "Crema",
    "Lifinity", "Mercurial", "Cykura", "Serum", "MarinadeDeposit",
    "MarinadeUnstake", "Aldrin", "AldrinV2", "Whirlpool", "Invariant",
    "Meteora", "GooseFX", "DeltaFi", "Balansol", "MarcoPolo", "Dradex",
    "LifinityV2", "RaydiumClmm", "Openbook", "Phoenix", "Symmetry",
    "TokenSwapV2", "HeliumTreasuryManagementRedeemV0",
    "StakeDexStakeWrappedSol", "StakeDexSwapViaStake", "GooseFXV2",
    "Perps", "PerpsAddLiquidity", "PerpsRemoveLiquidity", "MeteoraDlmm",
    "OpenBookV2", "RaydiumClmmV2",
    "StakeDexPrefundWithdrawStakeAndDepositStake", "Clone", "SanctumS",
    "SanctumSAddLiquidity", "SanctumSRemoveLiquidity", "RaydiumCP",
    "WhirlpoolSwapV2", "OneIntro", "PumpdotfunWrappedBuy",
    "PumpdotfunWrappedSell", "PerpsV2", "PerpsV2AddLiquidity",
    "PerpsV2RemoveLiquidity", "MoonshotWrappedBuy", "MoonshotWrappedSell",
    "StabbleStableSwap", "StabbleWeightedSwap", "Obric",
    "FoxBuyFromEstimatedCost", "FoxClaimPartial", "SolFi",
)


class JupiterInstructionDecodeError(ValueError):
    """Raised when a Jupiter V6 instruction payload is malformed/unsupported."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise JupiterInstructionDecodeError("truncated_instruction_data")
        value = self.data[self.offset:self.offset + size]
        self.offset += size
        return value

    def uint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "little")

    def boolean(self) -> bool:
        value = self.uint(1)
        if value not in (0, 1):
            raise JupiterInstructionDecodeError("noncanonical_boolean")
        return bool(value)

    def vector_length(self, maximum: int) -> int:
        value = self.uint(4)
        if value < 1 or value > maximum:
            raise JupiterInstructionDecodeError("route_step_count_out_of_range")
        return value

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise JupiterInstructionDecodeError("trailing_instruction_data")


@dataclass(frozen=True, slots=True)
class JupiterRouteStep:
    swap_variant: str
    swap_variant_index: int
    variant_args: tuple[int | bool, ...]
    percent: int
    input_index: int
    output_index: int


@dataclass(frozen=True, slots=True)
class JupiterV6RouteInstruction:
    instruction_name: str
    route_id: int | None
    exact_out: bool
    route_plan: tuple[JupiterRouteStep, ...]
    amount: int
    quoted_amount: int
    slippage_bps: int
    platform_fee_bps: int
    semantic_coverage: str = "jupiter_outer_instruction_only"
    execution_approved: bool = False


def _read_remaining_accounts_info(reader: _Reader) -> tuple[tuple[int, int], ...]:
    # RemainingAccountsInfo { slices: Vec<RemainingAccountsSlice> };
    count = reader.uint(4)
    if count > 16:
        raise JupiterInstructionDecodeError("remaining_account_slice_count_out_of_range")
    slices = []
    for _ in range(count):
        accounts_type = reader.uint(1)
        length = reader.uint(1)
        if accounts_type not in (0, 1) or length == 0:
            raise JupiterInstructionDecodeError("unsupported_remaining_account_slice")
        slices.append((accounts_type, length))
    return tuple(slices)


def _read_swap_variant(reader: _Reader) -> tuple[int, str, tuple[int | bool, ...]]:
    index = reader.uint(1)
    if index >= len(_SWAP_VARIANT_NAMES):
        raise JupiterInstructionDecodeError("unknown_jupiter_swap_variant")
    args: list[int | bool] = []
    for field in _SWAP_VARIANT_FIELDS.get(_SWAP_VARIANT_NAMES[index], ()):
        if field == "bool":
            args.append(reader.boolean())
        elif field == "u8":
            args.append(reader.uint(1))
        elif field == "u32":
            args.append(reader.uint(4))
        elif field == "u64":
            args.append(reader.uint(8))
        elif field == "side":
            side = reader.uint(1)
            if side not in (0, 1):
                raise JupiterInstructionDecodeError("unknown_swap_side")
            args.append(side)
        elif field == "remaining_accounts_info":
            # Preserve a stable count/length commitment; this field is not
            # interpreted as proof of the invoked transfer-hook behavior.
            slices = _read_remaining_accounts_info(reader)
            args.extend((len(slices), sum(length for _, length in slices)))
        else:
            raise JupiterInstructionDecodeError("unsupported_swap_variant_schema")
    return index, _SWAP_VARIANT_NAMES[index], tuple(args)


def decode_jupiter_v6_route_instruction(
    program_id: str, instruction_data: bytes, *, max_route_steps: int = 12
) -> JupiterV6RouteInstruction:
    """Decode one Jupiter V6 route-family payload; never authorizes execution."""
    if program_id != JUPITER_V6_PROGRAM_ID:
        raise JupiterInstructionDecodeError("unexpected_jupiter_program_id")
    if not isinstance(instruction_data, bytes) or len(instruction_data) < 8:
        raise JupiterInstructionDecodeError("instruction_data_too_short")
    if isinstance(max_route_steps, bool) or not isinstance(max_route_steps, int) or not 1 <= max_route_steps <= 64:
        raise JupiterInstructionDecodeError("invalid_route_step_limit")

    descriptor = _ROUTE_DISCRIMINATORS.get(instruction_data[:8])
    if descriptor is None:
        raise JupiterInstructionDecodeError("unsupported_jupiter_discriminator")
    name, has_route_id, exact_out = descriptor
    reader = _Reader(instruction_data[8:])
    route_id = reader.uint(1) if has_route_id else None
    count = reader.vector_length(max_route_steps)
    steps = []
    for _ in range(count):
        variant_index, variant_name, variant_args = _read_swap_variant(reader)
        percent = reader.uint(1)
        input_index = reader.uint(1)
        output_index = reader.uint(1)
        if percent > 100:
            raise JupiterInstructionDecodeError("route_percent_out_of_range")
        if input_index == output_index:
            raise JupiterInstructionDecodeError("route_step_input_output_alias")
        steps.append(JupiterRouteStep(
            swap_variant=variant_name,
            swap_variant_index=variant_index,
            variant_args=variant_args,
            percent=percent,
            input_index=input_index,
            output_index=output_index,
        ))

    amount = reader.uint(8)
    quoted_amount = reader.uint(8)
    slippage_bps = reader.uint(2)
    platform_fee_bps = reader.uint(1)
    if amount <= 0 or quoted_amount <= 0:
        raise JupiterInstructionDecodeError("route_amount_must_be_positive")
    if slippage_bps > 10_000 or platform_fee_bps > 100:
        raise JupiterInstructionDecodeError("route_fee_or_slippage_out_of_range")
    reader.finish()

    return JupiterV6RouteInstruction(
        instruction_name=name,
        route_id=route_id,
        exact_out=exact_out,
        route_plan=tuple(steps),
        amount=amount,
        quoted_amount=quoted_amount,
        slippage_bps=slippage_bps,
        platform_fee_bps=platform_fee_bps,
    )
