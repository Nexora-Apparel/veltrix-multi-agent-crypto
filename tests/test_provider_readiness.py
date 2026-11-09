from types import SimpleNamespace

from veltrix.provider_readiness import build_provider_readiness


def settings(**overrides):
    values = dict(
        trusted_liquidity_providers=(), trusted_developer_providers=(),
        trusted_bundle_providers=(), trusted_funding_providers=(),
        trusted_launch_origin_providers=(), goplus_access_token="",
        helius_api_key="", base_rpc_url="", bsc_rpc_url="",
        alchemy_quote_tokens_base=(), alchemy_quote_tokens_bsc=(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_readiness_is_static_and_never_claims_full_certification():
    report = build_provider_readiness(settings(
        goplus_access_token="configured", helius_api_key="configured",
        base_rpc_url="https://base.invalid", bsc_rpc_url="https://bsc.invalid",
        alchemy_quote_tokens_base=("0xquote",),
        alchemy_quote_tokens_bsc=("0xquote",),
        trusted_liquidity_providers=("goplus",),
        trusted_developer_providers=("dev-audit",),
        trusted_bundle_providers=("helius-launch", "base-rpc-launch", "bsc-rpc-launch"),
        trusted_funding_providers=("helius-funder", "base-quote-funder", "bsc-quote-funder"),
        trusted_launch_origin_providers=(
            "helius-launch-origin", "base-rpc-launch-origin", "bsc-rpc-launch-origin"
        ),
    ))
    assert len(report.claims) == 9
    assert not report.fully_ready
    assert all(not row.capability_complete for row in report.claims
               if row.claim in {"liquidity_lock", "developer_holdings"})
    assert all(not row.eligible_to_attempt for row in report.claims
               if row.claim == "developer_holdings")


def test_missing_credentials_and_trust_are_reported_separately():
    report = build_provider_readiness(settings())
    sol_bundle = next(row for row in report.claims
                      if row.chain == "solana" and row.claim == "launch_bundle")
    assert not sol_bundle.configured
    assert not sol_bundle.trusted
    assert any("indexer endpoint" in item for item in sol_bundle.blockers)
    assert any("not explicitly trusted" in item for item in sol_bundle.blockers)


def test_goplus_sol_requires_access_token_but_evm_public_endpoint_does_not():
    report = build_provider_readiness(settings())
    rows = {(row.chain, row.claim): row for row in report.claims}
    assert not rows[("solana", "liquidity_lock")].configured
    assert rows[("base", "liquidity_lock")].configured
    assert rows[("bsc", "liquidity_lock")].configured
    assert not rows[("base", "liquidity_lock")].capability_complete
