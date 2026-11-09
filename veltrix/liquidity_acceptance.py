"""Manual, read-only acceptance probe for a configured chain liquidity reader."""
import argparse
import json

from .config import settings
from .liquidity_evidence import LiquidityEvidenceAnalyzer
from .providers.factory import build_liquidity_snapshot_providers


LOCK_RATIO_ONLY = "at least one discovered pool lacks the required active locked LP share"


def run(chain, token_address):
    cfg = settings()
    providers = build_liquidity_snapshot_providers(cfg, chains=(chain,))
    provider = providers.get(chain)
    if provider is None:
        raise RuntimeError(f"No fully configured {chain} liquidity snapshot reader is available")

    batch = provider.fetch_inventory(token_address)
    report = LiquidityEvidenceAnalyzer(
        max_age_ms=cfg.safety_evidence_max_age_ms
    ).evaluate(batch, chain=chain, token_address=token_address, now_ms=batch.observed_at_ms)

    structural_reasons = [
        reason for reason in report.reasons if reason != LOCK_RATIO_ONLY
    ]
    accepted = (
        batch.inventory_complete is True
        and bool(batch.pools)
        and report.verified_pool_count == report.pool_count
        and not structural_reasons
    )
    result = {
        "reader_acceptance": "PASS" if accepted else "BLOCKED",
        "safety_gate": "PASS" if report.passed else "BLOCKED",
        "chain": chain,
        "token_address": token_address,
        "finalized_ref": batch.finalized_ref,
        "pool_set_id": batch.pool_set_id,
        "pool_count": report.pool_count,
        "verified_pool_count": report.verified_pool_count,
        "actively_locked_pool_count": report.active_locked_pool_count,
        "minimum_per_pool_locked_pct": report.active_locked_pct,
        "earliest_unlock_at_ms": report.earliest_unlock_at_ms,
        "evidence_id": report.evidence_id,
        "structural_blockers": structural_reasons,
        "safety_blockers": list(report.reasons),
        "pools": [
            {
                "pool": pool.pool_address,
                "lp_mint": pool.lp_token_address,
                "lp_total_supply": pool.total_lp_supply,
                "locker_custody": pool.locker_custody_amount,
                "finalized_ref": pool.finalized_ref,
            }
            for pool in batch.pools
        ],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if not accepted:
        raise RuntimeError("read-only liquidity reader acceptance failed")
    return result


def main():
    parser = argparse.ArgumentParser(description="VELTRIX read-only liquidity acceptance")
    parser.add_argument("--chain", required=True, choices=("base", "bsc", "solana"))
    parser.add_argument("--token-address", required=True)
    args = parser.parse_args()
    run(args.chain, args.token_address)


if __name__ == "__main__":
    main()
