"""Read-only live acceptance for launch-bundle indexing and funding provenance."""
import argparse
import hashlib
import json
import time
from pathlib import Path

from .launch_bundles import LaunchBundleAnalyzer
from .launch_origin import build_launch_origin_resolvers
from .providers.launch import build_launch_intelligence_service


def _same_token(chain, left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.lower() == right.lower() if chain in {"base", "bsc"} else left == right


def evaluate_launch_bundle_acceptance(*, chain, token_address, settings,
                                      indexers, origin_resolvers, now_ms=None):
    chain = str(chain).lower()
    if chain not in {"solana", "base", "bsc"}:
        raise ValueError("Unsupported launch-bundle acceptance chain")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    indexer = indexers.get(chain)
    resolver = origin_resolvers.get(chain)
    if indexer is None or resolver is None:
        raise ValueError("Launch indexer or launch-origin resolver is not configured")

    origin = resolver.resolve(
        token_address, settings.launch_window_ms, now_ms=now_ms
    )
    if (origin.complete is not True or origin.chain != chain
            or not _same_token(chain, origin.token_address, token_address)
            or not origin.evidence_id
            or origin.window_end_ms != origin.launch_at_ms + settings.launch_window_ms
            or origin.window_end_ms > now_ms):
        raise ValueError("Launch-origin evidence is incomplete or not token-bound")
    trusted_origins = frozenset(settings.trusted_launch_origin_providers)
    origin_trusted = origin.provider in trusted_origins
    if not origin_trusted:
        raise ValueError("Launch-origin provider is not explicitly trusted")

    launch_kwargs = {}
    if chain == "solana":
        if origin.launch_block is None:
            raise ValueError("Solana launch-origin slot is missing")
        launch_kwargs["launch_slot"] = origin.launch_block
    else:
        if origin.launch_block is None or origin.window_end_block is None:
            raise ValueError("EVM launch-origin block bounds are missing")
        launch_kwargs.update(
            launch_block=origin.launch_block,
            window_end_block=origin.window_end_block,
        )

    batch = indexer.index_launch(
        token_address, origin.launch_at_ms, origin.window_end_ms, **launch_kwargs
    )
    trusted_launch = frozenset(settings.trusted_bundle_providers)
    trusted_funding = frozenset(settings.trusted_funding_providers)
    rejected = list(batch.rejected)
    if batch.provider not in trusted_launch:
        rejected.append("launch indexer provider is not explicitly trusted")
    if batch.chain != chain or not _same_token(chain, batch.token_address, token_address):
        rejected.append("launch indexer returned a different chain or token")
    if batch.launch_at_ms != origin.launch_at_ms or batch.window_end_ms != origin.window_end_ms:
        rejected.append("launch indexer window differs from verified launch origin")
    if (not isinstance(batch.scanned_through_ms, int)
            or isinstance(batch.scanned_through_ms, bool)
            or batch.scanned_through_ms < batch.window_end_ms):
        rejected.append("launch indexer did not reach the requested window end")
    if (not isinstance(batch.pages_scanned, int)
            or isinstance(batch.pages_scanned, bool) or batch.pages_scanned < 1):
        rejected.append("launch indexer page coverage is missing or invalid")
    if batch.supply_source != batch.provider or not batch.supply_block_ref:
        rejected.append("historical launch-time supply provenance is missing")
    if (not isinstance(batch.supply_observed_at_ms, int)
            or isinstance(batch.supply_observed_at_ms, bool)
            or batch.supply_observed_at_ms > now_ms
            or now_ms - batch.supply_observed_at_ms > settings.safety_evidence_max_age_ms):
        rejected.append("historical launch-time supply evidence is stale or invalid")
    if not trusted_funding:
        rejected.append("no explicitly trusted funding-trace provider is configured")
    for item in batch.observations:
        if item.funder_provider not in trusted_funding:
            rejected.append(f"funding trace for {item.wallet_address} is not trusted")
        if not item.funder_evidence_id or not item.funder_address:
            rejected.append(f"funding trace for {item.wallet_address} is incomplete")

    effective_coverage = batch.coverage_complete is True and not rejected
    analyzer = LaunchBundleAnalyzer(
        trusted_providers=trusted_launch,
        launch_window_ms=settings.launch_window_ms,
        trusted_funder_providers=trusted_funding,
    )
    report = analyzer.analyze(
        provider=batch.provider, chain=chain, token_address=token_address,
        launch_at_ms=origin.launch_at_ms, total_supply=batch.total_supply,
        observations=batch.observations, coverage_complete=effective_coverage,
        now_ms=now_ms,
    )
    digest_material = {
        "origin_provider": origin.provider,
        "origin_evidence_id": origin.evidence_id,
        "indexer": batch.provider,
        "chain": chain,
        "token": token_address,
        "launch_at_ms": batch.launch_at_ms,
        "window_end_ms": batch.window_end_ms,
        "scanned_through_ms": batch.scanned_through_ms,
        "supply_block_ref": batch.supply_block_ref,
        "supply_total": batch.total_supply,
        "supply_observed_at_ms": batch.supply_observed_at_ms,
        "observations": [
            {
                "provider": item.provider,
                "wallet": item.wallet_address,
                "funder": item.funder_address,
                "transaction": item.transaction_id,
                "token_amount": item.token_amount,
                "observed_at_ms": item.observed_at_ms,
                "funder_provider": item.funder_provider,
                "funder_evidence_id": item.funder_evidence_id,
            }
            for item in batch.observations
        ],
        "provenance": list(batch.provenance),
        "rejected": rejected,
    }
    evidence_id = hashlib.sha256(
        json.dumps(digest_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    accepted = bool(
        effective_coverage and report.coverage_complete
        and report.bundled_supply_pct is not None
        and not report.rejected
    )
    capability_certified = bool(
        getattr(settings, "launch_bundle_evidence_capability_certified", False)
        and accepted and origin_trusted
    )
    return {
        "reader_acceptance": "PASS" if accepted else "BLOCKED",
        "capability_certified": capability_certified,
        "chain": chain,
        "token_address": token_address,
        "launch_origin": {
            "provider": origin.provider,
            "evidence_id": origin.evidence_id,
            "launch_at_ms": origin.launch_at_ms,
            "launch_block": origin.launch_block,
            "window_end_block": origin.window_end_block,
            "window_end_ms": origin.window_end_ms,
            "trusted": origin_trusted,
        },
        "launch_index": {
            "provider": batch.provider,
            "evidence_id": evidence_id,
            "coverage_complete": effective_coverage,
            "scanned_through_ms": batch.scanned_through_ms,
            "pages_scanned": batch.pages_scanned,
            "supply_source": batch.supply_source,
            "supply_block_ref": batch.supply_block_ref,
            "supply_observed_at_ms": batch.supply_observed_at_ms,
            "funder_providers": sorted({
                item.funder_provider for item in batch.observations
                if item.funder_provider
            }),
        },
        "bundle": {
            "bundled_supply_pct": report.bundled_supply_pct,
            "shared_funder_clusters": report.shared_funder_clusters,
            "participating_wallets": report.participating_wallets,
            "accepted_transactions": report.accepted_transactions,
            "rejected": list(report.rejected) + rejected,
            "notes": list(report.notes),
        },
        "live_execution_enabled": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="VELTRIX read-only launch-bundle acceptance")
    parser.add_argument("--chain", choices=("solana", "base", "bsc"), required=True)
    parser.add_argument("--token-address", required=True)
    parser.add_argument("--output", default="launch-bundle-acceptance.json")
    args = parser.parse_args(argv)
    from .config import Settings
    cfg = Settings()
    try:
        service = build_launch_intelligence_service(cfg, intelligence_adapters=None)
        origins = build_launch_origin_resolvers(cfg)
        result = evaluate_launch_bundle_acceptance(
            chain=args.chain, token_address=args.token_address, settings=cfg,
            indexers=service.indexers, origin_resolvers=origins,
        )
        exit_code = 0 if result["reader_acceptance"] == "PASS" else 1
    except Exception as exc:
        result = {
            "reader_acceptance": "BLOCKED",
            "capability_certified": False,
            "chain": args.chain,
            "token_address": args.token_address,
            "error_type": type(exc).__name__,
            "live_execution_enabled": False,
        }
        exit_code = 1
    rendered = json.dumps(result, sort_keys=True, indent=2)
    Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"Launch-bundle acceptance: {result['reader_acceptance']}; "
        f"capability certified: {result.get('capability_certified', False)}; "
        f"execution enabled: {result.get('live_execution_enabled', False)}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
