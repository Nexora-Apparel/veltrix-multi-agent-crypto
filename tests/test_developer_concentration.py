from veltrix.developer_concentration import (
    DeveloperConcentrationAnalyzer, FundingGraphSnapshot, FundingRelationship,
    HolderBalance, HolderSnapshot,
)


NOW = 2_000_000
TOKEN = "mint"


def inputs(*, chain="solana", holders=None, relationships=None, covered=None,
           holder_complete=True, funding_complete=True, holder_provider="holder-a",
           funding_provider="funding-b", holder_time=NOW - 100,
           funding_time=NOW - 100, supply=1000):
    holders = holders or (
        HolderBalance("dev", 100),
        HolderBalance("wallet-linked", 200),
        HolderBalance("wallet-clean", 700),
    )
    relationships = relationships or (
        FundingRelationship("wallet-linked", "dev", "funding-tx-1", NOW - 200, "edge-1"),
        FundingRelationship("wallet-clean", None, None, NOW - 200, "edge-2"),
    )
    covered = covered or tuple(x.wallet_address for x in holders)
    holder = HolderSnapshot(
        holder_provider, chain, TOKEN, holder_time, "holders-e1", "block:100",
        6, supply, tuple(holders), holder_complete,
    )
    funding = FundingGraphSnapshot(
        funding_provider, chain, TOKEN, funding_time, "funding-e1", "block:100",
        0, NOW - 50, tuple(covered), tuple(relationships), funding_complete,
    )
    return holder, funding


def test_complete_holder_and_funding_graph_calculates_developer_linked_supply():
    holder, funding = inputs()
    report = DeveloperConcentrationAnalyzer().analyze(
        "solana", TOKEN, holder, funding, ["dev"],
        root_provider="launch-origin", root_evidence_id="origin-e1",
        now_ms=NOW, certified_provider_id="dev-graph",
        independence_group="graph-vendor",
    )
    assert report.coverage_complete
    assert report.linked_wallets == ("dev", "wallet-linked")
    assert report.linked_supply_raw == 300
    assert report.total_supply_raw == 1000
    assert report.dev_hold_pct == 30.0
    evidence = report.to_safety_evidence()
    assert evidence is not None
    assert evidence.claim == "developer_holdings"
    assert evidence.coverage_complete is True


def test_common_funder_does_not_make_wallets_developer_linked():
    holder, funding = inputs(relationships=(
        FundingRelationship("wallet-linked", "shared-exchange", "tx-1", NOW - 200, "e1"),
        FundingRelationship("wallet-clean", "shared-exchange", "tx-2", NOW - 200, "e2"),
    ), covered=("dev", "wallet-linked", "wallet-clean"))
    # Root is not in the graph's ancestry, so shared exchange funding is not enough.
    holder = HolderSnapshot(
        holder.provider, holder.chain, holder.token_address, holder.observed_at_ms,
        holder.evidence_id, holder.block_ref, holder.decimals, holder.total_supply_raw,
        holder.holders + (HolderBalance("shared-exchange", 0),), holder.coverage_complete,
    )
    funding = FundingGraphSnapshot(
        funding.provider, funding.chain, funding.token_address, funding.observed_at_ms,
        funding.evidence_id, funding.block_ref, funding.coverage_start_ms,
        funding.coverage_end_ms, ("dev", "wallet-linked", "wallet-clean", "shared-exchange"),
        funding.relationships + (
            FundingRelationship("shared-exchange", None, None, NOW - 200, "e3"),
        ), funding.coverage_complete,
    )
    report = DeveloperConcentrationAnalyzer().analyze(
        "solana", TOKEN, holder, funding, ["dev"],
        root_provider="launch-origin", root_evidence_id="origin-e1", now_ms=NOW,
    )
    assert report.coverage_complete
    assert report.linked_wallets == ()


def test_incomplete_holder_or_funding_coverage_withholds_percentage():
    holder, funding = inputs(holder_complete=False)
    report = DeveloperConcentrationAnalyzer().analyze(
        "solana", TOKEN, holder, funding, ["dev"],
        root_provider="launch-origin", root_evidence_id="origin-e1", now_ms=NOW,
    )
    assert not report.coverage_complete
    assert report.dev_hold_pct is None
    assert report.to_safety_evidence() is None


def test_holder_balances_must_reconcile_to_total_supply():
    holder, funding = inputs(supply=999)
    report = DeveloperConcentrationAnalyzer().analyze(
        "solana", TOKEN, holder, funding, ["dev"],
        root_provider="launch-origin", root_evidence_id="origin-e1", now_ms=NOW,
    )
    assert not report.coverage_complete
    assert "holder balances do not reconcile exactly to total supply" in report.reasons


def test_every_holder_requires_explicit_funding_graph_coverage():
    holder, funding = inputs(covered=("wallet-linked", "wallet-clean"))
    report = DeveloperConcentrationAnalyzer().analyze(
        "solana", TOKEN, holder, funding, ["dev"],
        root_provider="launch-origin", root_evidence_id="origin-e1", now_ms=NOW,
    )
    assert not report.coverage_complete
    assert any("every token holder" in reason for reason in report.reasons)


def test_stale_evidence_and_shared_provider_sources_fail_closed():
    holder, funding = inputs(holder_time=NOW - 400_000)
    stale = DeveloperConcentrationAnalyzer().analyze(
        "solana", TOKEN, holder, funding, ["dev"],
        root_provider="launch-origin", root_evidence_id="origin-e1", now_ms=NOW,
    )
    assert not stale.coverage_complete
    holder, funding = inputs(funding_provider="holder-a")
    shared = DeveloperConcentrationAnalyzer().analyze(
        "solana", TOKEN, holder, funding, ["dev"],
        root_provider="launch-origin", root_evidence_id="origin-e1", now_ms=NOW,
    )
    assert not shared.coverage_complete
    assert any("independently identified" in reason for reason in shared.reasons)


def test_evm_address_matching_is_case_insensitive():
    holder, funding = inputs(
        chain="base",
        holders=(HolderBalance("0xAbC", 1000),),
        relationships=(FundingRelationship("0xabc", "0xDEV", "tx", NOW - 100, "edge"),),
        covered=("0xABC",),
        supply=1000,
    )
    report = DeveloperConcentrationAnalyzer().analyze(
        "base", TOKEN, holder, funding, ["0xdev"],
        root_provider="origin", root_evidence_id="origin-e1", now_ms=NOW,
    )
    assert report.coverage_complete
    assert report.dev_hold_pct == 100.0
