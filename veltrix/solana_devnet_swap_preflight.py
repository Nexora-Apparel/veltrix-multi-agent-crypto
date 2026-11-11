"""Read-only Raydium Devnet swap preparation and unsigned simulation.

This module discovers a live Devnet SOL/USDC route, asks Raydium's Devnet
Trade API to build an unsigned transaction, and simulates it on Solana Devnet.
It NEVER signs, sends, broadcasts, or executes a transaction.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

import requests
from solders.pubkey import Pubkey

DEVNET_RPC_URL = "https://api.devnet.solana.com"
DEVNET_GENESIS_HASH = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
RAYDIUM_DEVNET_API = "https://api-v3-devnet.raydium.io"
RAYDIUM_DEVNET_TRADE_API = "https://transaction-v1-devnet.raydium.io"
SOL_MINT = "So11111111111111111111111111111111111111112"
DEVNET_USDC_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
DEFAULT_WALLET = "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"
DEFAULT_AMOUNT_LAMPORTS = 10_000_000
MAX_AMOUNT_LAMPORTS = 50_000_000
ALLOWED_RAYDIUM_PROGRAMS = {
    "DRaya7Kj3aMWQSy19kSjvmuwq9docCHofyP9kanQGaav",
    "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb",
    "DRayAUgENGQBKVaX8owNhgzkEDyoHTGVEGHVJT1E9pfH",
    "DRayDdXc1NZQ9C3hRWmoSf8zK4iapgMnjdNZWrfwsP8m",
    "DRaybByLpbUL57LJARs3j8BitTxVfzBg351EaMr5UTCd",
}


class DevnetSwapPreflightError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DevnetSwapEvidence:
    evidence_type: str
    status: str
    cluster: str
    genesis_hash: str
    wallet_address: str
    input_mint: str
    output_mint: str
    input_amount_lamports: int
    quoted_output_amount_raw: str | None
    minimum_output_amount_raw: str | None
    price_impact_pct: float | None
    route_pool_ids: list[str]
    verified_pool_program_owners: dict[str, str]
    unsigned_transaction_count: int
    simulation_err: Any
    simulation_logs: list[str]
    rpc_endpoint: str
    raydium_api: str
    quote_received: bool
    unsigned_transaction_built: bool
    simulation_attempted: bool
    simulation_succeeded: bool
    transaction_signed: bool = False
    transaction_submitted: bool = False
    transaction_broadcast: bool = False
    swap_executed: bool = False
    live_trading: bool = False


class RaydiumDevnetSwapPreflight:
    def __init__(self, *, session: Any | None = None, timeout_seconds: float = 20.0) -> None:
        if not 0 < timeout_seconds <= 30:
            raise DevnetSwapPreflightError("timeout_seconds must be >0 and <=30")
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise DevnetSwapPreflightError("Provider returned a non-object JSON response")
        return payload

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(url, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise DevnetSwapPreflightError("Provider returned a non-object JSON response")
        return result

    def _rpc(self, method: str, params: list[Any] | None = None) -> Any:
        payload = self._post_json(
            DEVNET_RPC_URL,
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
        )
        if payload.get("error") is not None or "result" not in payload:
            raise DevnetSwapPreflightError(f"Devnet RPC {method} failed")
        return payload["result"]

    def run(self, wallet_address: str, amount_lamports: int = DEFAULT_AMOUNT_LAMPORTS) -> DevnetSwapEvidence:
        try:
            wallet = str(Pubkey.from_string(wallet_address))
        except Exception as exc:
            raise DevnetSwapPreflightError("wallet_address is not a valid Solana public key") from exc
        if isinstance(amount_lamports, bool) or not isinstance(amount_lamports, int):
            raise DevnetSwapPreflightError("amount_lamports must be an integer")
        if not 1_000_000 <= amount_lamports <= MAX_AMOUNT_LAMPORTS:
            raise DevnetSwapPreflightError("test amount must be between 0.001 and 0.05 Devnet SOL")

        genesis = self._rpc("getGenesisHash")
        if genesis != DEVNET_GENESIS_HASH:
            raise DevnetSwapPreflightError("RPC genesis hash does not identify Solana Devnet")
        balance_result = self._rpc("getBalance", [wallet, {"commitment": "confirmed"}])
        balance = balance_result.get("value") if isinstance(balance_result, dict) else None
        # Keep a 0.002 SOL reserve for fees and account setup.
        if not isinstance(balance, int) or isinstance(balance, bool) or balance < amount_lamports + 2_000_000:
            raise DevnetSwapPreflightError("Devnet wallet balance is insufficient for test amount plus 0.002 SOL reserve")

        # Verify the Devnet USDC mint exists on this cluster before using it.
        mint_info = self._rpc("getAccountInfo", [DEVNET_USDC_MINT, {"encoding": "base64", "commitment": "confirmed"}])
        mint_value = mint_info.get("value") if isinstance(mint_info, dict) else None
        if not isinstance(mint_value, dict) or not mint_value.get("owner") or not mint_value.get("data"):
            raise DevnetSwapPreflightError("Configured Devnet USDC mint is not present on Devnet")

        quote = self._get_json(
            f"{RAYDIUM_DEVNET_TRADE_API}/compute/swap-base-in",
            {
                "inputMint": SOL_MINT,
                "outputMint": DEVNET_USDC_MINT,
                "amount": str(amount_lamports),
                "slippageBps": "100",
                "txVersion": "V0",
            },
        )
        if quote.get("success") is not True or not isinstance(quote.get("data"), dict):
            raise DevnetSwapPreflightError(f"Raydium Devnet quote unavailable: {quote.get('msg', 'no route returned')}")
        qdata = quote["data"]
        if qdata.get("inputMint") != SOL_MINT or qdata.get("outputMint") != DEVNET_USDC_MINT:
            raise DevnetSwapPreflightError("Quote token pair does not match requested Devnet SOL/USDC pair")
        route = qdata.get("routePlan")
        if not isinstance(route, list) or not route:
            raise DevnetSwapPreflightError("Quote contains no executable liquidity route")
        pool_ids = sorted({str(item.get("poolId")) for item in route if isinstance(item, dict) and item.get("poolId")})
        if not pool_ids:
            raise DevnetSwapPreflightError("Quote route contains no pool identifiers")

        # Resolve pool metadata and independently check each pool account owner on Devnet.
        pool_payload = self._get_json(
            f"{RAYDIUM_DEVNET_API}/pools/info/ids",
            {"ids": ",".join(pool_ids)},
        )
        if pool_payload.get("success") is not True:
            raise DevnetSwapPreflightError("Raydium Devnet pool metadata lookup failed")
        pool_rows = pool_payload.get("data")
        if isinstance(pool_rows, dict):
            pool_rows = pool_rows.get("data", [])
        if not isinstance(pool_rows, list):
            raise DevnetSwapPreflightError("Raydium Devnet pool metadata has an unexpected shape")
        returned_ids = {str(row.get("id") or row.get("poolId")) for row in pool_rows if isinstance(row, dict)}
        if not set(pool_ids).issubset(returned_ids):
            raise DevnetSwapPreflightError("One or more quoted pools could not be resolved by the Devnet pool API")

        verified_owners: dict[str, str] = {}
        for pool_id in pool_ids:
            account = self._rpc("getAccountInfo", [pool_id, {"encoding": "base64", "commitment": "confirmed"}])
            value = account.get("value") if isinstance(account, dict) else None
            owner = value.get("owner") if isinstance(value, dict) else None
            if owner not in ALLOWED_RAYDIUM_PROGRAMS:
                raise DevnetSwapPreflightError(f"Pool {pool_id} owner is not an approved Raydium Devnet program")
            verified_owners[pool_id] = owner

        built = self._post_json(
            f"{RAYDIUM_DEVNET_TRADE_API}/transaction/swap-base-in",
            {
                "computeUnitPriceMicroLamports": "50000",
                "swapResponse": qdata,
                "txVersion": "V0",
                "wallet": wallet,
                "wrapSol": True,
                "unwrapSol": False,
            },
        )
        if built.get("success") is not True or not isinstance(built.get("data"), list) or not built["data"]:
            raise DevnetSwapPreflightError(f"Raydium Devnet unsigned transaction build failed: {built.get('msg', 'no transaction')}")
        txs = built["data"]
        if len(txs) > 3:
            raise DevnetSwapPreflightError("Unexpected transaction bundle size; refusing simulation")
        logs: list[str] = []
        simulation_err: Any = None
        for item in txs:
            encoded = item.get("transaction") if isinstance(item, dict) else None
            if not isinstance(encoded, str):
                raise DevnetSwapPreflightError("Unsigned transaction response is malformed")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise DevnetSwapPreflightError("Unsigned transaction is not valid base64") from exc
            sim = self._rpc(
                "simulateTransaction",
                [base64.b64encode(raw).decode("ascii"), {
                    "encoding": "base64",
                    "commitment": "confirmed",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "innerInstructions": False,
                }],
            )
            value = sim.get("value") if isinstance(sim, dict) else None
            if not isinstance(value, dict):
                raise DevnetSwapPreflightError("Devnet simulation returned an invalid response")
            simulation_err = value.get("err")
            logs.extend(value.get("logs") or [])
            if simulation_err is not None:
                break

        return DevnetSwapEvidence(
            evidence_type="raydium_devnet_swap_preflight",
            status="simulation_succeeded" if simulation_err is None else "simulation_failed",
            cluster="devnet",
            genesis_hash=genesis,
            wallet_address=wallet,
            input_mint=SOL_MINT,
            output_mint=DEVNET_USDC_MINT,
            input_amount_lamports=amount_lamports,
            quoted_output_amount_raw=str(qdata.get("outputAmount")) if qdata.get("outputAmount") is not None else None,
            minimum_output_amount_raw=str(qdata.get("otherAmountThreshold")) if qdata.get("otherAmountThreshold") is not None else None,
            price_impact_pct=float(qdata["priceImpactPct"]) if qdata.get("priceImpactPct") is not None else None,
            route_pool_ids=pool_ids,
            verified_pool_program_owners=verified_owners,
            unsigned_transaction_count=len(txs),
            simulation_err=simulation_err,
            simulation_logs=logs[-80:],
            rpc_endpoint=DEVNET_RPC_URL,
            raydium_api=RAYDIUM_DEVNET_TRADE_API,
            quote_received=True,
            unsigned_transaction_built=True,
            simulation_attempted=True,
            simulation_succeeded=simulation_err is None,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and simulate a Raydium Devnet swap without signing or sending")
    parser.add_argument("--wallet", required=True, help="Public wallet address only")
    parser.add_argument("--amount-lamports", type=int, default=DEFAULT_AMOUNT_LAMPORTS)
    parser.add_argument("--output", default="artifacts/raydium-devnet-swap-preflight.json")
    args = parser.parse_args()
    try:
        evidence = RaydiumDevnetSwapPreflight().run(args.wallet, args.amount_lamports)
        payload = asdict(evidence)
        code = 0 if evidence.simulation_succeeded else 1
    except Exception as exc:
        payload = {
            "evidence_type": "raydium_devnet_swap_preflight",
            "status": "failed",
            "cluster": "devnet",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "transaction_signed": False,
            "transaction_submitted": False,
            "transaction_broadcast": False,
            "swap_executed": False,
            "live_trading": False,
        }
        code = 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
