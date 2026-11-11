"""Read-only Solana Devnet wallet balance preflight.

This module never requests signatures, builds transactions, or submits RPC
methods other than getGenesisHash and getBalance.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

import requests
from solders.pubkey import Pubkey

DEVNET_RPC_URL = "https://api.devnet.solana.com"
DEVNET_GENESIS_HASH = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
DEFAULT_WALLET = "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"


class DevnetPreflightError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DevnetWalletEvidence:
    evidence_type: str
    status: str
    cluster: str
    genesis_hash: str
    wallet_address: str
    balance_lamports: int
    balance_sol: str
    rpc_endpoint: str
    transaction_constructed: bool = False
    transaction_signed: bool = False
    transaction_submitted: bool = False
    transaction_broadcast: bool = False
    live_trading: bool = False


class SolanaDevnetWalletPreflight:
    def __init__(self, *, session: Any | None = None, timeout_seconds: float = 10.0) -> None:
        if not 0 < timeout_seconds <= 20:
            raise DevnetPreflightError("timeout_seconds must be greater than zero and at most 20")
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)

    def _rpc(self, method: str, params: list[Any] | None = None) -> Any:
        response = self.session.post(
            DEVNET_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
            headers={"Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("error") is not None or "result" not in payload:
            raise DevnetPreflightError(f"Devnet RPC {method} returned an invalid response")
        return payload["result"]

    def check(self, wallet_address: str) -> DevnetWalletEvidence:
        try:
            wallet = str(Pubkey.from_string(wallet_address))
        except Exception as exc:
            raise DevnetPreflightError("wallet_address is not a valid Solana public key") from exc

        genesis = self._rpc("getGenesisHash")
        if genesis != DEVNET_GENESIS_HASH:
            raise DevnetPreflightError(
                "RPC endpoint genesis hash mismatch: "
                f"received={genesis!r}; expected_devnet={DEVNET_GENESIS_HASH!r}"
            )
        result = self._rpc("getBalance", [wallet, {"commitment": "confirmed"}])
        if not isinstance(result, dict) or isinstance(result.get("value"), bool) or not isinstance(result.get("value"), int) or result["value"] < 0:
            raise DevnetPreflightError("Devnet getBalance returned an invalid balance")
        lamports = result["value"]
        return DevnetWalletEvidence(
            evidence_type="solana_devnet_wallet_preflight",
            status="balance_verified",
            cluster="devnet",
            genesis_hash=genesis,
            wallet_address=wallet,
            balance_lamports=lamports,
            balance_sol=f"{lamports / 1_000_000_000:.9f}",
            rpc_endpoint=DEVNET_RPC_URL,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Solana Devnet wallet balance check")
    parser.add_argument("--wallet", required=True, help="Public Solana wallet address")
    parser.add_argument("--output", default="artifacts/solana-devnet-wallet-preflight.json")
    args = parser.parse_args()
    try:
        evidence = SolanaDevnetWalletPreflight().check(args.wallet)
        payload = asdict(evidence)
        exit_code = 0
    except Exception as exc:
        payload = {
            "evidence_type": "solana_devnet_wallet_preflight",
            "status": "failed",
            "cluster": "devnet",
            "wallet_address": args.wallet,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "transaction_constructed": False,
            "transaction_signed": False,
            "transaction_submitted": False,
            "transaction_broadcast": False,
            "live_trading": False,
        }
        exit_code = 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
