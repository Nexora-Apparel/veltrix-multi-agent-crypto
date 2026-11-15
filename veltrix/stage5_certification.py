"""Aggregate the nine read-only Stage 5 acceptance artifacts.

This tool verifies evidence completeness only. It never changes trust allowlists,
capability flags, execution settings or trading state.
"""
import argparse
import json
from pathlib import Path

CHAINS = ("solana", "base", "bsc")
SOLANA_REQUIRED_LOCKERS = {
    "GsSCS3vPWrtJ5Y9aEVVT65fmrex5P5RGHXdZvsdbWgfo",
    "LockrWmn6K5twhz3y9w1dQERbmgSaRkfnTeTKbpofwE",
}


def _address_set(values, chain):
    values = values or ()
    normalize = str.lower if chain in {"base", "bsc"} else (lambda value: value)
    return {normalize(value) for value in values if isinstance(value, str)}


def evaluate_stage5_artifacts(*, liquidity, developer, launch_bundle):
    blockers = []
    evidence = {}
    for chain in CHAINS:
        liq = liquidity.get(chain)
        dev = developer.get(chain)
        bundle = launch_bundle.get(chain)
        if not isinstance(liq, dict):
            blockers.append(f"{chain}: liquidity acceptance artifact is missing")
            continue
        if not isinstance(dev, dict):
            blockers.append(f"{chain}: developer acceptance artifact is missing")
            continue
        if not isinstance(bundle, dict):
            blockers.append(f"{chain}: launch-bundle acceptance artifact is missing")
            continue

        for name, artifact in (
            ("liquidity", liq), ("developer", dev), ("launch_bundle", bundle)
        ):
            if artifact.get("chain") != chain:
                blockers.append(f"{chain}: {name} artifact chain identity mismatch")
            if artifact.get("live_execution_enabled") is True:
                blockers.append(f"{chain}: {name} artifact unexpectedly reports execution enabled")

        if liq.get("reader_acceptance") != "PASS":
            blockers.append(f"{chain}: liquidity reader acceptance did not pass")
        if (not isinstance(liq.get("locker_position_count"), int)
                or isinstance(liq.get("locker_position_count"), bool)
                or liq["locker_position_count"] < 1):
            blockers.append(f"{chain}: liquidity artifact contains no decoded locker positions")
        if not liq.get("evidence_id"):
            blockers.append(f"{chain}: liquidity evidence ID is missing")
        positions = _address_set(liq.get("locker_position_programs"), chain)
        deployments = _address_set(
            [row.get("address") for row in liq.get("locker_deployments", []) if isinstance(row, dict)],
            chain,
        )
        if not deployments or not deployments.issubset(positions):
            blockers.append(f"{chain}: not every configured locker deployment has a decoded live position")
        if chain == "solana" and not SOLANA_REQUIRED_LOCKERS.issubset(positions):
            blockers.append("solana: acceptance did not exercise both UNCX V4 and Raydium Burn & Earn CPMM")

        if dev.get("structural_pass") is not True:
            blockers.append(f"{chain}: developer evidence structural coverage did not pass")
        if dev.get("trust_ready") is not True:
            blockers.append(f"{chain}: developer evidence provider trust is incomplete")
        for evidence_name, section in (
            ("holder", "holder"), ("funding", "funding"),
            ("developer root", "developer_roots"),
        ):
            if not (dev.get(section) or {}).get("evidence_id"):
                blockers.append(f"{chain}: developer {evidence_name} evidence ID is missing")

        if bundle.get("reader_acceptance") != "PASS":
            blockers.append(f"{chain}: launch-bundle reader acceptance did not pass")
        origin = bundle.get("launch_origin") or {}
        index = bundle.get("launch_index") or {}
        bundle_report = bundle.get("bundle") or {}
        if origin.get("trusted") is not True or not origin.get("evidence_id"):
            blockers.append(f"{chain}: launch-origin evidence is not trusted or token-bound")
        if index.get("coverage_complete") is not True or not index.get("supply_block_ref"):
            blockers.append(f"{chain}: launch-window or historical-supply coverage is incomplete")
        if not index.get("evidence_id"):
            blockers.append(f"{chain}: launch-bundle evidence ID is missing")
        if bundle_report.get("rejected"):
            blockers.append(f"{chain}: launch-bundle evidence contains rejected observations")

        evidence[chain] = {
            "liquidity_evidence_id": liq.get("evidence_id"),
            "developer_holder_evidence_id": (dev.get("holder") or {}).get("evidence_id"),
            "developer_funding_evidence_id": (dev.get("funding") or {}).get("evidence_id"),
            "developer_root_evidence_id": (dev.get("developer_roots") or {}).get("evidence_id"),
            "launch_origin_evidence_id": origin.get("evidence_id"),
            "launch_bundle_evidence_id": index.get("evidence_id"),
        }

    return {
        "status": "EVIDENCE_COMPLETE" if not blockers else "BLOCKED",
        "evidence_complete": not blockers,
        "capability_certified": False,
        "manual_independent_review_required": True,
        "capability_flags_changed": False,
        "live_execution_enabled": False,
        "blockers": blockers,
        "evidence_ids": evidence,
    }


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aggregate VELTRIX Stage 5 acceptance artifacts")
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--output", default="stage5-certification-report.json")
    args = parser.parse_args(argv)
    root = Path(args.artifacts_dir)
    liquidity, developer, launch_bundle = {}, {}, {}
    for chain in CHAINS:
        for group, target in (
            ("liquidity", liquidity),
            ("developer", developer),
            ("launch-bundle", launch_bundle),
        ):
            path = root / group / f"{chain}.json"
            if path.exists():
                target[chain] = _load(path)
    result = evaluate_stage5_artifacts(
        liquidity=liquidity, developer=developer, launch_bundle=launch_bundle
    )
    Path(args.output).write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Stage 5 evidence: {result['status']}; "
        f"capability certified: {result['capability_certified']}; "
        f"execution enabled: {result['live_execution_enabled']}"
    )
    return 0 if result["evidence_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
