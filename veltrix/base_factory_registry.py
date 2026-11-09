"""Pinned Base V2 factory identities; not a complete AMM registry.

The Uniswap V2 factory identity is cross-checked against the official Uniswap
deployment directory. Its chain state is separately pinned by the read-only
archive acceptance report. Registry completeness remains false until every
supported Base V2 AMM factory has been enumerated and independently accepted.
"""
from dataclasses import dataclass

from .providers.evm_liquidity_reader import EvmFactorySpec


@dataclass(frozen=True)
class BaseFactoryIdentity:
    amm_id: str
    chain_id: int
    address: str
    deployment_block: int
    creation_tx: str
    expected_code_hash: str
    source_ref: str

    def as_reader_spec(self) -> EvmFactorySpec:
        """Convert the identity into the reader's minimal factory spec."""
        return EvmFactorySpec(
            amm_id=self.amm_id,
            address=self.address,
            deployment_block=self.deployment_block,
            expected_code_hash=self.expected_code_hash,
            chain_id=self.chain_id,
            creation_tx=self.creation_tx,
            source_ref=self.source_ref,
        )

    def validate_observation(
        self, *, chain_id: int, address: str, deployment_block: int,
        creation_tx: str, code_hash: str,
    ) -> None:
        """Fail closed unless every observed identity field matches the pin."""
        expected = {
            "chain_id": self.chain_id,
            "address": self.address.lower(),
            "deployment_block": self.deployment_block,
            "creation_tx": self.creation_tx.lower(),
            "code_hash": self.expected_code_hash.lower().removeprefix("0x"),
        }
        observed = {
            "chain_id": chain_id,
            "address": str(address).lower(),
            "deployment_block": deployment_block,
            "creation_tx": str(creation_tx).lower(),
            "code_hash": str(code_hash).lower().removeprefix("0x"),
        }
        mismatches = [
            field for field in expected if observed[field] != expected[field]
        ]
        if mismatches:
            raise ValueError(
                "Base factory identity mismatch: " + ", ".join(mismatches)
            )


BASE_UNISWAP_V2_FACTORY = BaseFactoryIdentity(
    amm_id="uniswap-v2",
    chain_id=8453,
    address="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
    deployment_block=6601915,
    creation_tx="0x3c94031f81d9afe3beeb8fbcf4dcf1bd5b5688b86081d94e3d0231514dc00d31",
    expected_code_hash="bab145d02e7005f0d84c6c1639d39b799b0ea16df99ebbdaf5a14d9da820b4e0",
    source_ref="https://developers.uniswap.org/docs/protocols/v2/deployments",
)

# One official factory is pinned. This must remain False until the full
# supported Base V2 AMM universe has been enumerated and reviewed.
BASE_V2_FACTORY_CANDIDATES = (BASE_UNISWAP_V2_FACTORY,)
BASE_V2_FACTORY_READER_SPECS = tuple(
    item.as_reader_spec() for item in BASE_V2_FACTORY_CANDIDATES
)
BASE_V2_FACTORY_REGISTRY_COMPLETE = False
