"""Bind a Jupiter v0 order to exact user token-account balance effects.

Only pre-existing classic SPL Associated Token Accounts are supported. The
expected output is exact, not merely a lower bound, to reject quote/simulation
drift. Native SOL wrapping, temporary WSOL accounts and Token-2022 are excluded.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.simulation_gate import (
    ExpectedTokenDelta,
    SimulationEvidence,
    SimulationGateResult,
    SimulatedTokenBalance,
)
from veltrix.transaction_validator import TransactionValidationResult


CLASSIC_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


class JupiterSwapEffectError(ValueError):
    """Raised when simulated user token effects do not match the Jupiter order."""


def derive_associated_token_address(owner: str, mint: str) -> str:
    try:
        owner_key = Pubkey.from_string(owner)
        mint_key = Pubkey.from_string(mint)
        token_program = Pubkey.from_string(CLASSIC_TOKEN_PROGRAM_ID)
        associated_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
        address, _ = Pubkey.find_program_address(
            [bytes(owner_key), bytes(token_program), bytes(mint_key)],
            associated_program,
        )
        return str(address)
    except Exception as exc:
        raise JupiterSwapEffectError("could not derive classic SPL associated token account") from exc


def build_expected_jupiter_token_deltas(order: JupiterOrderTransaction) -> tuple[ExpectedTokenDelta, ...]:
    if not isinstance(order, JupiterOrderTransaction):
        raise JupiterSwapEffectError("Jupiter v0 order is required")
    if order.taker != order.fee_payer:
        raise JupiterSwapEffectError("taker must be the order fee payer")
    if order.input_mint == order.output_mint:
        raise JupiterSwapEffectError("input and output mints must differ")
    if (
        isinstance(order.input_amount_raw, bool)
        or not isinstance(order.input_amount_raw, int)
        or order.input_amount_raw <= 0
        or isinstance(order.output_amount_raw, bool)
        or not isinstance(order.output_amount_raw, int)
        or order.output_amount_raw <= 0
        or isinstance(order.minimum_output_amount_raw, bool)
        or not isinstance(order.minimum_output_amount_raw, int)
        or not 0 < order.minimum_output_amount_raw <= order.output_amount_raw
    ):
        raise JupiterSwapEffectError("Jupiter order raw amounts are invalid")
    input_ata = derive_associated_token_address(order.taker, order.input_mint)
    output_ata = derive_associated_token_address(order.taker, order.output_mint)
    if input_ata == output_ata:
        raise JupiterSwapEffectError("derived input and output token accounts unexpectedly match")
    return (
        ExpectedTokenDelta(
            token_account=input_ata,
            mint=order.input_mint,
            owner=order.taker,
            delta_raw=-order.input_amount_raw,
        ),
        ExpectedTokenDelta(
            token_account=output_ata,
            mint=order.output_mint,
            owner=order.taker,
            delta_raw=order.output_amount_raw,
        ),
    )


@dataclass(frozen=True, slots=True)
class JupiterSwapEffectPolicy:
    max_order_age_ms: int = 10_000
    max_future_skew_ms: int = 1_000

    def __post_init__(self) -> None:
        for field in ("max_order_age_ms", "max_future_skew_ms"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JupiterSwapEffectError(f"{field} must be a non-negative integer")
        if self.max_order_age_ms == 0:
            raise JupiterSwapEffectError("max_order_age_ms must be positive")


@dataclass(frozen=True, slots=True)
class JupiterSwapEffectResult:
    passed: bool
    message_sha256: str
    input_token_account: str
    output_token_account: str
    input_delta_raw: int
    output_delta_raw: int
    minimum_output_amount_raw: int
    exact_output_amount_raw: int
    simulation_context_slot: int


def evaluate_jupiter_swap_effects(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    policy: JupiterSwapEffectPolicy,
    now_epoch_ms: int,
) -> JupiterSwapEffectResult:
    """Require exact input spend and exact quoted output in the taker's classic SPL ATAs."""
    if not isinstance(order, JupiterOrderTransaction):
        raise JupiterSwapEffectError("Jupiter v0 order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise JupiterSwapEffectError("structural transaction validation is required")
    if not isinstance(simulation, SimulationEvidence):
        raise JupiterSwapEffectError("simulation balance evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise JupiterSwapEffectError("successful SimulationGate approval is required")
    if not isinstance(policy, JupiterSwapEffectPolicy):
        raise JupiterSwapEffectError("Jupiter swap effect policy is required")
    if isinstance(now_epoch_ms, bool) or not isinstance(now_epoch_ms, int) or now_epoch_ms <= 0:
        raise JupiterSwapEffectError("now_epoch_ms must be a positive integer")

    if not isinstance(order.message_bytes, bytes) or not order.message_bytes:
        raise JupiterSwapEffectError("Jupiter order message bytes are missing")
    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if digest != validation.message_sha256 or digest != simulation.message_sha256 or digest != simulation_gate.message_sha256:
        raise JupiterSwapEffectError("order, validation and simulation message digests differ")
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise JupiterSwapEffectError("Jupiter taker is not the validated transaction fee payer")
    if simulation.recent_blockhash != order.recent_blockhash:
        raise JupiterSwapEffectError("simulation blockhash differs from Jupiter order")
    if simulation.last_valid_block_height != order.last_valid_block_height:
        raise JupiterSwapEffectError("simulation last-valid block height differs from Jupiter order")
    if (
        simulation.response_id != simulation_gate.response_id
        or simulation.provider_id != simulation_gate.provider_id
        or simulation.context_slot != simulation_gate.context_slot
        or simulation.units_consumed != simulation_gate.units_consumed
        or simulation.fee_lamports != simulation_gate.fee_lamports
    ):
        raise JupiterSwapEffectError("simulation evidence and gate result refer to different RPC responses")
    if (
        isinstance(order.observed_at_epoch_ms, bool)
        or not isinstance(order.observed_at_epoch_ms, int)
        or order.observed_at_epoch_ms <= 0
        or order.observed_at_epoch_ms > now_epoch_ms + policy.max_future_skew_ms
    ):
        raise JupiterSwapEffectError("Jupiter order observation timestamp is invalid or future-dated")
    if now_epoch_ms - order.observed_at_epoch_ms > policy.max_order_age_ms:
        raise JupiterSwapEffectError("Jupiter order quote is stale")
    if simulation.observed_at_epoch_ms < order.observed_at_epoch_ms:
        raise JupiterSwapEffectError("simulation predates the Jupiter order")
    if simulation_gate.verified_token_account_count != 2 or len(simulation.token_balances) != 2:
        raise JupiterSwapEffectError("exactly two user token-account balance records are required")

    expected = build_expected_jupiter_token_deltas(order)
    expected_by_address = {item.token_account: item for item in expected}
    actual_by_address = {item.token_account: item for item in simulation.token_balances}
    if len(actual_by_address) != len(simulation.token_balances):
        raise JupiterSwapEffectError("simulation contains duplicate user token-account records")
    if set(expected_by_address) != set(actual_by_address):
        raise JupiterSwapEffectError("simulation did not capture exactly the taker's input and output associated token accounts")

    for address, expectation in expected_by_address.items():
        actual = actual_by_address[address]
        if actual.token_program_id != CLASSIC_TOKEN_PROGRAM_ID:
            raise JupiterSwapEffectError("only classic SPL Token account effects are supported")
        if (actual.mint, actual.owner) != (expectation.mint, expectation.owner):
            raise JupiterSwapEffectError("simulated associated token account mint or owner differs from the taker order")
        delta = actual.post_amount_raw - actual.pre_amount_raw
        if delta != expectation.delta_raw:
            raise JupiterSwapEffectError("simulated token-account delta differs from the exact Jupiter order amount")

    input_balance = actual_by_address[expected[0].token_account]
    output_balance = actual_by_address[expected[1].token_account]
    input_delta = input_balance.post_amount_raw - input_balance.pre_amount_raw
    output_delta = output_balance.post_amount_raw - output_balance.pre_amount_raw
    if input_balance.pre_amount_raw < order.input_amount_raw:
        raise JupiterSwapEffectError("taker input token account does not contain the required raw amount")
    if output_delta < order.minimum_output_amount_raw:
        raise JupiterSwapEffectError("simulated output is below Jupiter's minimum output threshold")

    return JupiterSwapEffectResult(
        passed=True,
        message_sha256=digest,
        input_token_account=expected[0].token_account,
        output_token_account=expected[1].token_account,
        input_delta_raw=input_delta,
        output_delta_raw=output_delta,
        minimum_output_amount_raw=order.minimum_output_amount_raw,
        exact_output_amount_raw=order.output_amount_raw,
        simulation_context_slot=simulation.context_slot,
    )
