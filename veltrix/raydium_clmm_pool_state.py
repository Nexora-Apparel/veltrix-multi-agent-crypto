"""Verify Raydium CLMM pool/config/oracle/tick and vault state from simulation snapshots.

Pinned to raydium-io/raydium-clmm source commit
6d7a0f82d2f293c553c69aaf943377367c6c4cc6. Requires exact same-slot account
snapshots produced by Phase 09.17; it never performs an RPC request itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmCpiResult,
)
from veltrix.simulation_gate import SimulationAccountSnapshot, SimulationEvidence
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID


RAYDIUM_POOL_STATE_LEN = 1544
RAYDIUM_AMM_CONFIG_LEN = 117
RAYDIUM_OBSERVATION_STATE_LEN = 4483
RAYDIUM_TICK_ARRAY_STATE_LEN = 10240
SPL_TOKEN_ACCOUNT_BASE_LEN = 165
POOL_STATE_STATUS_SWAP_DISABLED = 1 << 4


class RaydiumPoolStateError(ValueError):
    """Raised when simulated Raydium pool or vault state does not match the CPI."""


def _discriminator(account_name: str) -> bytes:
    return hashlib.sha256(f"account:{account_name}".encode("ascii")).digest()[:8]


def _pubkey(raw: bytes, field: str) -> str:
    if len(raw) != 32:
        raise RaydiumPoolStateError(f"{field} is not a 32-byte public key")
    try:
        return str(Pubkey.from_bytes(raw))
    except Exception as exc:
        raise RaydiumPoolStateError(f"{field} is not a valid Solana public key") from exc


@dataclass(frozen=True, slots=True)
class RaydiumPoolState:
    bump: int
    amm_config: str
    token_mint_0: str
    token_mint_1: str
    token_vault_0: str
    token_vault_1: str
    observation_key: str
    mint_decimals_0: int
    mint_decimals_1: int
    tick_spacing: int
    status: int
    seed_index: bytes


def decode_raydium_pool_state(data: bytes) -> RaydiumPoolState:
    if not isinstance(data, bytes) or len(data) != RAYDIUM_POOL_STATE_LEN:
        raise RaydiumPoolStateError("Raydium PoolState data length is invalid")
    if data[:8] != _discriminator("PoolState"):
        raise RaydiumPoolStateError("Raydium PoolState account discriminator is invalid")
    return RaydiumPoolState(
        bump=data[8],
        amm_config=_pubkey(data[9:41], "PoolState.amm_config"),
        token_mint_0=_pubkey(data[73:105], "PoolState.token_mint_0"),
        token_mint_1=_pubkey(data[105:137], "PoolState.token_mint_1"),
        token_vault_0=_pubkey(data[137:169], "PoolState.token_vault_0"),
        token_vault_1=_pubkey(data[169:201], "PoolState.token_vault_1"),
        observation_key=_pubkey(data[201:233], "PoolState.observation_key"),
        mint_decimals_0=data[233],
        mint_decimals_1=data[234],
        tick_spacing=int.from_bytes(data[235:237], "little"),
        status=data[389],
        seed_index=data[391:393],
    )


@dataclass(frozen=True, slots=True)
class RaydiumTokenAccountState:
    mint: str
    authority: str
    amount_raw: int


def decode_classic_token_account(data: bytes) -> RaydiumTokenAccountState:
    if not isinstance(data, bytes) or len(data) < SPL_TOKEN_ACCOUNT_BASE_LEN:
        raise RaydiumPoolStateError("classic SPL token account data is truncated")
    return RaydiumTokenAccountState(
        mint=_pubkey(data[0:32], "token account mint"),
        authority=_pubkey(data[32:64], "token account authority"),
        amount_raw=int.from_bytes(data[64:72], "little"),
    )


@dataclass(frozen=True, slots=True)
class RaydiumPoolStatePolicy:
    program_id: str = RAYDIUM_CLMM_MAINNET_PROGRAM_ID
    require_exact_vault_balance_deltas: bool = True

    def __post_init__(self) -> None:
        try:
            if str(Pubkey.from_string(self.program_id)) != self.program_id:
                raise ValueError("non-canonical")
        except Exception as exc:
            raise RaydiumPoolStateError("Raydium program ID is invalid") from exc
        if self.program_id != RAYDIUM_CLMM_MAINNET_PROGRAM_ID:
            raise RaydiumPoolStateError("only the source-pinned Raydium CLMM mainnet program ID is supported")
        if not isinstance(self.require_exact_vault_balance_deltas, bool):
            raise RaydiumPoolStateError("require_exact_vault_balance_deltas must be a boolean")


@dataclass(frozen=True, slots=True)
class RaydiumPoolStateResult:
    passed: bool
    message_sha256: str
    pool_state: str
    amm_config: str
    token_mint_0: str
    token_mint_1: str
    token_vault_0: str
    token_vault_1: str
    observation_state: str
    tick_array: str
    tick_array_start_index: int
    tick_spacing: int
    pool_status: int
    input_vault_delta_raw: int
    output_vault_delta_raw: int
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT


def _required_snapshot(
    snapshots: dict[str, SimulationAccountSnapshot],
    address: str,
    *,
    owner: str,
    field: str,
) -> SimulationAccountSnapshot:
    snapshot = snapshots.get(address)
    if snapshot is None:
        raise RaydiumPoolStateError(f"simulation omitted the {field} account snapshot")
    if not snapshot.pre_exists or not snapshot.post_exists:
        raise RaydiumPoolStateError(f"{field} account must exist before and after simulation")
    if (
        snapshot.pre_owner != owner
        or snapshot.post_owner != owner
        or snapshot.pre_executable is not False
        or snapshot.post_executable is not False
    ):
        raise RaydiumPoolStateError(f"{field} account owner or executable status is invalid")
    if not snapshot.pre_data or not snapshot.post_data:
        raise RaydiumPoolStateError(f"{field} account data is missing")
    return snapshot


def _decode_config_tick_spacing(data: bytes) -> int:
    if len(data) != RAYDIUM_AMM_CONFIG_LEN or data[:8] != _discriminator("AmmConfig"):
        raise RaydiumPoolStateError("Raydium AmmConfig account discriminator or length is invalid")
    return int.from_bytes(data[51:53], "little")


def _decode_observation_pool_id(data: bytes) -> tuple[bool, str]:
    if len(data) != RAYDIUM_OBSERVATION_STATE_LEN or data[:8] != _discriminator("ObservationState"):
        raise RaydiumPoolStateError("Raydium ObservationState discriminator or length is invalid")
    initialized = data[8]
    if initialized not in (0, 1):
        raise RaydiumPoolStateError("Raydium ObservationState initialized flag is not canonical")
    return bool(initialized), _pubkey(data[19:51], "ObservationState.pool_id")


def _decode_tick_array_pool(data: bytes) -> tuple[str, int]:
    if len(data) != RAYDIUM_TICK_ARRAY_STATE_LEN or data[:8] != _discriminator("TickArrayState"):
        raise RaydiumPoolStateError("Raydium TickArrayState discriminator or length is invalid")
    return _pubkey(data[8:40], "TickArrayState.pool_id"), int.from_bytes(data[40:44], "little", signed=True)


def _validate_pool_pda(pool_address: str, state: RaydiumPoolState, program_id: str) -> None:
    seed_index = state.seed_index if state.seed_index != bytes(2) else b""
    expected, bump = Pubkey.find_program_address(
        [
            b"pool",
            bytes(Pubkey.from_string(state.amm_config)),
            bytes(Pubkey.from_string(state.token_mint_0)),
            bytes(Pubkey.from_string(state.token_mint_1)),
            seed_index,
        ],
        Pubkey.from_string(program_id),
    )
    if str(expected) != pool_address or bump != state.bump:
        raise RaydiumPoolStateError("Raydium PoolState address or bump does not match its canonical PDA seeds")


def _validate_tick_array_pda(tick_array: str, pool: str, start_tick_index: int, program_id: str) -> None:
    expected, _ = Pubkey.find_program_address(
        [
            b"tick_array",
            bytes(Pubkey.from_string(pool)),
            int(start_tick_index).to_bytes(4, "big", signed=True),
        ],
        Pubkey.from_string(program_id),
    )
    if str(expected) != tick_array:
        raise RaydiumPoolStateError("Raydium TickArrayState address does not match its pool/start-index PDA")


def evaluate_raydium_pool_state(
    order: JupiterOrderTransaction,
    *,
    cpi: RaydiumClmmCpiResult,
    simulation: SimulationEvidence,
    policy: RaydiumPoolStatePolicy,
) -> RaydiumPoolStateResult:
    """Verify the simulated Raydium pool, config, observation, tick and token vault state."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumPoolStateError("Jupiter order is required")
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumPoolStateError("successful Raydium CLMM CPI gate result is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumPoolStateError("simulation evidence is required")
    if not isinstance(policy, RaydiumPoolStatePolicy):
        raise RaydiumPoolStateError("Raydium pool state policy is required")
    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if digest != cpi.message_sha256 or digest != simulation.message_sha256:
        raise RaydiumPoolStateError("order, Raydium CPI and simulation message digests differ")
    if simulation.simulation_error is not None:
        raise RaydiumPoolStateError("simulation reported an execution error")
    if cpi.program_id != policy.program_id or cpi.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumPoolStateError("Raydium CPI result does not use the pinned source/program")
    if len(order.route_plan) != 1 or order.route_plan[0].label != "Raydium CLMM":
        raise RaydiumPoolStateError("only a single-hop Raydium CLMM route is supported")
    if order.route_plan[0].amm_key != cpi.pool_state:
        raise RaydiumPoolStateError("Jupiter route AMM key differs from the Raydium pool account")
    if cpi.input_token_account == cpi.output_token_account:
        raise RaydiumPoolStateError("Raydium user input/output token accounts unexpectedly alias")
    if not simulation.account_snapshots:
        raise RaydiumPoolStateError("same-slot transaction account snapshots are required")
    snapshots = {item.address: item for item in simulation.account_snapshots}
    if len(snapshots) != len(simulation.account_snapshots):
        raise RaydiumPoolStateError("simulation contains duplicate account snapshots")

    pool_snapshot = _required_snapshot(snapshots, cpi.pool_state, owner=policy.program_id, field="pool state")
    config_snapshot = _required_snapshot(snapshots, cpi.amm_config, owner=policy.program_id, field="AMM config")
    observation_snapshot = _required_snapshot(snapshots, cpi.observation_state, owner=policy.program_id, field="observation")
    tick_snapshot = _required_snapshot(snapshots, cpi.tick_array, owner=policy.program_id, field="tick array")
    input_vault_snapshot = _required_snapshot(snapshots, cpi.input_vault, owner=CLASSIC_TOKEN_PROGRAM_ID, field="input vault")
    output_vault_snapshot = _required_snapshot(snapshots, cpi.output_vault, owner=CLASSIC_TOKEN_PROGRAM_ID, field="output vault")
    input_user_snapshot = _required_snapshot(snapshots, cpi.input_token_account, owner=CLASSIC_TOKEN_PROGRAM_ID, field="user input token account")
    output_user_snapshot = _required_snapshot(snapshots, cpi.output_token_account, owner=CLASSIC_TOKEN_PROGRAM_ID, field="user output token account")

    pool_pre = decode_raydium_pool_state(pool_snapshot.pre_data)
    pool_post = decode_raydium_pool_state(pool_snapshot.post_data)
    static_fields = (
        "bump", "amm_config", "token_mint_0", "token_mint_1", "token_vault_0",
        "token_vault_1", "observation_key", "mint_decimals_0", "mint_decimals_1",
        "tick_spacing", "status", "seed_index",
    )
    if any(getattr(pool_pre, field) != getattr(pool_post, field) for field in static_fields):
        raise RaydiumPoolStateError("Raydium pool identity/configuration fields changed during simulation")
    if pool_pre.status & POOL_STATE_STATUS_SWAP_DISABLED:
        raise RaydiumPoolStateError("Raydium pool has swaps disabled")
    if pool_pre.tick_spacing <= 0:
        raise RaydiumPoolStateError("Raydium pool tick spacing is invalid")
    if bytes(Pubkey.from_string(pool_pre.token_mint_0)) >= bytes(Pubkey.from_string(pool_pre.token_mint_1)):
        raise RaydiumPoolStateError("Raydium pool token mints are not in canonical ascending order")
    _validate_pool_pda(cpi.pool_state, pool_pre, policy.program_id)

    config_tick_spacing_pre = _decode_config_tick_spacing(config_snapshot.pre_data)
    config_tick_spacing_post = _decode_config_tick_spacing(config_snapshot.post_data)
    if config_tick_spacing_pre != config_tick_spacing_post or config_tick_spacing_pre != pool_pre.tick_spacing:
        raise RaydiumPoolStateError("Raydium AMM config tick spacing differs from the pool state")

    observation_pre = _decode_observation_pool_id(observation_snapshot.pre_data)
    observation_post = _decode_observation_pool_id(observation_snapshot.post_data)
    if observation_pre != observation_post or observation_pre != (True, cpi.pool_state):
        raise RaydiumPoolStateError("Raydium observation account is not initialized for the selected pool")

    tick_pre = _decode_tick_array_pool(tick_snapshot.pre_data)
    tick_post = _decode_tick_array_pool(tick_snapshot.post_data)
    if tick_pre != tick_post or tick_pre[0] != cpi.pool_state:
        raise RaydiumPoolStateError("Raydium tick array does not belong to the selected pool")
    ticks_per_array = 60 * pool_pre.tick_spacing
    if ticks_per_array <= 0 or tick_pre[1] % ticks_per_array != 0:
        raise RaydiumPoolStateError("Raydium tick array start index is not aligned to pool tick spacing")
    _validate_tick_array_pda(cpi.tick_array, cpi.pool_state, tick_pre[1], policy.program_id)

    if pool_pre.amm_config != cpi.amm_config or pool_pre.observation_key != cpi.observation_state:
        raise RaydiumPoolStateError("Raydium CPI config/observation accounts differ from the pool state")
    if {pool_pre.token_vault_0, pool_pre.token_vault_1} != {cpi.input_vault, cpi.output_vault}:
        raise RaydiumPoolStateError("Raydium CPI vault accounts differ from the pool state")
    if {pool_pre.token_mint_0, pool_pre.token_mint_1} != {order.input_mint, order.output_mint}:
        raise RaydiumPoolStateError("Raydium pool mint pair differs from the Jupiter route")
    if order.input_mint == pool_pre.token_mint_0:
        expected_input_vault, expected_output_vault = pool_pre.token_vault_0, pool_pre.token_vault_1
    else:
        expected_input_vault, expected_output_vault = pool_pre.token_vault_1, pool_pre.token_vault_0
    if (cpi.input_vault, cpi.output_vault) != (expected_input_vault, expected_output_vault):
        raise RaydiumPoolStateError("Raydium CPI vault direction does not match the route input/output mints")

    input_vault_pre = decode_classic_token_account(input_vault_snapshot.pre_data)
    input_vault_post = decode_classic_token_account(input_vault_snapshot.post_data)
    output_vault_pre = decode_classic_token_account(output_vault_snapshot.pre_data)
    output_vault_post = decode_classic_token_account(output_vault_snapshot.post_data)
    if (
        input_vault_pre.mint != order.input_mint
        or input_vault_post.mint != order.input_mint
        or output_vault_pre.mint != order.output_mint
        or output_vault_post.mint != order.output_mint
    ):
        raise RaydiumPoolStateError("Raydium vault mint does not match the route direction")
    if (
        input_vault_pre.authority != input_vault_post.authority
        or output_vault_pre.authority != output_vault_post.authority
    ):
        raise RaydiumPoolStateError("Raydium vault authority changed during simulation")
    if input_vault_post.amount_raw <= input_vault_pre.amount_raw:
        raise RaydiumPoolStateError("Raydium input vault did not receive tokens during the swap")
    if output_vault_post.amount_raw >= output_vault_pre.amount_raw:
        raise RaydiumPoolStateError("Raydium output vault did not debit tokens during the swap")
    if policy.require_exact_vault_balance_deltas and (
        input_vault_post.amount_raw - input_vault_pre.amount_raw != order.input_amount_raw
        or output_vault_pre.amount_raw - output_vault_post.amount_raw != order.output_amount_raw
    ):
        raise RaydiumPoolStateError("Raydium vault balance deltas differ from the exact route amounts")

    user_input_pre = decode_classic_token_account(input_user_snapshot.pre_data)
    user_input_post = decode_classic_token_account(input_user_snapshot.post_data)
    user_output_pre = decode_classic_token_account(output_user_snapshot.pre_data)
    user_output_post = decode_classic_token_account(output_user_snapshot.post_data)
    if (user_input_pre.mint, user_input_pre.authority) != (order.input_mint, order.taker):
        raise RaydiumPoolStateError("user input token account mint or authority differs from the taker order")
    if (user_output_pre.mint, user_output_pre.authority) != (order.output_mint, order.taker):
        raise RaydiumPoolStateError("user output token account mint or authority differs from the taker order")
    if (user_input_post.mint, user_input_post.authority) != (order.input_mint, order.taker):
        raise RaydiumPoolStateError("user input token account changed mint or authority")
    if (user_output_post.mint, user_output_post.authority) != (order.output_mint, order.taker):
        raise RaydiumPoolStateError("user output token account changed mint or authority")
    if user_input_pre.amount_raw - user_input_post.amount_raw != order.input_amount_raw:
        raise RaydiumPoolStateError("user input token account debit differs from the exact Jupiter input amount")
    if user_output_post.amount_raw - user_output_pre.amount_raw != order.output_amount_raw:
        raise RaydiumPoolStateError("user output token account credit differs from the exact Jupiter output amount")

    balance_map = {item.token_account: item for item in simulation.token_balances}
    for address, pre, post in (
        (cpi.input_token_account, user_input_pre, user_input_post),
        (cpi.output_token_account, user_output_pre, user_output_post),
    ):
        recorded = balance_map.get(address)
        if recorded is None or (
            recorded.mint != pre.mint
            or recorded.owner != pre.authority
            or recorded.token_program_id != CLASSIC_TOKEN_PROGRAM_ID
            or recorded.pre_amount_raw != pre.amount_raw
            or recorded.post_amount_raw != post.amount_raw
        ):
            raise RaydiumPoolStateError("token account raw data differs from the simulation balance evidence")

    input_vault_delta = input_vault_post.amount_raw - input_vault_pre.amount_raw
    output_vault_delta = output_vault_post.amount_raw - output_vault_pre.amount_raw
    if policy.require_exact_vault_balance_deltas and (
        input_vault_delta != order.input_amount_raw
        or output_vault_delta != -order.output_amount_raw
    ):
        raise RaydiumPoolStateError("Raydium vault balance deltas differ from the exact route amounts")

    return RaydiumPoolStateResult(
        passed=True,
        message_sha256=digest,
        pool_state=cpi.pool_state,
        amm_config=cpi.amm_config,
        token_mint_0=pool_pre.token_mint_0,
        token_mint_1=pool_pre.token_mint_1,
        token_vault_0=pool_pre.token_vault_0,
        token_vault_1=pool_pre.token_vault_1,
        observation_state=cpi.observation_state,
        tick_array=cpi.tick_array,
        tick_array_start_index=tick_pre[1],
        tick_spacing=pool_pre.tick_spacing,
        pool_status=pool_pre.status,
        input_vault_delta_raw=input_vault_delta,
        output_vault_delta_raw=output_vault_delta,
    )
