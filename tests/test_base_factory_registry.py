from veltrix.base_factory_registry import (
    BASE_UNISWAP_V2_FACTORY,
    BASE_V2_FACTORY_CANDIDATES,
    BASE_V2_FACTORY_REGISTRY_COMPLETE,
    UNISWAP_DEPLOYMENTS_SOURCE,
)


def test_base_uniswap_v2_factory_is_pinned_to_archive_qualified_identity():
    assert BASE_UNISWAP_V2_FACTORY.amm_id == "uniswap-v2"
    assert BASE_UNISWAP_V2_FACTORY.address == "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
    assert BASE_UNISWAP_V2_FACTORY.deployment_block == 6_601_915
    assert BASE_UNISWAP_V2_FACTORY.expected_code_hash == (
        "bab145d02e7005f0d84c6c1639d39b799b0ea16df99ebbdaf5a14d9da820b4e0"
    )
    assert BASE_V2_FACTORY_CANDIDATES == (BASE_UNISWAP_V2_FACTORY,)
    assert UNISWAP_DEPLOYMENTS_SOURCE.startswith("https://")


def test_single_factory_candidate_does_not_claim_complete_base_amm_coverage():
    assert BASE_V2_FACTORY_REGISTRY_COMPLETE is False
