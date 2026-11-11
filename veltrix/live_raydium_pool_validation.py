"""Read-only live mainnet validation for one pinned Raydium CLMM pool.

This command fetches real account bytes and validates the PoolState, AMM config,
ObservationState, and both SPL token vaults. It does not construct, sign, or
submit a transaction. The output is an account-validation report, not swap
simulation parity evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_observation import (
    decode_raydium_clmm_observation_state,
    derive_raydium_clmm_observation_address,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    decode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_replay_config import decode_raydium_clmm_amm_config
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter

DEFAULT_POOL = "CYbD9RaToYMtWKA7QZyoLahnHdWq553Vm62Lh6qWtuxq"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
ALLOWED_HOSTS = {"mainnet.helius-rpc.com", "api.mainnet-beta.solana.com"}


class HeliusHeaderSession:
    """Pass Helius query credentials as a header without logging or retaining URL query data."""
    def __init__(self, api_key: str | None):
        import requests
        self._session = requests.Session()
        self._api_key = api_key

    def post(self, endpoint, *, json, headers, timeout, allow_redirects):
        request_headers = dict(headers)
        if self._api_key:
            request_headers["X-Api-Key"] = self._api_key
        return self._session.post(
            endpoint, json=json, headers=request_headers,
            timeout=timeout, allow_redirects=allow_redirects,
        )


def account_evidence(account, label: str) -> DexPoolAccountEvidence:
    return DexPoolAccountEvidence(
        amm_key=account.address,
        dex_label=label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=account.owner,
        lamports=account.lamports,
        executable=account.executable,
        data_length=len(account.data),
        data_sha256=hashlib.sha256(account.data).hexdigest(),
        data_bytes=account.data,
    )


def decode_token_vault(account, expected_mint: str, expected_authority: str) -> dict:
    if account.owner not in TOKEN_PROGRAMS:
        raise ValueError(f"vault {account.address} is not owned by a supported SPL Token program")
    data = account.data
    if len(data) < 165:
        raise ValueError(f"vault {account.address} is shorter than the SPL token account base layout")
    mint = str(Pubkey.from_bytes(data[0:32]))
    token_owner = str(Pubkey.from_bytes(data[32:64]))
    amount = int.from_bytes(data[64:72], "little")
    if mint != expected_mint:
        raise ValueError(f"vault {account.address} mint does not match PoolState")
    if token_owner != expected_authority:
        raise ValueError(
            f"vault {account.address} token authority does not match the Raydium pool authority"
        )
    return {
        "address": account.address,
        "token_program": account.owner,
        "mint": mint,
        "token_authority": token_owner,
        "amount_raw": str(amount),
        "data_length": len(data),
        "data_sha256": hashlib.sha256(data).hexdigest(),
    }



def require_stable_pool_dependencies(
    discovered: tuple[str, ...], authoritative: tuple[str, ...]
) -> None:
    """Fail closed if PoolState references changed before the authoritative snapshot."""
    if discovered != authoritative:
        raise ValueError(
            "PoolState dependencies changed between discovery and authoritative snapshot; retry validation"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", default=DEFAULT_POOL)
    parser.add_argument("--output", default="artifacts/raydium-live-pool-validation.json")
    args = parser.parse_args()

    endpoint_secret = os.environ.get("SOLANA_RPC_URL", "").strip()
    if not endpoint_secret:
        raise SystemExit("SOLANA_RPC_URL repository secret is missing")
    parsed = urlsplit(endpoint_secret)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise SystemExit("SOLANA_RPC_URL must use HTTPS on an explicitly supported mainnet RPC host")
    if parsed.username or parsed.password or parsed.fragment:
        raise SystemExit("SOLANA_RPC_URL contains unsupported URL credentials or fragment")
    query = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
    if set(query) - {"api-key"} or len(query.get("api-key", [])) > 1:
        raise SystemExit("SOLANA_RPC_URL has unsupported query parameters")
    api_key = query.get("api-key", [None])[0]
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))

    pool_address = str(Pubkey.from_string(args.pool))
    adapter = SolanaRpcSimulationAdapter(
        endpoint=endpoint,
        provider_id="configured-mainnet-rpc",
        cluster="mainnet-beta",
        expected_genesis_hash=MAINNET_GENESIS,
        allowed_hosts=tuple(ALLOWED_HOSTS),
        timeout_seconds=12.0,
        session=HeliusHeaderSession(api_key),
    )

    # First fetch only PoolState, then derive all dependent addresses from decoded bytes.
    pool_snapshot = adapter.fetch_account_snapshot((pool_address,), min_context_slot=0)
    pool_account = pool_snapshot.accounts[0]
    pool_evidence = account_evidence(pool_account, "Raydium CLMM")
    pool = decode_raydium_clmm_pool_state(
        pool_evidence, expected_pool_address=pool_address
    )
    if {pool.token_mint_0, pool.token_mint_1} != {WSOL_MINT, USDC_MINT}:
        raise SystemExit("Selected PoolState is not the expected WSOL/USDC mint pair")
    if pool.liquidity <= 0 or not pool.swaps_enabled:
        raise SystemExit("Pool has zero liquidity or swaps are disabled")

    observation_address, _ = derive_raydium_clmm_observation_address(pool_address)
    addresses = (
        pool.amm_config, pool.observation_key,
        pool.token_vault_0, pool.token_vault_1,
    )
    if observation_address != pool.observation_key:
        raise SystemExit("PoolState observation key is not the canonical pool-derived PDA")
    # The first PoolState read only discovers dependent addresses. The authoritative
    # evidence snapshot re-fetches PoolState and every dependency in ONE RPC call,
    # so all reported account bytes share the same bank/context slot.
    authoritative_addresses = (pool_address, *addresses)
    snapshot = adapter.fetch_account_snapshot(
        authoritative_addresses, min_context_slot=pool_snapshot.context_slot
    )
    by_address = {account.address: account for account in snapshot.accounts}
    if len(by_address) != len(authoritative_addresses):
        raise SystemExit("RPC snapshot returned duplicate or missing pool/dependent accounts")

    authoritative_pool_account = by_address[pool_address]
    authoritative_pool = decode_raydium_clmm_pool_state(
        account_evidence(authoritative_pool_account, "Raydium CLMM"),
        expected_pool_address=pool_address,
    )
    if {authoritative_pool.token_mint_0, authoritative_pool.token_mint_1} != {WSOL_MINT, USDC_MINT}:
        raise SystemExit("Authoritative PoolState is not the expected WSOL/USDC mint pair")
    if authoritative_pool.liquidity <= 0 or not authoritative_pool.swaps_enabled:
        raise SystemExit("Authoritative pool has zero liquidity or swaps are disabled")
    authoritative_observation, _ = derive_raydium_clmm_observation_address(pool_address)
    if authoritative_observation != authoritative_pool.observation_key:
        raise SystemExit("Authoritative PoolState observation key is not the canonical pool-derived PDA")
    authoritative_dependencies = (
        authoritative_pool.amm_config,
        authoritative_pool.observation_key,
        authoritative_pool.token_vault_0,
        authoritative_pool.token_vault_1,
    )
    try:
        require_stable_pool_dependencies(addresses, authoritative_dependencies)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    pool = authoritative_pool

    def evidence(address: str, label: str) -> DexPoolAccountEvidence:
        account = by_address[address]
        if account.owner != RAYDIUM_CLMM_PROGRAM_ID or account.executable or account.lamports <= 0:
            raise ValueError(f"{label} account owner/executable/lamports validation failed")
        return account_evidence(account, label)

    config = decode_raydium_clmm_amm_config(
        evidence(pool.amm_config, "Raydium CLMM AMM Config"),
        expected_address=pool.amm_config,
        expected_tick_spacing=pool.tick_spacing,
    )
    observation = decode_raydium_clmm_observation_state(
        evidence(pool.observation_key, "Raydium CLMM Observation"),
        expected_address=pool.observation_key,
        expected_pool_address=pool_address,
    )
    vault0 = decode_token_vault(by_address[pool.token_vault_0], pool.token_mint_0, pool_address)
    vault1 = decode_token_vault(by_address[pool.token_vault_1], pool.token_mint_1, pool_address)

    report = {
        "schema_version": 1,
        "evidence_type": "live_mainnet_pool_account_validation",
        "execution_ready": False,
        "swap_simulation_performed": False,
        "transaction_signed": False,
        "transaction_submitted": False,
        "provider_id": snapshot.provider_id,
        "cluster": snapshot.cluster,
        "genesis_hash": snapshot.genesis_hash,
        "context_slot": snapshot.context_slot,
        "snapshot_account_addresses": list(authoritative_addresses),
        "pool_address": pool.pool_address,
        "program_id": RAYDIUM_CLMM_PROGRAM_ID,
        "source_commit": pool.source_commit,
        "pool_state": {
            "data_length": len(pool.raw_data),
            "data_sha256": pool.account_data_sha256,
            "mint_0": pool.token_mint_0,
            "mint_1": pool.token_mint_1,
            "vault_0": pool.token_vault_0,
            "vault_1": pool.token_vault_1,
            "observation": pool.observation_key,
            "amm_config": pool.amm_config,
            "tick_spacing": pool.tick_spacing,
            "current_tick": pool.tick_current,
            "liquidity_raw": str(pool.liquidity),
            "sqrt_price_x64": str(pool.sqrt_price_x64),
            "swaps_enabled": pool.swaps_enabled,
        },
        "amm_config": {
            "address": config.address,
            "data_sha256": config.data_sha256,
            "index": config.index,
            "tick_spacing": config.tick_spacing,
            "trade_fee_rate": config.trade_fee_rate,
            "protocol_fee_rate": config.protocol_fee_rate,
            "fund_fee_rate": config.fund_fee_rate,
        },
        "observation_state": {
            "address": observation.address,
            "data_sha256": observation.data_sha256,
            "initialized": observation.initialized,
            "observation_index": observation.observation_index,
            "record_count": len(observation.observations),
        },
        "vaults": [vault0, vault1],
        "validation": {
            "pool_owner_and_layout": "passed",
            "pool_pda_and_mint_pair": "passed",
            "amm_config_owner_layout_and_tick_spacing": "passed",
            "observation_owner_layout_and_pool_binding": "passed",
            "both_vault_token_programs_mints_and_balances": "passed",
            "all_snapshots_same_provider_and_mainnet": "passed",
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "PASS",
        "pool": pool_address,
        "context_slot": snapshot.context_slot,
        "report": str(output),
        "note": "Account validation only; no swap simulation or execution was performed.",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
