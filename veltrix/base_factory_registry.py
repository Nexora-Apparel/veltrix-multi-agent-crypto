"""Pinned Base V2 factory identities; this is not yet a complete AMM registry.

The Uniswap V2 factory identity is cross-checked against the official Uniswap
deployment directory. Its chain state is separately pinned by the read-only
archive acceptance report. Registry completeness remains false until every
supported Base V2 AMM factory has been enumerated and independently accepted.
"""
from .providers.evm_liquidity_reader import EvmFactorySpec


BASE_UNISWAP_V2_FACTORY = EvmFactorySpec(
    amm_id="uniswap-v2",
    address="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
    deployment_block=6601915,
    expected_code_hash="bab145d02e7005f0d84c6c1639d39b799b0ea16df99ebbdaf5a14d9da820b4e0",
)

# One official factory is pinned. This must remain False until the full
# supported Base V2 AMM universe has been enumerated and reviewed.
BASE_V2_FACTORY_CANDIDATES = (BASE_UNISWAP_V2_FACTORY,)
BASE_V2_FACTORY_REGISTRY_COMPLETE = False

UNISWAP_DEPLOYMENTS_SOURCE = (
    "https://developers.uniswap.org/docs/protocols/v2/deployments"
)
