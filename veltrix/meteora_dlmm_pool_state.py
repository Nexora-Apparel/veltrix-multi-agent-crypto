"""Meteora DLMM pool/reserve account-state verification.

The fixed Borsh offsets are pinned to MeteoraAg/dlmm-sdk idls/dlmm.json at
commit 1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1. This module consumes
same-context pre/post snapshots captured by the read-only RPC adapter.
"""
from __future__ import annotations

from dataclasses import dataclass

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    JupiterSwapEffectResult,
    derive_associated_token_address,
)
from veltrix.meteora_dlmm_cpi import (
    METEORA_DLMM_IDL_COMMIT,
    METEORA_DLMM_PROGRAM_ID,
    MeteoraDlmmCpiResult,
)
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult, SimulatedAccountState
from veltrix.transaction_validator import TransactionValidationResult


METEORA_LB_PAIR_DISCRIMINATOR = bytes((33, 11, 49, 98, 181, 101, 177, 13))
METEORA_ORACLE_DISCRIMINATOR = bytes((139, 194, 131, 179, 140, 179, 229, 244))
METEORA_BITMAP_EXTENSION_DISCRIMINATOR = bytes((80, 111, 124, 113, 55, 237, 18, 5))
LB_PAIR_ACCOUNT_SIZE = 904
SPL_TOKEN_ACCOUNT_SIZE = 165

# Absolute offsets include the 8-byte Anchor account discriminator.
LB_PAIR_OFFSETS = {
    "token_x_mint": 88,
    "token_y_mint": 120,
    "reserve_x": 152,
    "reserve_y": 184,
    "oracle": 552,
}


class MeteoraDlmmPoolStateError(ValueError):
    """Raised when simulated Meteora pool/reserve state is missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class MeteoraLbPairState:
    address: str
    token_x_mint: str
    token_y_mint: str
    reserve_x: str
    reserve_y: str
    oracle: str


@dataclass(frozen=True, slots=True)
class SplTokenAccountState:
    address: str
    mint: str
    authority: str
    pre_amount: int
    post_amount: int


@dataclass(frozen=True, slots=True)
class MeteoraDlmmPoolStateResult:
    passed: bool
    pool_state: MeteoraLbPairState
    reserve_x_state: SplTokenAccountState
    reserve_y_state: SplTokenAccountState
    oracle_address: str
    idl_commit: str = METEORA_DLMM_IDL_COMMIT


def _read_pubkey(data: bytes, offset: int, field: str) -> str:
    raw = data[offset:offset + 32]
    if len(raw) != 32:
        raise MeteoraDlmmPoolStateError(f"Meteora LB-pair account is truncated at {field}")
    try:
        return str(Pubkey.from_bytes(raw))
    except Exception as exc:
        raise MeteoraDlmmPoolStateError(f"Meteora LB-pair field {field} is not a public key") from exc


def parse_meteora_lb_pair_state(snapshot: SimulatedAccountState) -> MeteoraLbPairState:
    if not isinstance(snapshot, SimulatedAccountState):
        raise MeteoraDlmmPoolStateError("same-context LB-pair account snapshot is required")
    if snapshot.pre_owner_program_id != METEORA_DLMM_PROGRAM_ID or snapshot.pre_executable:
        raise MeteoraDlmmPoolStateError("LB-pair account is not owned by the pinned Meteora program")
    if len(snapshot.pre_data) != LB_PAIR_ACCOUNT_SIZE or len(snapshot.post_data) != LB_PAIR_ACCOUNT_SIZE:
        raise MeteoraDlmmPoolStateError("Meteora LB-pair account size differs from the pinned 904-byte IDL layout")
    if (
        snapshot.pre_data[:8] != METEORA_LB_PAIR_DISCRIMINATOR
        or snapshot.post_data[:8] != METEORA_LB_PAIR_DISCRIMINATOR
    ):
        raise MeteoraDlmmPoolStateError("account data is not the pinned Meteora LbPair account type")

    def fields(data: bytes) -> tuple[str, str, str, str, str]:
        return tuple(
            _read_pubkey(data, LB_PAIR_OFFSETS[name], name)
            for name in ("token_x_mint", "token_y_mint", "reserve_x", "reserve_y", "oracle")
        )

    before = fields(snapshot.pre_data)
    after = fields(snapshot.post_data)
    if before != after:
        raise MeteoraDlmmPoolStateError("Meteora LB-pair mint, reserve, or oracle links changed during simulation")
    token_x, token_y, reserve_x, reserve_y, oracle = before
    if token_x == token_y or reserve_x == reserve_y:
        raise MeteoraDlmmPoolStateError("Meteora LB-pair has duplicate mint or reserve addresses")
    return MeteoraLbPairState(
        address=snapshot.address,
        token_x_mint=token_x,
        token_y_mint=token_y,
        reserve_x=reserve_x,
        reserve_y=reserve_y,
        oracle=oracle,
    )


def parse_classic_spl_token_account(snapshot: SimulatedAccountState) -> SplTokenAccountState:
    if not isinstance(snapshot, SimulatedAccountState):
        raise MeteoraDlmmPoolStateError("same-context SPL token account snapshot is required")
    if snapshot.pre_owner_program_id != CLASSIC_TOKEN_PROGRAM_ID or snapshot.pre_executable:
        raise MeteoraDlmmPoolStateError("Meteora reserve is not owned by the classic SPL Token program")
    if len(snapshot.pre_data) != SPL_TOKEN_ACCOUNT_SIZE or len(snapshot.post_data) != SPL_TOKEN_ACCOUNT_SIZE:
        raise MeteoraDlmmPoolStateError("Meteora reserve does not have the classic 165-byte SPL token-account layout")
    pre_mint = _read_pubkey(snapshot.pre_data, 0, "reserve mint")
    post_mint = _read_pubkey(snapshot.post_data, 0, "post-state reserve mint")
    pre_authority = _read_pubkey(snapshot.pre_data, 32, "reserve authority")
    post_authority = _read_pubkey(snapshot.post_data, 32, "post-state reserve authority")
    if pre_mint != post_mint or pre_authority != post_authority:
        raise MeteoraDlmmPoolStateError("Meteora reserve mint or token authority changed during simulation")
    if snapshot.pre_data[108] != 1 or snapshot.post_data[108] != 1:
        raise MeteoraDlmmPoolStateError("Meteora reserve token account is not initialized and transferable")
    pre_amount = int.from_bytes(snapshot.pre_data[64:72], "little")
    post_amount = int.from_bytes(snapshot.post_data[64:72], "little")
    return SplTokenAccountState(
        address=snapshot.address,
        mint=pre_mint,
        authority=pre_authority,
        pre_amount=pre_amount,
        post_amount=post_amount,
    )


def _require_program_owned_discriminator(snapshot: SimulatedAccountState, discriminator: bytes, field: str) -> None:
    if snapshot.pre_owner_program_id != METEORA_DLMM_PROGRAM_ID or snapshot.pre_executable:
        raise MeteoraDlmmPoolStateError(f"Meteora {field} account is not owned by the pinned program")
    if len(snapshot.pre_data) < 8 or len(snapshot.post_data) < 8:
        raise MeteoraDlmmPoolStateError(f"Meteora {field} account data is truncated")
    if snapshot.pre_data[:8] != discriminator or snapshot.post_data[:8] != discriminator:
        raise MeteoraDlmmPoolStateError(f"account data is not the pinned Meteora {field} account type")


def evaluate_meteora_dlmm_pool_state(
    order: JupiterOrderTransaction,
    *,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    cpi_result: MeteoraDlmmCpiResult,
    swap_effects: JupiterSwapEffectResult,
) -> MeteoraDlmmPoolStateResult:
    """Verify pool-to-reserve links and classic token account state for one-hop Meteora swaps."""
    if not isinstance(order, JupiterOrderTransaction):
        raise MeteoraDlmmPoolStateError("Jupiter order is required")
    if not isinstance(simulation, SimulationEvidence):
        raise MeteoraDlmmPoolStateError("simulation account-state evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise MeteoraDlmmPoolStateError("successful SimulationGate approval is required")
    if not isinstance(cpi_result, MeteoraDlmmCpiResult) or cpi_result.passed is not True:
        raise MeteoraDlmmPoolStateError("successful Meteora CPI semantic approval is required")
    if not isinstance(swap_effects, JupiterSwapEffectResult) or swap_effects.passed is not True:
        raise MeteoraDlmmPoolStateError("successful quote-to-balance effect approval is required")
    if len(order.route_plan) != 1 or len(cpi_result.decoded_swaps) != 1:
        raise MeteoraDlmmPoolStateError("pool-state verification currently supports exactly one Meteora DLMM route hop")
    digest = __import__("hashlib").sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        simulation.message_sha256, simulation_gate.message_sha256,
        cpi_result.message_sha256, swap_effects.message_sha256,
    )):
        raise MeteoraDlmmPoolStateError("order, simulation, CPI and swap-effect digests differ")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise MeteoraDlmmPoolStateError("simulation account snapshots and SimulationGate refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot or swap_effects.simulation_context_slot != simulation.context_slot:
        raise MeteoraDlmmPoolStateError("pool account snapshots and token effects are not bound to the same simulation slot")

    decoded = cpi_result.decoded_swaps[0]
    step = order.route_plan[0]
    if decoded.lb_pair != step.amm_key:
        raise MeteoraDlmmPoolStateError("decoded Meteora pool differs from the Jupiter route plan")
    snapshots = {item.address: item for item in simulation.account_states}
    if len(snapshots) != len(simulation.account_states):
        raise MeteoraDlmmPoolStateError("simulation contains duplicate account-state snapshots")
    required_addresses = {decoded.lb_pair, decoded.reserve_x, decoded.reserve_y, decoded.oracle}
    if not required_addresses.issubset(snapshots):
        raise MeteoraDlmmPoolStateError("pool, reserve, or oracle account state is missing from simulation evidence")

    pool = parse_meteora_lb_pair_state(snapshots[decoded.lb_pair])
    if (
        pool.token_x_mint != decoded.token_x_mint
        or pool.token_y_mint != decoded.token_y_mint
        or pool.reserve_x != decoded.reserve_x
        or pool.reserve_y != decoded.reserve_y
        or pool.oracle != decoded.oracle
    ):
        raise MeteoraDlmmPoolStateError("Meteora CPI pool/reserve/oracle accounts differ from the on-chain LB-pair state")
    if {pool.token_x_mint, pool.token_y_mint} != {step.input_mint, step.output_mint}:
        raise MeteoraDlmmPoolStateError("on-chain LB-pair token mints differ from the Jupiter quote route hop")

    reserve_x = parse_classic_spl_token_account(snapshots[pool.reserve_x])
    reserve_y = parse_classic_spl_token_account(snapshots[pool.reserve_y])
    if reserve_x.mint != pool.token_x_mint or reserve_y.mint != pool.token_y_mint:
        raise MeteoraDlmmPoolStateError("Meteora reserve token-account mints differ from the LB-pair token mints")

    oracle_snapshot = snapshots[pool.oracle]
    _require_program_owned_discriminator(oracle_snapshot, METEORA_ORACLE_DISCRIMINATOR, "oracle")

    input_ata = derive_associated_token_address(order.taker, order.input_mint)
    output_ata = derive_associated_token_address(order.taker, order.output_mint)
    if decoded.user_token_in != input_ata or decoded.user_token_out != output_ata:
        raise MeteoraDlmmPoolStateError("Meteora CPI user token accounts are not the taker's exact input/output associated token accounts")
    user_balances = {item.token_account: item for item in simulation.token_balances}
    if input_ata not in user_balances or output_ata not in user_balances:
        raise MeteoraDlmmPoolStateError("taker input/output token-account balance evidence is missing")
    if (
        user_balances[input_ata].mint != order.input_mint
        or user_balances[input_ata].owner != order.taker
        or user_balances[output_ata].mint != order.output_mint
        or user_balances[output_ata].owner != order.taker
    ):
        raise MeteoraDlmmPoolStateError("taker token-account state differs from the Jupiter order")

    return MeteoraDlmmPoolStateResult(
        passed=True,
        pool_state=pool,
        reserve_x_state=reserve_x,
        reserve_y_state=reserve_y,
        oracle_address=pool.oracle,
    )
