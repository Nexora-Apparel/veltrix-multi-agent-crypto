"""One-shot finalized Solana mint-account smoke check for Phase 05.

Run with:
    python -m veltrix.solana_meme.risk_smoke

Uses the public Solana mainnet RPC by default, or SOLANA_RPC_URL / HELIUS_API_KEY
when configured. It performs one read-only getAccountInfo request.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..providers.solana_mint import SolanaMintAuthorityProvider, SolanaMintAuthoritySnapshot

BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@dataclass(frozen=True)
class RiskSmokeResult:
    mint_snapshot: SolanaMintAuthoritySnapshot


def run_risk_smoke(*, rpc_url: str | None = None, provider=None) -> RiskSmokeResult:
    if provider is None:
        endpoint = rpc_url or os.getenv("SOLANA_RPC_URL") or ""
        if not endpoint and os.getenv("HELIUS_API_KEY"):
            endpoint = "https://mainnet.helius-rpc.com/?api-key=" + os.environ["HELIUS_API_KEY"]
        if not endpoint:
            endpoint = "https://api.mainnet-beta.solana.com"
        provider = SolanaMintAuthorityProvider(endpoint)
    snapshot = provider.fetch_snapshot(BONK_MINT)
    if snapshot.mint != BONK_MINT or snapshot.supply_raw <= 0:
        raise RuntimeError("finalized BONK mint snapshot failed identity/supply validation")
    return RiskSmokeResult(snapshot)


def main() -> None:
    result = run_risk_smoke()
    snapshot = result.mint_snapshot
    print("Solana finalized mint-account read: PASS")
    print(f"Mint: {snapshot.mint}")
    print(f"Token program: {snapshot.token_program_id}")
    print(f"Finalized slot: {snapshot.finalized_slot}")
    print(f"Decimals: {snapshot.decimals}")
    print(f"Mint authority active: {snapshot.mint_authority is not None}")
    print(f"Freeze authority active: {snapshot.freeze_authority is not None}")
    print(f"Token-2022 extensions: {', '.join(snapshot.extension_types) or 'none'}")
    print(f"Account data SHA-256: {snapshot.account_data_sha256}")


if __name__ == "__main__":
    main()
