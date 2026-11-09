import pytest

from veltrix.base_factory_registry import (
    BASE_UNISWAP_V2_FACTORY,
    BASE_V2_FACTORY_CANDIDATES,
    BASE_V2_FACTORY_READER_SPECS,
    BASE_V2_FACTORY_REGISTRY_COMPLETE,
)


def test_base_uniswap_v2_factory_is_pinned_to_archive_qualified_identity():
    identity = BASE_UNISWAP_V2_FACTORY
    assert identity.amm_id == "uniswap-v2"
    assert identity.chain_id == 8453
    assert identity.address == "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
    assert identity.deployment_block == 6_601_915
    assert identity.creation_tx == (
        "0x3c94031f81d9afe3beeb8fbcf4dcf1bd5b5688b86081d94e3d0231514dc00d31"
    )
    assert identity.expected_code_hash == (
        "bab145d02e7005f0d84c6c1639d39b799b0ea16df99ebbdaf5a14d9da820b4e0"
    )
    assert identity.source_ref.startswith("https://")
    assert BASE_V2_FACTORY_CANDIDATES == (identity,)
    assert len(BASE_V2_FACTORY_READER_SPECS) == 1
    assert BASE_V2_FACTORY_READER_SPECS[0].address == identity.address


def test_factory_identity_validator_accepts_exact_pinned_observation():
    identity = BASE_UNISWAP_V2_FACTORY
    identity.validate_observation(
        chain_id=8453,
        address=identity.address,
        deployment_block=6_601_915,
        creation_tx=identity.creation_tx,
        code_hash=identity.expected_code_hash,
    )


@pytest.mark.parametrize("field, value", [
    ("chain_id", 1),
    ("address", "0x" + "11" * 20),
    ("deployment_block", 6_601_916),
    ("creation_tx", "0x" + "22" * 32),
    ("code_hash", "00" * 32),
])
def test_factory_identity_validator_rejects_each_mismatch(field, value):
    identity = BASE_UNISWAP_V2_FACTORY
    observed = {
        "chain_id": 8453,
        "address": identity.address,
        "deployment_block": 6_601_915,
        "creation_tx": identity.creation_tx,
        "code_hash": identity.expected_code_hash,
    }
    observed[field] = value
    with pytest.raises(ValueError, match="Base factory identity mismatch"):
        identity.validate_observation(**observed)


def test_single_factory_candidate_does_not_claim_complete_base_amm_coverage():
    assert BASE_V2_FACTORY_REGISTRY_COMPLETE is False
