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


def test_goplus_does_not_configure_a_complete_chain_specific_locker_reader():
    report = build_provider_readiness(settings())
    rows = {(row.chain, row.claim): row for row in report.claims}
    assert not rows[("solana", "liquidity_lock")].configured
    assert not rows[("base", "liquidity_lock")].configured
    assert not rows[("bsc", "liquidity_lock")].configured
    assert not rows[("base", "liquidity_lock")].capability_complete


def test_developer_readiness_reports_signed_root_and_chain_provider_blockers():
    report = build_provider_readiness(settings(
        solana_rpc_url="https://solana.example",
        helius_api_key="configured",
        base_rpc_url="https://base.example",
        alchemy_api_key="configured",
        bsc_rpc_url="https://bsc.example",
        trusted_developer_root_providers=(),
        trusted_holder_inventory_providers=(),
        trusted_developer_funding_providers=(),
        developer_root_attestation_url="",
        developer_root_attestation_public_key="",
        developer_root_attestation_provider_id="signed-developer-roots",
    ))
    rows = {row.chain: row for row in report.claims if row.claim == "developer_holdings"}
    assert any("Ed25519 public key is missing" in x for x in rows["solana"].blockers)
    assert any("solana-rpc-holder-inventory is not explicitly trusted" in x
               for x in rows["solana"].blockers)
    assert any("helius-solana-funding-graph is not explicitly trusted" in x
               for x in rows["solana"].blockers)
    assert any("base-rpc-holder-inventory is not explicitly trusted" in x
               for x in rows["base"].blockers)
    assert any("bsc complete first-funder ancestry provider is unavailable" in x
               for x in rows["bsc"].blockers)
    assert all(not row.capability_complete for row in rows.values())


def test_liquidity_readiness_names_missing_chain_specific_snapshot_readers():
    report = build_provider_readiness(settings())
    rows = {row.chain: row for row in report.claims if row.claim == "liquidity_lock"}
    assert all(any("RPC endpoints" in item and "separate" in item
                   for item in row.blockers) for row in rows.values())
    assert any("runtime hash" in item for item in rows["base"].blockers)
    assert any("deployed executable" in item for item in rows["solana"].blockers)


def test_configured_liquidity_reader_still_requires_live_acceptance_and_explicit_trust():
    report = build_provider_readiness(settings(
        base_liquidity_pool_rpc_url="https://base-pool.example",
        base_liquidity_locker_rpc_url="https://base-locker.example",
        base_liquidity_factories_json='[{"amm_id":"uniswap-v2"}]',
        base_liquidity_lockers_json='[{"amm_id":"uniswap-v2"}]',
        base_liquidity_registry_complete=True,
        trusted_liquidity_providers=("base-finalized-all-factory-liquidity",),
    ))
    row = next(row for row in report.claims
               if row.chain == "base" and row.claim == "liquidity_lock")
    assert row.configured and row.trusted
    assert not row.capability_complete
    assert any("live read-only acceptance" in item for item in row.blockers)


def test_solana_liquidity_reader_requires_pinned_raydium_executable_hash():
    report = build_provider_readiness(settings(
        solana_liquidity_pool_rpc_url="https://solana-pool.example",
        solana_liquidity_locker_rpc_url="https://solana-locker.example",
    ))
    row = next(row for row in report.claims
               if row.chain == "solana" and row.claim == "liquidity_lock")
    assert not row.configured
    assert any("Raydium AMM V4 executable SHA-256 hash is missing" in item
               for item in row.blockers)
