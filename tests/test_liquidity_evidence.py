from dataclasses import replace

import pytest

from veltrix.liquidity_evidence import (
    EVM_LOCKER_CANDIDATES,
    SOLANA_UNCX_PROGRAM_CANDIDATES,
    LiquidityEvidenceAnalyzer,
    LiquidityInventoryBatch,
    LockerDeployment,
    LockerPositionSnapshot,
    PoolLiquiditySnapshot,
    EvmLiquidityLockerProvider,
    SolanaLiquidityLockerProvider,
)


NOW = 2_000_000_000_000
TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"
LP = "0x3333333333333333333333333333333333333333"
LOCKER = "0xc4E637D37113192F4F1F060DaEbD7758De7F4131"
CODE_HASH = "a" * 64


def base_deployment():
    return LockerDeployment(
        chain="base", provider="base-locker-reader", address=LOCKER,
        abi_profile="uncx-v2.1-locks-nonce", expected_code_hash=CODE_HASH,
        source_ref="https://verified.example/base-locker",
        independence_group="uncx", chain_id=8453,
        observed_code_hash=CODE_HASH,
    )


def base_batch(*, second_pool=False, incomplete=False, expired=False,
               custody_mismatch=False, unsupported=False):
    deployment = base_deployment()
    p1 = PoolLiquiditySnapshot(
        chain="base", token_address=TOKEN, pool_address=POOL,
        lp_token_address=LP, total_lp_supply=1_000,
        observed_at_ms=NOW - 1_000, finalized_ref="block:100",
        inventory_provider="base-pool-indexer", inventory_complete=True,
        pool_set_id="token-pools-v1",
        locker_custody_amount=999 if custody_mismatch else 1_000,
    )
    pools = [p1]
    positions = [LockerPositionSnapshot(
        chain="base", pool_address=POOL, lp_token_address=LP,
        locker_address=("0x4444444444444444444444444444444444444444" if unsupported else LOCKER),
        position_id="lock-1", amount=1_000, unlock_at_ms=NOW - 1 if expired else NOW + 86_400_000,
        observed_at_ms=NOW - 1_000, owner="0x5555555555555555555555555555555555555555",
        evidence_ref="tx:lock-1", finalized_ref="block:100",
    )]
    if second_pool:
        pools.append(PoolLiquiditySnapshot(
            chain="base", token_address=TOKEN,
            pool_address="0x6666666666666666666666666666666666666666",
            lp_token_address="0x7777777777777777777777777777777777777777",
            total_lp_supply=500, observed_at_ms=NOW - 1_000,
            finalized_ref="block:100", inventory_provider="base-pool-indexer",
            inventory_complete=True, pool_set_id="token-pools-v1",
            locker_custody_amount=0,
        ))
    return LiquidityInventoryBatch(
        chain="base", token_address=TOKEN, provider="base-pool-indexer",
        observed_at_ms=NOW - 1_000, finalized_ref="block:100",
        pool_set_id="token-pools-v1", inventory_complete=not incomplete,
        independence_group="independent-pool-indexer",
        pools=tuple(pools), positions=tuple(positions),
        deployments=(deployment,),
    )


def test_official_deployment_candidates_are_chain_specific():
    assert EVM_LOCKER_CANDIDATES["base"]["uncx-univ2-v2"]["abi_profile"] == "uncx-v2.1-locks-nonce"
    assert EVM_LOCKER_CANDIDATES["bsc"]["uncx-pancakeswap-v2"]["abi_profile"] == "uncx-v1-token-lock-index-events"
    assert set(SOLANA_UNCX_PROGRAM_CANDIDATES) == {
        "raydium-amm-v4", "raydium-amm-v4-smart-lockers",
        "raydium-cp-swap", "raydium-clmm", "meteora-dlmm", "pumpswap",
    }
    assert SOLANA_UNCX_PROGRAM_CANDIDATES["raydium-amm-v4"]["published_executable_sha256"]
    for key in set(SOLANA_UNCX_PROGRAM_CANDIDATES) - {"raydium-amm-v4"}:
        assert SOLANA_UNCX_PROGRAM_CANDIDATES[key]["published_executable_sha256"] is None


def test_complete_single_pool_snapshot_passes_and_emits_scoped_evidence():
    report = LiquidityEvidenceAnalyzer().evaluate(
        base_batch(), chain="base", token_address=TOKEN, now_ms=NOW,
    )
    assert report.passed
    assert report.pool_count == report.verified_pool_count == 1
    assert report.active_locked_pool_count == 1
    assert report.active_locked_pct == 100.0
    assert report.total_lp_supply is None
    assert report.active_locked_lp is None
    assert report.per_pool_locked_pct == ((POOL, 100.0),)
    evidence = report.to_safety_evidence(independence_group="independent-lock-auditor")
    assert evidence.claim == "liquidity_lock"
    assert evidence.liquidity_inventory_complete is True
    assert evidence.liquidity_pool_count == 1


def test_one_locked_pool_does_not_certify_a_second_unlocked_pool():
    report = LiquidityEvidenceAnalyzer().evaluate(
        base_batch(second_pool=True), chain="base", token_address=TOKEN, now_ms=NOW,
    )
    assert not report.passed
    assert report.pool_count == 2
    assert report.active_locked_pool_count == 1
    assert report.active_locked_pct == 0.0
    assert dict(report.per_pool_locked_pct) == {POOL: 100.0, "0x6666666666666666666666666666666666666666": 0.0}
    assert any("at least one discovered pool" in reason for reason in report.reasons)


@pytest.mark.parametrize("kwargs,reason", [
    ({"incomplete": True}, "pool inventory is not certified complete"),
    ({"expired": True}, "at least one discovered pool"),
    ({"custody_mismatch": True}, "locker records do not reconcile"),
    ({"unsupported": True}, "unsupported or unverified locker deployment"),
])
def test_incomplete_expired_mismatched_or_unsupported_evidence_fails(kwargs, reason):
    report = LiquidityEvidenceAnalyzer().evaluate(
        base_batch(**kwargs), chain="base", token_address=TOKEN, now_ms=NOW,
    )
    assert not report.passed
    assert any(reason in item for item in report.reasons)


def test_evm_provider_requires_exact_pinned_deployment_and_reader_identity():
    class Reader:
        provider_id = "reader"

        def __init__(self, batch):
            self.batch = batch

        def fetch_inventory(self, chain, token_address):
            return self.batch

    provider = EvmLiquidityLockerProvider("base", base_deployment(), Reader(base_batch()))
    assert provider.fetch_inventory(TOKEN).pool_set_id == "token-pools-v1"
    wrong = replace(base_deployment(), address="0x8888888888888888888888888888888888888888")
    with pytest.raises(ValueError, match="not verified"):
        EvmLiquidityLockerProvider("base", wrong, Reader(base_batch())).fetch_inventory(TOKEN)


def test_evm_provider_rejects_cross_chain_deployment():
    with pytest.raises(ValueError, match="matching Base/BSC"):
        EvmLiquidityLockerProvider(
            "base", replace(base_deployment(), chain="bsc", chain_id=56), object()
        )


SOLANA_PROGRAM = "GsSCS3vPWrtJ5Y9aEVVT65fmrex5P5RGHXdZvsdbWgfo"
SOLANA_AUTHORITY = "BzKincxjgFQjj4FmhaWrwHES1ekBGN73YesA7JwJJo7X"
SOLANA_MINT = "So11111111111111111111111111111111111111112"
SOLANA_POOL = "11111111111111111111111111111111"
SOLANA_LP = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def solana_batch():
    digest = SOLANA_UNCX_PROGRAM_CANDIDATES["raydium-amm-v4"]["published_executable_sha256"]
    deployment = LockerDeployment(
        chain="solana", provider="solana-locker-reader", address=SOLANA_PROGRAM,
        abi_profile="uncx-anchor-idl-v0.1", expected_code_hash=digest,
        source_ref="https://github.com/uncx-network/raydium-amm-lp-locker",
        independence_group="uncx", program_executable_hash=digest,
        observed_code_hash=digest, custody_authority=SOLANA_AUTHORITY,
    )
    pool = PoolLiquiditySnapshot(
        chain="solana", token_address=SOLANA_MINT, pool_address=SOLANA_POOL,
        lp_token_address=SOLANA_LP, total_lp_supply=100,
        observed_at_ms=NOW - 100, finalized_ref="slot:900",
        inventory_provider="solana-pool-indexer", inventory_complete=True,
        pool_set_id="mint-pools-v1", locker_custody_amount=100,
    )
    position = LockerPositionSnapshot(
        chain="solana", pool_address=SOLANA_POOL, lp_token_address=SOLANA_LP,
        locker_address=SOLANA_PROGRAM, position_id="pda:lock-1", amount=100,
        unlock_at_ms=NOW + 10_000, observed_at_ms=NOW - 100,
        owner="OwnerWallet111111111111111111111111111",
        evidence_ref="signature:lock-1", account_owner=SOLANA_PROGRAM,
        custody_account="CustodyTokenAccount111111111111111111111",
        custody_authority=SOLANA_AUTHORITY, custody_token_mint=SOLANA_LP,
        custody_account_owner=TOKEN_PROGRAM, finalized_ref="slot:900",
    )
    return LiquidityInventoryBatch(
        chain="solana", token_address=SOLANA_MINT, provider="solana-pool-indexer",
        observed_at_ms=NOW - 100, finalized_ref="slot:900",
        pool_set_id="mint-pools-v1", inventory_complete=True,
        independence_group="independent-pool-indexer",
        pools=(pool,), positions=(position,), deployments=(deployment,),
    )


def test_solana_program_owner_executable_and_token_custody_are_checked():
    batch = solana_batch()

    class Reader:
        def fetch_inventory(self, chain, token_address):
            return batch

    deployment = batch.deployments[0]
    provider = SolanaLiquidityLockerProvider(deployment, Reader())
    fetched = provider.fetch_inventory(SOLANA_MINT)
    report = LiquidityEvidenceAnalyzer().evaluate(
        fetched, chain="solana", token_address=SOLANA_MINT, now_ms=NOW,
    )
    assert report.passed


def test_solana_unknown_program_and_wrong_account_owner_are_rejected():
    batch = solana_batch()

    class Reader:
        def __init__(self, result):
            self.result = result

        def fetch_inventory(self, chain, token_address):
            return self.result

    unknown = replace(batch.deployments[0], address="11111111111111111111111111111111")
    with pytest.raises(ValueError, match="unsupported Solana"):
        SolanaLiquidityLockerProvider(unknown, Reader(batch))

    bad_position = replace(batch.positions[0], account_owner="11111111111111111111111111111111")
    bad_batch = replace(batch, positions=(bad_position,))
    with pytest.raises(ValueError, match="not owned by the pinned program"):
        SolanaLiquidityLockerProvider(batch.deployments[0], Reader(bad_batch)).fetch_inventory(SOLANA_MINT)


def test_base_uncx_v21_candidate_matches_current_official_deployment():
    from veltrix.liquidity_evidence import EVM_LOCKER_CANDIDATES
    assert EVM_LOCKER_CANDIDATES["base"]["uncx-univ2-v2"]["address"] == (
        "0xc4E637D37113192F4F1F060DaEbD7758De7F4131"
    )
