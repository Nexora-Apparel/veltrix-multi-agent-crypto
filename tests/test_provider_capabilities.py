from veltrix.providers.capabilities import (
    CAPABILITIES, capability_can_certify, capability_for,
)


def test_no_current_candidate_provider_is_misrepresented_as_complete():
    assert CAPABILITIES
    assert not any(item.can_certify for item in CAPABILITIES)


def test_goplus_locker_api_is_candidate_only_for_base_and_bsc():
    for chain in ("base", "bsc"):
        item = capability_for("goplus", chain, "liquidity_lock")
        assert item is not None
        assert item.status == "partial"
        assert not capability_can_certify("goplus", chain, "liquidity_lock")
        assert "response schema" in item.certification_blocker


def test_insightx_clusters_are_not_complete_developer_holdings_proof():
    for chain in ("solana", "base", "bsc"):
        item = capability_for("insightx-clusters", chain, "developer_holdings")
        assert item is not None
        assert item.status == "partial"
        assert not item.can_certify
        assert "exhaustive holder coverage" in item.certification_blocker


def test_unsupported_provider_chain_claim_combination_is_unknown():
    assert capability_for("goplus", "solana", "liquidity_lock").status == "unsupported"
    assert not capability_can_certify("goplus", "solana", "liquidity_lock")
    assert capability_for("unknown", "base", "developer_holdings") is None
