"""Read-only live acceptance for the developer holder/funding evidence graph.

This checks provider responses and graph completeness. It never adds provider
trust, certifies a capability, submits transactions or opens paper positions.
"""
import argparse
import json
import time
from pathlib import Path

from .config import Settings
from .developer_concentration import DeveloperConcentrationAnalyzer
from .providers.factory import (
    build_funding_graph_providers,
    build_holder_inventory_providers,
)
from .launch_origin import build_launch_origin_resolvers
from .providers.signed_roots import SignedDeveloperRootProvider


def run_developer_acceptance(*, chain, token_address, settings,
                             holder_providers, funding_providers,
                             root_provider, origin_resolvers=None,
                             now_ms=None):
    chain = str(chain).lower()
    if chain not in {"solana", "base", "bsc"}:
        raise ValueError("Unsupported developer acceptance chain")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    holder_provider = holder_providers.get(chain)
    funding_provider = funding_providers.get(chain)
    if holder_provider is None or funding_provider is None:
        raise ValueError("Required holder or funding provider is not configured")
    root = root_provider.fetch_attestation(chain, token_address)
    same_token = (
        root.token_address.lower() == token_address.lower()
        if chain in {"base", "bsc"} and isinstance(root.token_address, str)
        else root.token_address == token_address
    )
    if (root.chain != chain or not same_token or root.signature_verified is not True
            or root.coverage_complete is not True or not root.roots or not root.evidence_id):
        raise ValueError("Developer-root attestation is not verified, complete and token-bound")

    deployment_block = None
    origin_provider_id = None
    origin_evidence_id = None
    origin_provider_trusted = None
    if chain in {"base", "bsc"}:
        resolver = (origin_resolvers or {}).get(chain)
        if resolver is None:
            raise ValueError("EVM deployment-block resolver is not configured")
        origin = resolver.resolve(token_address, settings.launch_window_ms)
        if not origin.complete or origin.chain != chain or origin.launch_block is None:
            raise ValueError("EVM token deployment block is not verified")
        deployment_block = origin.launch_block
        origin_provider_id = origin.provider
        origin_evidence_id = origin.evidence_id
        origin_provider_trusted = origin.provider in frozenset(
            settings.trusted_launch_origin_providers
        )
        holder = holder_provider.fetch_snapshot(token_address, deployment_block)
    else:
        holder = holder_provider.fetch_snapshot(token_address)

    funding = funding_provider.fetch_snapshot(
        token_address, [row.wallet_address for row in holder.holders]
    )
    report = DeveloperConcentrationAnalyzer(
        max_age_ms=settings.safety_evidence_max_age_ms
    ).analyze(
        chain, token_address, holder, funding, root.roots,
        root_provider=root.provider, root_evidence_id=root.evidence_id,
        now_ms=now_ms, certified_provider_id=None,
    )
    trusted_roots = frozenset(settings.trusted_developer_root_providers)
    trusted_holders = frozenset(settings.trusted_holder_inventory_providers)
    trusted_funding = frozenset(settings.trusted_developer_funding_providers)
    trust = {
        "root_provider_trusted": root.provider in trusted_roots,
        "holder_provider_trusted": holder.provider in trusted_holders,
        "funding_provider_trusted": funding.provider in trusted_funding,
        "developer_claim_provider_trusted": (
            "developer-graph-provider" in frozenset(settings.trusted_developer_providers)
        ),
        "launch_origin_provider_trusted": origin_provider_trusted if chain in {"base", "bsc"} else True,
    }
    trust_ready = all(trust.values())
    return {
        "chain": chain,
        "token_address": token_address,
        "status": "structural_pass" if report.coverage_complete else "blocked",
        "structural_pass": bool(report.coverage_complete),
        "trust_ready": trust_ready,
        "capability_certified": bool(
            getattr(settings, "developer_evidence_capability_certified", False)
            and report.coverage_complete and trust_ready
        ),
        "live_execution_enabled": False,
        "observed_at_ms": now_ms,
        "deployment_block": deployment_block,
        "launch_origin": {
            "provider": origin_provider_id,
            "evidence_id": origin_evidence_id,
        } if origin_provider_id else None,
        "holder": {
            "provider": holder.provider,
            "evidence_id": holder.evidence_id,
            "block_ref": holder.block_ref,
            "holder_count": len(holder.holders),
            "total_supply_raw": holder.total_supply_raw,
            "coverage_complete": holder.coverage_complete,
        },
        "funding": {
            "provider": funding.provider,
            "evidence_id": funding.evidence_id,
            "block_ref": funding.block_ref,
            "covered_wallet_count": len(funding.covered_wallets),
            "relationship_count": len(funding.relationships),
            "coverage_complete": funding.coverage_complete,
        },
        "developer_roots": {
            "provider": root.provider,
            "evidence_id": root.evidence_id,
            "source_ref": root.source_ref,
            "signature_verified": root.signature_verified,
            "signing_key_fingerprint": root.signing_key_fingerprint,
            "root_count": len(root.roots),
            "coverage_complete": root.coverage_complete,
        },
        "concentration": {
            "coverage_complete": report.coverage_complete,
            "developer_hold_pct": report.dev_hold_pct,
            "linked_wallet_count": len(report.linked_wallets),
            "linked_supply_raw": report.linked_supply_raw,
            "total_supply_raw": report.total_supply_raw,
            "reasons": list(report.reasons),
        },
        "trust": trust,
        "notes": [
            "Structural acceptance is not provider capability certification.",
            "Provider trust and root-identity methodology require independent review.",
            "No transactions were submitted and no trading execution was enabled.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="VELTRIX read-only developer evidence acceptance")
    parser.add_argument("--chain", choices=("solana", "base", "bsc"), required=True)
    parser.add_argument("--token-address", required=True)
    parser.add_argument("--output", default="developer-acceptance.json")
    args = parser.parse_args(argv)
    cfg = Settings()
    result = None
    exit_code = 1
    try:
        holders = build_holder_inventory_providers(cfg)
        funding = build_funding_graph_providers(cfg)
        if not cfg.developer_root_attestation_url or not cfg.developer_root_attestation_public_key:
            raise ValueError("Signed developer-root endpoint and public key are required")
        roots = SignedDeveloperRootProvider(
            cfg.developer_root_attestation_url,
            cfg.developer_root_attestation_public_key,
            provider_id=cfg.developer_root_attestation_provider_id,
            max_age_ms=cfg.safety_evidence_max_age_ms,
        )
        origins = build_launch_origin_resolvers(cfg)
        result = run_developer_acceptance(
            chain=args.chain, token_address=args.token_address, settings=cfg,
            holder_providers=holders, funding_providers=funding,
            root_provider=roots, origin_resolvers=origins,
        )
        exit_code = 0 if result["structural_pass"] and result["trust_ready"] else 1
    except Exception as exc:
        result = {
            "chain": args.chain,
            "token_address": args.token_address,
            "status": "blocked",
            "structural_pass": False,
            "capability_certified": False,
            "live_execution_enabled": False,
            "error_type": type(exc).__name__,
            "notes": ["Provider failure details are withheld to avoid exposing credential-bearing request metadata."],
        }
    Path(args.output).write_text(
        json.dumps(result, sort_keys=True, indent=2), encoding="utf-8"
    )
    print(
        f"Developer evidence acceptance: {result['status']}; "
        f"trust ready: {result.get('trust_ready', False)}; "
        f"capability certified: {result.get('capability_certified', False)}; "
        f"execution enabled: {result.get('live_execution_enabled', False)}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
