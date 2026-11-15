from veltrix.stage5_certification import evaluate_stage5_artifacts, SOLANA_REQUIRED_LOCKERS


def liquidity(chain, lockers):
    return {
        "chain": chain,
        "reader_acceptance": "PASS",
        "capability_certified": False,
        "live_execution_enabled": False,
        "locker_position_count": len(lockers),
        "locker_position_programs": lockers,
        "locker_deployments": [{"address": item} for item in lockers],
        "evidence_id": f"{chain}-liquidity-proof",
    }


def developer(chain):
    return {
        "chain": chain,
        "structural_pass": True,
        "trust_ready": True,
        "capability_certified": False,
        "live_execution_enabled": False,
        "holder": {"evidence_id": f"{chain}-holder-proof"},
        "funding": {"evidence_id": f"{chain}-funding-proof"},
        "developer_roots": {"evidence_id": f"{chain}-root-proof"},
    }


def launch_bundle(chain):
    return {
        "chain": chain,
        "reader_acceptance": "PASS",
        "capability_certified": False,
        "live_execution_enabled": False,
        "launch_origin": {"trusted": True, "evidence_id": f"{chain}-origin-proof"},
        "launch_index": {
            "coverage_complete": True,
            "supply_block_ref": "finalized-block:123",
            "evidence_id": f"{chain}-bundle-proof",
        },
        "bundle": {"rejected": []},
    }


def complete_artifacts():
    liquidity_rows = {
        "solana": liquidity("solana", sorted(SOLANA_REQUIRED_LOCKERS)),
        "base": liquidity("base", ["0x" + "11" * 20]),
        "bsc": liquidity("bsc", ["0x" + "22" * 20]),
    }
    return (
        liquidity_rows,
        {chain: developer(chain) for chain in ("solana", "base", "bsc")},
        {chain: launch_bundle(chain) for chain in ("solana", "base", "bsc")},
    )


def test_stage5_gate_requires_all_nine_claim_artifacts_and_never_self_certifies():
    liquidity_rows, developer_rows, bundle_rows = complete_artifacts()
    report = evaluate_stage5_artifacts(
        liquidity=liquidity_rows, developer=developer_rows,
        launch_bundle=bundle_rows,
    )
    assert report["status"] == "EVIDENCE_COMPLETE"
    assert report["evidence_complete"] is True
    assert report["capability_certified"] is False
    assert report["manual_independent_review_required"] is True
    assert report["live_execution_enabled"] is False
    assert len(report["evidence_ids"]) == 3


def test_stage5_gate_blocks_missing_chain_artifact():
    liquidity_rows, developer_rows, bundle_rows = complete_artifacts()
    del developer_rows["bsc"]
    report = evaluate_stage5_artifacts(
        liquidity=liquidity_rows, developer=developer_rows,
        launch_bundle=bundle_rows,
    )
    assert report["evidence_complete"] is False
    assert any("bsc: developer acceptance artifact is missing" in x
               for x in report["blockers"])


def test_stage5_gate_requires_both_solana_locker_profiles():
    liquidity_rows, developer_rows, bundle_rows = complete_artifacts()
    liquidity_rows["solana"] = liquidity(
        "solana", ["GsSCS3vPWrtJ5Y9aEVVT65fmrex5P5RGHXdZvsdbWgfo"]
    )
    report = evaluate_stage5_artifacts(
        liquidity=liquidity_rows, developer=developer_rows,
        launch_bundle=bundle_rows,
    )
    assert any("both UNCX V4 and Raydium Burn & Earn CPMM" in x
               for x in report["blockers"])


def test_stage5_gate_blocks_untrusted_developer_and_incomplete_launch_bundle():
    liquidity_rows, developer_rows, bundle_rows = complete_artifacts()
    developer_rows["base"]["trust_ready"] = False
    bundle_rows["bsc"]["launch_index"]["coverage_complete"] = False
    report = evaluate_stage5_artifacts(
        liquidity=liquidity_rows, developer=developer_rows,
        launch_bundle=bundle_rows,
    )
    assert any("base: developer evidence provider trust is incomplete" in x
               for x in report["blockers"])
    assert any("bsc: launch-window or historical-supply coverage is incomplete" in x
               for x in report["blockers"])


def test_stage5_gate_rejects_any_artifact_that_claims_execution_enabled():
    liquidity_rows, developer_rows, bundle_rows = complete_artifacts()
    bundle_rows["base"]["live_execution_enabled"] = True
    report = evaluate_stage5_artifacts(
        liquidity=liquidity_rows, developer=developer_rows,
        launch_bundle=bundle_rows,
    )
    assert any("base: launch_bundle artifact does not explicitly attest execution disabled" in x
               for x in report["blockers"])



def test_stage5_gate_requires_explicit_execution_disabled_attestation():
    liquidity_rows, developer_rows, bundle_rows = complete_artifacts()
    del liquidity_rows["solana"]["live_execution_enabled"]
    report = evaluate_stage5_artifacts(
        liquidity=liquidity_rows, developer=developer_rows,
        launch_bundle=bundle_rows,
    )
    assert any(
        "solana: liquidity artifact does not explicitly attest execution disabled" in x
        for x in report["blockers"]
    )
