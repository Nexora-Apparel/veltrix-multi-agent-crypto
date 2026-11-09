"""Read-only finalized token-balance preflight for the simulation test wallet."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .base import ProviderError
from ..solana_meme.candidates import canonical_mint

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass(frozen=True)
class SolanaSimulationWalletPreflight:
    mint: str
    wallet: str
    required_amount_raw: int
    token_balance_raw: int
    token_account_count: int
    sol_balance_lamports: int
    finalized_slot: int
    observed_at_ms: int
    state: str
    token_program_ids: tuple[str, ...]


class SolanaSimulationWalletPreflightProvider:
    """Check exact token holdings and SOL balance without signing or sending."""

    provider_id = "solana-simulation-wallet-preflight"

    def __init__(
        self, rpc_url: str, wallet_public_key: str, *,
        session: Any | None = None, timeout_seconds: float = 12.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(rpc_url, str) or not rpc_url.startswith("https://"):
            raise ValueError("Solana RPC URL must use HTTPS")
        self.rpc_url = rpc_url
        self.wallet = canonical_mint(wallet_public_key)
        self.session = session or requests
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _rpc(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = self.session.post(
                self.rpc_url, json=payload, timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise ProviderError(f"Solana preflight {method} failed: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or body.get("error") is not None:
            raise ProviderError(f"Solana preflight {method} returned an RPC error")
        result = body.get("result")
        if not isinstance(result, dict):
            raise ProviderError(f"Solana preflight {method} returned malformed data")
        return result

    def fetch(self, mint: str, required_amount_raw: int) -> SolanaSimulationWalletPreflight:
        canonical = canonical_mint(mint)
        if (isinstance(required_amount_raw, bool) or not isinstance(required_amount_raw, int)
                or required_amount_raw <= 0):
            raise ValueError("required_amount_raw must be a positive integer")

        token_result = self._rpc("getTokenAccountsByOwner", [
            self.wallet, {"mint": canonical},
            {"encoding": "jsonParsed", "commitment": "finalized"},
        ])
        balance_result = self._rpc("getBalance", [
            self.wallet, {"commitment": "finalized"},
        ])
        token_context = token_result.get("context")
        balance_context = balance_result.get("context")
        token_slot = token_context.get("slot") if isinstance(token_context, dict) else None
        balance_slot = balance_context.get("slot") if isinstance(balance_context, dict) else None
        for slot in (token_slot, balance_slot):
            if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
                raise ProviderError("Solana preflight finalized slot is invalid")

        accounts = token_result.get("value")
        if not isinstance(accounts, list):
            raise ProviderError("Solana preflight token-account list is malformed")
        total = 0
        programs: set[str] = set()
        seen: set[str] = set()
        for item in accounts:
            if not isinstance(item, dict):
                raise ProviderError("Solana preflight token-account row is malformed")
            pubkey = item.get("pubkey")
            account = item.get("account")
            program = account.get("owner") if isinstance(account, dict) else None
            data = account.get("data") if isinstance(account, dict) else None
            parsed = data.get("parsed") if isinstance(data, dict) else None
            info = parsed.get("info") if isinstance(parsed, dict) else None
            token_amount = info.get("tokenAmount") if isinstance(info, dict) else None
            if not isinstance(pubkey, str) or not pubkey or pubkey in seen:
                raise ProviderError("Solana preflight token-account identity is invalid")
            if program not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
                raise ProviderError("Solana preflight token account uses an unsupported program")
            if not isinstance(info, dict) or info.get("mint") != canonical or info.get("owner") != self.wallet:
                raise ProviderError("Solana preflight token account owner or mint mismatch")
            if not isinstance(token_amount, dict) or token_amount.get("decimals") is None:
                raise ProviderError("Solana preflight token amount metadata is malformed")
            raw = token_amount.get("amount")
            if not isinstance(raw, str) or not raw.isdigit():
                raise ProviderError("Solana preflight raw token amount is malformed")
            if info.get("state") not in (None, "initialized"):
                raise ProviderError("Solana preflight token account is not initialized")
            seen.add(pubkey)
            programs.add(program)
            total += int(raw)

        balance_value = balance_result.get("value")
        if isinstance(balance_value, bool) or not isinstance(balance_value, int) or balance_value < 0:
            raise ProviderError("Solana preflight native SOL balance is malformed")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ProviderError("Solana preflight timestamp is invalid")
        return SolanaSimulationWalletPreflight(
            mint=canonical, wallet=self.wallet,
            required_amount_raw=required_amount_raw,
            token_balance_raw=total, token_account_count=len(accounts),
            sol_balance_lamports=balance_value,
            finalized_slot=min(token_slot, balance_slot),
            observed_at_ms=now,
            state="BALANCE_READY" if total >= required_amount_raw else "INSUFFICIENT_TOKEN_BALANCE",
            token_program_ids=tuple(sorted(programs)),
        )
