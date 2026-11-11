"""Same-provider, same-or-newer-slot Raydium CLMM vault token-account verification."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolRouteCheck,
    RaydiumClmmPoolState,
)
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


CLASSIC_SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
RAYDIUM_POOL_VAULT_SEED = b"pool_vault"
CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH = 165
TOKEN_ACCOUNT_INITIALIZED = 1


class RaydiumClmmVaultError(ValueError):
    """Raised when a Raydium pool vault is not the expected initialized token account."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmVaultAccount:
    address: str
    pool_address: str
    mint: str
    token_program_id: str
    token_authority: str
    amount_raw: int
    state: int
    lamports: int
    data_sha256: str
    account_data: bytes = b""
    source_data_sha256: str = ""


@dataclass(frozen=True, slots=True)
class RaydiumClmmVaultSnapshot:
    route_message_sha256: str
    simulation_response_id: str
    simulation_context_slot: int
    provider_id: str
    cluster: str
    genesis_hash: str
    context_slot: int
    vaults: tuple[RaydiumClmmVaultAccount, ...]
    execution_ready: bool = False


def derive_raydium_clmm_vault_address(*, pool_address: str, mint: str) -> str:
    try:
        pool = Pubkey.from_string(pool_address)
        mint_key = Pubkey.from_string(mint)
        program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        address, _ = Pubkey.find_program_address(
            [RAYDIUM_POOL_VAULT_SEED, bytes(pool), bytes(mint_key)],
            program,
        )
        return str(address)
    except Exception as exc:
        raise RaydiumClmmVaultError("Raydium vault PDA derivation failed") from exc


def _decode_token_account(data: bytes, *, address: str, pool_address: str, mint: str, lamports: int, data_sha256: str) -> RaydiumClmmVaultAccount:
    if not isinstance(data, bytes) or len(data) != CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH:
        raise RaydiumClmmVaultError("Raydium vault is not an exact 165-byte classic SPL token account")
    if hashlib.sha256(data).hexdigest() != data_sha256:
        raise RaydiumClmmVaultError("Raydium vault data hash differs from the fetched account evidence")
    observed_mint = str(Pubkey.from_bytes(data[0:32]))
    token_authority = str(Pubkey.from_bytes(data[32:64]))
    amount_raw = int.from_bytes(data[64:72], "little")
    delegate_option = int.from_bytes(data[72:76], "little")
    state = data[108]
    native_option = int.from_bytes(data[109:113], "little")
    close_authority_option = int.from_bytes(data[129:133], "little")
    if observed_mint != mint:
        raise RaydiumClmmVaultError("Raydium vault token mint differs from the pool's expected mint")
    if token_authority != pool_address:
        raise RaydiumClmmVaultError("Raydium vault token authority is not the pool PDA")
    if state != TOKEN_ACCOUNT_INITIALIZED:
        raise RaydiumClmmVaultError("Raydium vault token account is not initialized or is frozen")
    if delegate_option not in (0, 1) or native_option not in (0, 1) or close_authority_option not in (0, 1):
        raise RaydiumClmmVaultError("Raydium vault contains an invalid SPL token COption tag")
    if delegate_option != 0:
        raise RaydiumClmmVaultError("Raydium vault has an unexpected delegate authority")
    if native_option != 0:
        raise RaydiumClmmVaultError("wrapped-native vaults are outside the supported classic SPL route policy")
    if close_authority_option != 0:
        raise RaydiumClmmVaultError("Raydium vault has an unexpected close authority")
    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RaydiumClmmVaultError("Raydium vault account must have positive lamports")
    return RaydiumClmmVaultAccount(
        address=address,
        pool_address=pool_address,
        mint=mint,
        token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
        token_authority=token_authority,
        amount_raw=amount_raw,
        state=state,
        lamports=lamports,
        data_sha256=data_sha256,
        account_data=data,
        source_data_sha256=data_sha256,
    )


def encode_raydium_clmm_vault_account(vault: RaydiumClmmVaultAccount) -> bytes:
    """Patch the token amount into the verified 165-byte classic SPL account."""
    if not isinstance(vault, RaydiumClmmVaultAccount):
        raise RaydiumClmmVaultError("verified Raydium vault account is required")
    if vault.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID:
        raise RaydiumClmmVaultError("vault token program is not the classic SPL Token program")
    if vault.address != derive_raydium_clmm_vault_address(
        pool_address=vault.pool_address, mint=vault.mint
    ):
        raise RaydiumClmmVaultError("vault address is not the canonical pool/mint PDA")
    if (
        not isinstance(vault.account_data, bytes)
        or len(vault.account_data) != CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH
    ):
        raise RaydiumClmmVaultError("verified original 165-byte vault account data is required")
    source_hash = vault.source_data_sha256 or vault.data_sha256
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or hashlib.sha256(vault.account_data).hexdigest() != source_hash
    ):
        raise RaydiumClmmVaultError("original vault bytes do not match their source commitment")
    if (
        isinstance(vault.amount_raw, bool)
        or not isinstance(vault.amount_raw, int)
        or not 0 <= vault.amount_raw < (1 << 64)
    ):
        raise RaydiumClmmVaultError("vault amount must fit u64")
    data = bytearray(vault.account_data)
    data[64:72] = vault.amount_raw.to_bytes(8, "little")
    encoded = bytes(data)
    _decode_token_account(
        encoded,
        address=vault.address,
        pool_address=vault.pool_address,
        mint=vault.mint,
        lamports=vault.lamports,
        data_sha256=hashlib.sha256(encoded).hexdigest(),
    )
    return encoded


def apply_raydium_clmm_vault_delta(
    vault: RaydiumClmmVaultAccount, *, amount_delta: int
) -> RaydiumClmmVaultAccount:
    """Apply one signed base-unit delta and commit the resulting token-account bytes."""
    if isinstance(amount_delta, bool) or not isinstance(amount_delta, int):
        raise RaydiumClmmVaultError("vault amount delta must be an integer")
    next_amount = vault.amount_raw + amount_delta
    if not 0 <= next_amount < (1 << 64):
        raise RaydiumClmmVaultError("vault balance delta underflows or overflows u64")
    updated = replace(vault, amount_raw=next_amount, data_sha256="")
    encoded = encode_raydium_clmm_vault_account(updated)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(
        updated,
        account_data=encoded,
        data_sha256=digest,
        source_data_sha256=digest,
    )


def apply_raydium_clmm_swap_vault_settlement(
    pool: RaydiumClmmPoolState,
    vault_0: RaydiumClmmVaultAccount,
    vault_1: RaydiumClmmVaultAccount,
    *,
    amount_0: int,
    amount_1: int,
    zero_for_one: bool,
) -> tuple[RaydiumClmmVaultAccount, RaydiumClmmVaultAccount]:
    """Reconcile the two pool-vault deltas returned by swap_internal."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmVaultError("decoded Raydium PoolState is required")
    if not isinstance(vault_0, RaydiumClmmVaultAccount) or not isinstance(vault_1, RaydiumClmmVaultAccount):
        raise RaydiumClmmVaultError("two verified Raydium vault accounts are required")
    if (
        vault_0.pool_address != pool.pool_address
        or vault_1.pool_address != pool.pool_address
        or vault_0.mint != pool.token_mint_0
        or vault_1.mint != pool.token_mint_1
        or vault_0.address != pool.token_vault_0
        or vault_1.address != pool.token_vault_1
    ):
        raise RaydiumClmmVaultError("vault accounts do not match PoolState token-0/token-1 identities")
    if not isinstance(zero_for_one, bool):
        raise RaydiumClmmVaultError("swap direction must be boolean")
    for value, field in ((amount_0, "amount_0"), (amount_1, "amount_1")):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < (1 << 64):
            raise RaydiumClmmVaultError(f"{field} must be a positive u64 amount")
    if zero_for_one:
        return (
            apply_raydium_clmm_vault_delta(vault_0, amount_delta=amount_0),
            apply_raydium_clmm_vault_delta(vault_1, amount_delta=-amount_1),
        )
    return (
        apply_raydium_clmm_vault_delta(vault_0, amount_delta=-amount_0),
        apply_raydium_clmm_vault_delta(vault_1, amount_delta=amount_1),
    )



def validate_raydium_exact_internal_settlement(
    pool: RaydiumClmmPoolState,
    vault_0_before: RaydiumClmmVaultAccount,
    vault_1_before: RaydiumClmmVaultAccount,
    vault_0_after: RaydiumClmmVaultAccount,
    vault_1_after: RaydiumClmmVaultAccount,
    *,
    amount_0: int,
    amount_1: int,
    zero_for_one: bool,
    is_base_input: bool,
    amount_specified: int,
    sqrt_price_before_x64: int,
    sqrt_price_after_x64: int,
    sqrt_price_limit_x64: int,
) -> int:
    """Validate exact_internal vault reloads, price direction, and fill rules.

    Returns actual output for exact-input swaps and actual input for
    exact-output swaps, matching the pinned wrapper's return convention.
    """
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmVaultError("decoded Raydium PoolState is required")
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool):
        raise RaydiumClmmVaultError("swap direction and exact-input flag must be boolean")
    before = (vault_0_before, vault_1_before)
    after = (vault_0_after, vault_1_after)
    if any(not isinstance(item, RaydiumClmmVaultAccount) for item in before + after):
        raise RaydiumClmmVaultError("four verified before/after pool vault states are required")
    for index, (prior, current) in enumerate(zip(before, after)):
        if (
            prior.address != current.address
            or prior.pool_address != current.pool_address
            or prior.mint != current.mint
            or prior.pool_address != pool.pool_address
        ):
            raise RaydiumClmmVaultError(f"vault {index} identity changed during exact_internal")
    if (
        vault_0_before.address != pool.token_vault_0
        or vault_1_before.address != pool.token_vault_1
        or vault_0_before.mint != pool.token_mint_0
        or vault_1_before.mint != pool.token_mint_1
    ):
        raise RaydiumClmmVaultError("before/after vaults do not match PoolState token identities")

    for value, field in (
        (amount_0, "amount_0"),
        (amount_1, "amount_1"),
        (amount_specified, "amount_specified"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < (1 << 64):
            raise RaydiumClmmVaultError(f"{field} must be a positive u64 amount")
    for value, field in (
        (sqrt_price_before_x64, "sqrt_price_before_x64"),
        (sqrt_price_after_x64, "sqrt_price_after_x64"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < (1 << 128):
            raise RaydiumClmmVaultError(f"{field} must be a positive u128 value")
    if (
        isinstance(sqrt_price_limit_x64, bool)
        or not isinstance(sqrt_price_limit_x64, int)
        or not 0 <= sqrt_price_limit_x64 < (1 << 128)
    ):
        raise RaydiumClmmVaultError("sqrt_price_limit_x64 must be zero or a u128 value")

    delta_0 = vault_0_after.amount_raw - vault_0_before.amount_raw
    delta_1 = vault_1_after.amount_raw - vault_1_before.amount_raw
    expected = (
        (amount_0, -amount_1)
        if zero_for_one
        else (-amount_0, amount_1)
    )
    if (delta_0, delta_1) != expected:
        raise RaydiumClmmVaultError("reloaded pool-vault balances do not match swap_internal amounts")

    if zero_for_one:
        if sqrt_price_before_x64 < sqrt_price_after_x64:
            raise RaydiumClmmVaultError("zero-for-one sqrt price moved in the wrong direction")
        output_before = vault_1_before.amount_raw
        output_after = vault_1_after.amount_raw
        input_before = vault_0_before.amount_raw
        input_after = vault_0_after.amount_raw
        exact_input_amount = amount_0
        exact_output_amount = amount_1
        if sqrt_price_limit_x64 and sqrt_price_after_x64 < sqrt_price_limit_x64:
            raise RaydiumClmmVaultError("zero-for-one sqrt price crossed its configured limit")
    else:
        if sqrt_price_after_x64 < sqrt_price_before_x64:
            raise RaydiumClmmVaultError("one-for-zero sqrt price moved in the wrong direction")
        output_before = vault_0_before.amount_raw
        output_after = vault_0_after.amount_raw
        input_before = vault_1_before.amount_raw
        input_after = vault_1_after.amount_raw
        exact_input_amount = amount_1
        exact_output_amount = amount_0
        if sqrt_price_limit_x64 and sqrt_price_after_x64 > sqrt_price_limit_x64:
            raise RaydiumClmmVaultError("one-for-zero sqrt price crossed its configured limit")

    if sqrt_price_limit_x64 == 0:
        required = exact_input_amount if is_base_input else exact_output_amount
        if amount_specified != required:
            raise RaydiumClmmVaultError("no-limit exact swap did not fill the specified amount")
    actual_output = output_before - output_after
    actual_input = input_after - input_before
    if actual_output <= 0 or actual_input <= 0:
        raise RaydiumClmmVaultError("exact_internal reloaded vault deltas must both be positive")
    return actual_output if is_base_input else actual_input


def fetch_raydium_clmm_vault_snapshot(
    pool_evidence: DexPoolEvidenceSnapshot,
    *,
    pool_checks: tuple[RaydiumClmmPoolRouteCheck, ...],
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
) -> RaydiumClmmVaultSnapshot:
    """Fetch both pool vaults and verify their token-account mint/authority/PDA roles."""
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmVaultError("provenance-bound DEX pool evidence is required")
    if not isinstance(pool_checks, tuple) or not pool_checks or any(
        not isinstance(item, RaydiumClmmPoolRouteCheck) or item.passed is not True
        for item in pool_checks
    ):
        raise RaydiumClmmVaultError("successful Raydium CLMM pool route checks are required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmVaultError("Solana RPC simulation evidence is required")
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumClmmVaultError("provider-pinned Solana RPC adapter is required")
    if (
        simulation.message_sha256 != pool_evidence.route_message_sha256
        or simulation.response_id != pool_evidence.simulation_response_id
        or simulation.context_slot != pool_evidence.simulation_context_slot
    ):
        raise RaydiumClmmVaultError("pool evidence and simulation response/slot/digest do not match")
    if (
        pool_evidence.provider_id != simulation.provider_id
        or pool_evidence.cluster != simulation.cluster
        or pool_evidence.genesis_hash != simulation.genesis_hash
        or rpc_adapter.provider_id != simulation.provider_id
        or rpc_adapter.cluster != simulation.cluster
        or rpc_adapter.expected_genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmVaultError("vault, pool and simulation RPC provenance must use the same provider and cluster")

    expected: dict[str, tuple[str, str]] = {}
    for check in pool_checks:
        pool = check.pool
        if pool.source_commit == "":
            raise RaydiumClmmVaultError("Raydium pool source layout provenance is missing")
        for address, mint in (
            (pool.token_vault_0, pool.token_mint_0),
            (pool.token_vault_1, pool.token_mint_1),
        ):
            derived = derive_raydium_clmm_vault_address(pool_address=pool.pool_address, mint=mint)
            if derived != address:
                raise RaydiumClmmVaultError("pool vault address does not match the canonical Raydium vault PDA")
            prior = expected.get(address)
            if prior is not None and prior != (pool.pool_address, mint):
                raise RaydiumClmmVaultError("one vault address is assigned to conflicting pool/mint roles")
            expected[address] = (pool.pool_address, mint)
    if not expected:
        raise RaydiumClmmVaultError("Raydium pool checks contain no vault accounts")

    minimum_slot = max(simulation.context_slot, pool_evidence.context_slot)
    snapshot = rpc_adapter.fetch_account_snapshot(tuple(sorted(expected)), min_context_slot=minimum_slot)
    if (
        snapshot.provider_id != simulation.provider_id
        or snapshot.cluster != simulation.cluster
        or snapshot.genesis_hash != simulation.genesis_hash
        or snapshot.context_slot < minimum_slot
    ):
        raise RaydiumClmmVaultError("vault snapshot provider, cluster or context slot differs from the approved evidence")
    if len(snapshot.accounts) != len(expected):
        raise RaydiumClmmVaultError("vault RPC snapshot account count differs from the expected pool vault set")

    vaults = []
    seen: set[str] = set()
    for account in snapshot.accounts:
        if account.address in seen:
            raise RaydiumClmmVaultError("vault snapshot contains duplicate account addresses")
        seen.add(account.address)
        role = expected.get(account.address)
        if role is None:
            raise RaydiumClmmVaultError("vault RPC snapshot returned an unexpected account")
        pool_address, mint = role
        if account.owner != CLASSIC_SPL_TOKEN_PROGRAM_ID:
            raise RaydiumClmmVaultError("Raydium vault is not owned by the classic SPL Token program")
        if account.executable:
            raise RaydiumClmmVaultError("Raydium vault token account must not be executable")
        if not isinstance(account.data, bytes):
            raise RaydiumClmmVaultError("Raydium vault account data is missing")
        vaults.append(_decode_token_account(
            account.data,
            address=account.address,
            pool_address=pool_address,
            mint=mint,
            lamports=account.lamports,
            data_sha256=hashlib.sha256(account.data).hexdigest(),
        ))
    if seen != set(expected):
        raise RaydiumClmmVaultError("vault RPC snapshot omitted one or more pool vaults")

    return RaydiumClmmVaultSnapshot(
        route_message_sha256=simulation.message_sha256,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        vaults=tuple(vaults),
        execution_ready=False,
    )
