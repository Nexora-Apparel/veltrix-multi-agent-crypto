"""Chain-specific, fail-closed LP-lock evidence normalization.

RPC/indexer adapters must supply complete, finalized snapshots. This module
validates deployment identity, account ownership, per-pool custody, active
unlock schedules and total-supply reconciliation before producing a
claim-scoped SafetyEvidence. It never treats a locker name or a single locked
pool as proof that all liquidity is locked.
"""
from dataclasses import dataclass
import hashlib
import math
import re
import time
from typing import Iterable, Protocol

from .safety import SafetyEvidence


EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
HEX_HASH = re.compile(r"^[0-9a-f]{64}$")

# These are discovery candidates only. UNCX's current and legacy docs contain
# conflicting Base labels and explicitly flag some rows as pending verification.
# The runtime reader does not trust this list: operators must confirm the exact
# locker/factory relationship, ABI and deployed bytecode hash before configuring it.
EVM_LOCKER_CANDIDATES = {
    "base": {
        "uncx-univ2-v2": {
            "address": "0xED9180976c2a4742C7A57354FD39d8BEc6cbd8AB",
            "abi_profile": "uncx-v2.1-locks-nonce",
            "source_ref": "https://docs.uncx.network/guides/for-developers/liquidity-lockers/lockers-v2/contracts",
        },
        "uncx-sushiswap-v2": {
            "address": "0xBeddF48499788607B4c2e704e9099561ab38Aae8",
            "abi_profile": "uncx-v2.0-locks-nonce",
            "source_ref": "https://github.com/uncx-network/liquidity-locker-univ2-contracts",
        },
    },
    "bsc": {
        "uncx-pancakeswap-v2": {
            "address": "0xC765bddB93b0D1c1A88282BA0fa6B2d00E3e0c83",
            "abi_profile": "uncx-v1-token-lock-index-events",
            "source_ref": "https://github.com/uncx-network/liquidity-locker-univ2-contracts",
        },
    },
}

# UNCX's public Solana guide lists these program IDs and LP storage accounts.
# Only the AMM V4 program has a published verifiable-executable hash in the
# project's public source. Other listed programs are discovery candidates,
# not supported/certifying profiles until their deployed executable and IDL
# are independently pinned.
SOLANA_UNCX_PROGRAM_CANDIDATES = {
    "raydium-amm-v4": {
        "program_id": "GsSCS3vPWrtJ5Y9aEVVT65fmrex5P5RGHXdZvsdbWgfo",
        "documented_lp_storage_account": "BzKincxjgFQjj4FmhaWrwHES1ekBGN73YesA7JwJJo7X",
        "custody_authority_seed": "uncx_authority",
        "lp_vault_seed": "uncx_lp_vault",
        "published_executable_sha256": "60838187b2c2b92fc6094bba1dcc2a9607ca40f7fb0fdb9e7011e08fa0c863e9",
        "source_ref": "https://github.com/uncx-network/raydium-amm-lp-locker",
    },
}


@dataclass(frozen=True)
class LockerDeployment:
    chain: str
    provider: str
    address: str
    abi_profile: str
    expected_code_hash: str
    source_ref: str
    independence_group: str
    chain_id: int | None = None
    observed_code_hash: str | None = None
    program_executable_hash: str | None = None
    custody_authority: str | None = None


@dataclass(frozen=True)
class PoolLiquiditySnapshot:
    chain: str
    token_address: str
    pool_address: str
    lp_token_address: str
    total_lp_supply: int
    observed_at_ms: int
    finalized_ref: str
    inventory_provider: str
    inventory_complete: bool
    pool_set_id: str
    # Exact on-chain LP token balance held by the locker custody account.
    locker_custody_amount: int


@dataclass(frozen=True)
class LockerPositionSnapshot:
    chain: str
    pool_address: str
    lp_token_address: str
    locker_address: str
    position_id: str
    amount: int
    unlock_at_ms: int
    observed_at_ms: int
    owner: str
    evidence_ref: str
    account_owner: str | None = None
    custody_account: str | None = None
    custody_authority: str | None = None
    custody_token_mint: str | None = None
    custody_account_owner: str | None = None
    finalized_ref: str | None = None


@dataclass(frozen=True)
class LiquidityInventoryBatch:
    chain: str
    token_address: str
    provider: str
    independence_group: str
    observed_at_ms: int
    finalized_ref: str
    pool_set_id: str
    inventory_complete: bool
    pools: tuple[PoolLiquiditySnapshot, ...]
    positions: tuple[LockerPositionSnapshot, ...]
    deployments: tuple[LockerDeployment, ...]
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiquidityEvidenceReport:
    chain: str
    token_address: str
    passed: bool
    pool_count: int
    verified_pool_count: int
    active_locked_pool_count: int
    total_lp_supply: int
    active_locked_lp: int
    active_locked_pct: float | None
    earliest_unlock_at_ms: int | None
    evidence_id: str
    provider: str
    independence_group: str
    observed_at_ms: int
    source_ref: str
    reasons: tuple[str, ...]

    def to_safety_evidence(self, *, independence_group: str) -> SafetyEvidence:
        return SafetyEvidence(
            token_address=self.token_address,
            provider=self.provider,
            evidence_id=self.evidence_id,
            observed_at_ms=self.observed_at_ms,
            liquidity_locked=self.passed,
            dev_hold_pct=None,
            bundled_supply_pct=None,
            claim="liquidity_lock",
            chain=self.chain,
            coverage_complete=self.passed,
            source_ref=self.source_ref,
            method="chain_specific_all_pool_locker_ownership_and_supply_reconciliation",
            independence_group=f"{independence_group}+{self.independence_group}",
            liquidity_inventory_complete=self.passed,
            liquidity_pool_count=self.pool_count,
            liquidity_locked_pool_count=self.active_locked_pool_count,
            liquidity_locked_lp_pct=self.active_locked_pct,
        )


class LiquiditySnapshotSource(Protocol):
    provider_id: str

    def fetch_inventory(self, chain: str, token_address: str) -> LiquidityInventoryBatch: ...


def _same_address(chain: str, left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.lower() == right.lower() if chain in {"base", "bsc"} else left == right


def _valid_address(chain: str, address: str) -> bool:
    if chain in {"base", "bsc"}:
        return bool(EVM_ADDRESS.fullmatch(address or ""))
    return bool(SOLANA_ADDRESS.fullmatch(address or ""))


def _id_for(batch: LiquidityInventoryBatch) -> str:
    payload = (
        batch.chain, batch.token_address, batch.provider, batch.observed_at_ms,
        batch.finalized_ref, batch.pool_set_id,
        tuple(sorted((p.pool_address, p.lp_token_address, p.total_lp_supply,
                      p.locker_custody_amount) for p in batch.pools)),
        tuple(sorted((p.position_id, p.locker_address, p.lp_token_address,
                      p.amount, p.unlock_at_ms, p.evidence_ref, p.finalized_ref) for p in batch.positions)),
        tuple(sorted((d.address, d.expected_code_hash, d.observed_code_hash,
                      d.abi_profile, d.provider, d.source_ref) for d in batch.deployments)),
    )
    return hashlib.sha256(repr(payload).encode()).hexdigest()


class LiquidityEvidenceAnalyzer:
    """Validate every pool and every locker record in one finalized inventory."""

    def __init__(self, *, max_age_ms: int = 300_000, minimum_locked_pct: float = 99.99):
        if max_age_ms <= 0 or not 0 < minimum_locked_pct <= 100:
            raise ValueError("invalid liquidity evidence bounds")
        self.max_age_ms = max_age_ms
        self.minimum_locked_pct = minimum_locked_pct

    def evaluate(
        self, batch: LiquidityInventoryBatch | None, *, chain: str,
        token_address: str, now_ms: int,
    ) -> LiquidityEvidenceReport:
        reasons: list[str] = []
        chain = chain.lower()
        if chain not in {"solana", "base", "bsc"}:
            reasons.append("unsupported liquidity evidence chain")
        if not isinstance(batch, LiquidityInventoryBatch):
            reasons.append("complete liquidity inventory is missing")
            return self._report(chain, token_address, None, reasons, now_ms)
        if batch.chain != chain or not _same_address(chain, batch.token_address, token_address):
            reasons.append("liquidity inventory chain or token mismatch")
        if (not batch.provider or not batch.independence_group
                or not batch.finalized_ref or not batch.pool_set_id):
            reasons.append("liquidity inventory provenance is incomplete")
        if batch.inventory_complete is not True:
            reasons.append("pool inventory is not certified complete")
        if batch.rejected:
            reasons.append("liquidity inventory contains rejected source records")
        if (not isinstance(batch.observed_at_ms, int) or isinstance(batch.observed_at_ms, bool)
                or batch.observed_at_ms > now_ms or now_ms - batch.observed_at_ms > self.max_age_ms):
            reasons.append("liquidity inventory is stale or timestamp-invalid")
        if not batch.pools:
            reasons.append("no pools were discovered for token")
        if not batch.deployments:
            reasons.append("no independently pinned locker deployment is present")

        deployment_map = {}
        for deployment in batch.deployments:
            if not isinstance(deployment, LockerDeployment):
                reasons.append("malformed locker deployment record")
                continue
            if deployment.chain != chain:
                reasons.append("locker deployment chain mismatch")
                continue
            if not _valid_address(chain, deployment.address):
                reasons.append("locker deployment address is malformed")
                continue
            if (not HEX_HASH.fullmatch(deployment.expected_code_hash or "")
                    or deployment.observed_code_hash != deployment.expected_code_hash):
                reasons.append("locker deployment runtime hash is not verified against pinned bytecode")
                continue
            if (not deployment.provider or deployment.provider == batch.provider
                    or deployment.independence_group == batch.independence_group):
                reasons.append("locker deployment identity must be independently sourced from pool inventory")
            if (not deployment.source_ref.startswith("https://")
                    or not deployment.independence_group):
                reasons.append("locker deployment provenance or ownership is missing")
                continue
            if chain in {"base", "bsc"}:
                expected_chain_id = 8453 if chain == "base" else 56
                if deployment.chain_id != expected_chain_id or not deployment.abi_profile:
                    reasons.append("unsupported EVM chain ID or locker ABI profile")
                    continue
            if chain == "solana" and (
                not HEX_HASH.fullmatch(deployment.program_executable_hash or "")
                or deployment.program_executable_hash != deployment.expected_code_hash
                or deployment.observed_code_hash != deployment.expected_code_hash
                or not _valid_address("solana", deployment.custody_authority or "")
            ):
                reasons.append("Solana locker executable hash or custody authority is not pinned")
                continue
            deployment_key = deployment.address.lower() if chain in {"base", "bsc"} else deployment.address
            if deployment_key in deployment_map:
                reasons.append("duplicate locker deployment identity")
                continue
            deployment_map[deployment_key] = deployment

        pools = {}
        total_supply = 0
        total_active_locked = 0
        active_locked_pool_count = 0
        per_pool_locked_pct = []
        earliest_unlock = None
        seen_positions = set()
        for pool in batch.pools:
            if not isinstance(pool, PoolLiquiditySnapshot):
                reasons.append("malformed pool snapshot")
                continue
            key = pool.pool_address.lower() if chain in {"base", "bsc"} else pool.pool_address
            if key in pools:
                reasons.append("duplicate pool in inventory")
                continue
            pools[key] = pool
            if pool.chain != chain or not _same_address(chain, pool.token_address, token_address):
                reasons.append("pool chain or token mismatch")
            if not _valid_address(chain, pool.pool_address) or not _valid_address(chain, pool.lp_token_address):
                reasons.append("pool or LP token address is malformed")
            if pool.inventory_provider != batch.provider or pool.pool_set_id != batch.pool_set_id:
                reasons.append("pool inventory provider or set identity mismatch")
            if pool.finalized_ref != batch.finalized_ref or pool.observed_at_ms != batch.observed_at_ms:
                reasons.append("pool snapshots do not share the batch finalized state")
            if pool.inventory_complete is not True:
                reasons.append("pool-level inventory coverage is incomplete")
            if (not isinstance(pool.total_lp_supply, int) or isinstance(pool.total_lp_supply, bool)
                    or pool.total_lp_supply <= 0):
                reasons.append("pool LP total supply is missing or invalid")
                continue
            if (not isinstance(pool.locker_custody_amount, int)
                    or isinstance(pool.locker_custody_amount, bool)
                    or pool.locker_custody_amount < 0):
                reasons.append("locker LP custody balance is invalid")
                continue
            total_supply += pool.total_lp_supply

        positions_by_pool = {key: [] for key in pools}
        for position in batch.positions:
            if not isinstance(position, LockerPositionSnapshot):
                reasons.append("malformed locker position")
                continue
            if position.chain != chain:
                reasons.append("locker position chain mismatch")
            if position.finalized_ref != batch.finalized_ref or position.observed_at_ms != batch.observed_at_ms:
                reasons.append("locker positions do not share the batch finalized state")
            pool_key = position.pool_address.lower() if chain in {"base", "bsc"} else position.pool_address
            pool = pools.get(pool_key)
            if pool is None:
                reasons.append("locker position references an undiscovered pool")
                continue
            if not _same_address(chain, position.lp_token_address, pool.lp_token_address):
                reasons.append("locker position LP token does not match pool")
            locker_key = position.locker_address.lower() if chain in {"base", "bsc"} else position.locker_address
            deployment = deployment_map.get(locker_key)
            if deployment is None:
                reasons.append("position uses an unsupported or unverified locker deployment")
            if chain == "solana":
                if not position.account_owner or not _same_address("solana", position.account_owner, position.locker_address):
                    reasons.append("Solana lock account owner does not match verified locker program")
                if not position.custody_account or not position.custody_authority:
                    reasons.append("Solana locker token-account custody or authority is missing")
                if deployment is not None and position.custody_authority != deployment.custody_authority:
                    reasons.append("Solana LP custody authority does not match the pinned locker authority")
                if not _same_address("solana", position.custody_token_mint or "", position.lp_token_address):
                    reasons.append("Solana locker custody token mint does not match LP mint")
                if position.custody_account_owner not in {
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                }:
                    reasons.append("Solana custody account is not owned by a supported token program")
            if not position.position_id or position.position_id in seen_positions:
                reasons.append("locker position ID is missing or duplicated")
            if not position.owner:
                reasons.append("locker position owner is missing")
            seen_positions.add(position.position_id)
            if (not isinstance(position.amount, int) or isinstance(position.amount, bool)
                    or position.amount <= 0):
                reasons.append("locker position amount is invalid")
                continue
            if (not isinstance(position.unlock_at_ms, int) or isinstance(position.unlock_at_ms, bool)
                    or position.unlock_at_ms <= 0):
                reasons.append("locker unlock timestamp is missing or invalid")
                continue
            if deployment is not None and not _same_address(chain, position.locker_address, deployment.address):
                reasons.append("locker position address does not match pinned deployment")
            if not position.evidence_ref:
                reasons.append("locker position source reference is missing")
            if (not isinstance(position.observed_at_ms, int) or isinstance(position.observed_at_ms, bool)
                    or position.observed_at_ms > now_ms
                    or now_ms - position.observed_at_ms > self.max_age_ms):
                reasons.append("locker position is stale or timestamp-invalid")
            positions_by_pool[pool_key].append(position)

        for pool_key, pool in pools.items():
            positions = positions_by_pool[pool_key]
            recorded_custody = sum(item.amount for item in positions)
            if recorded_custody != pool.locker_custody_amount:
                reasons.append("locker records do not reconcile to on-chain LP custody balance")
            active = sum(item.amount for item in positions if item.unlock_at_ms > now_ms)
            if active > pool.total_lp_supply:
                reasons.append("active locked LP exceeds pool LP total supply")
            total_active_locked += active
            per_pool_locked_pct.append(100.0 * active / pool.total_lp_supply)
            if active > 0:
                active_locked_pool_count += 1
                unlocks = [item.unlock_at_ms for item in positions if item.amount > 0 and item.unlock_at_ms > now_ms]
                if unlocks:
                    earliest_unlock = min(earliest_unlock, min(unlocks)) if earliest_unlock else min(unlocks)

        # LP units are pool-specific and cannot be summed into a meaningful
        # cross-pool ratio. The weakest pool determines the aggregate result.
        locked_pct = min(per_pool_locked_pct) if per_pool_locked_pct else None
        if locked_pct is None or not math.isfinite(locked_pct) or any(
            pct < self.minimum_locked_pct for pct in per_pool_locked_pct
        ):
            reasons.append("at least one discovered pool lacks the required active locked LP share")
        if len(pools) != len(batch.pools):
            reasons.append("pool inventory contains malformed or duplicate entries")
        passed = not reasons and bool(pools) and locked_pct is not None
        return LiquidityEvidenceReport(
            chain, token_address, passed, len(batch.pools), len(pools),
            active_locked_pool_count, total_supply, total_active_locked, locked_pct,
            earliest_unlock, _id_for(batch), batch.provider,
            "+".join(sorted({d.independence_group for d in deployment_map.values()})),
            batch.observed_at_ms, batch.finalized_ref, tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _report(chain, token, batch, reasons, now_ms):
        return LiquidityEvidenceReport(
            chain, token, False, 0, 0, 0, 0, 0, None, None,
            hashlib.sha256(f"{chain}:{token}:{now_ms}".encode()).hexdigest(),
            getattr(batch, "provider", "missing"), "unverified-locker",
            now_ms,
            getattr(batch, "finalized_ref", ""), tuple(reasons),
        )


class EvmLiquidityLockerProvider:
    """Chain-pinned EVM locker snapshot adapter.

    The injected reader must verify eth_chainId, eth_getCode against the
    operator-pinned runtime hash, enumerate the full locker ABI state/event
    history, decode current positions and query each LP token's totalSupply and
    locker balance at the same finalized block. This wrapper rejects an
    unpinned ABI/address or incomplete reader result.
    """
    def __init__(self, chain: str, deployment: LockerDeployment, reader: LiquiditySnapshotSource):
        if chain not in {"base", "bsc"} or deployment.chain != chain:
            raise ValueError("EVM liquidity provider requires a matching Base/BSC deployment")
        self.chain, self.deployment, self.reader = chain, deployment, reader
        self.provider_id = f"{chain}-uncx-locker-inventory"

    def fetch_inventory(self, token_address: str) -> LiquidityInventoryBatch:
        batch = self.reader.fetch_inventory(self.chain, token_address)
        if not isinstance(batch, LiquidityInventoryBatch) or batch.chain != self.chain:
            raise ValueError("EVM locker reader returned malformed or wrong-chain inventory")
        if not any(
            d.address.lower() == self.deployment.address.lower()
            and d.expected_code_hash == self.deployment.expected_code_hash
            and d.observed_code_hash == self.deployment.expected_code_hash
            and d.abi_profile == self.deployment.abi_profile
            for d in batch.deployments
        ):
            raise ValueError("configured EVM locker deployment was not verified by reader")
        return batch


class SolanaLiquidityLockerProvider:
    """Solana account-owner/program-hash verifier for supported locker profiles.

    The injected reader must enumerate every lock PDA and custody token account,
    decode the exact program account layout, and verify account owner/program
    executable hash at one finalized slot. Unknown programs and account layouts
    are never interpreted as locks.
    """
    SUPPORTED_PROGRAMS = {
        "GsSCS3vPWrtJ5Y9aEVVT65fmrex5P5RGHXdZvsdbWgfo": "raydium-amm-v4",
    }

    def __init__(self, deployment: LockerDeployment, reader: LiquiditySnapshotSource):
        if deployment.chain != "solana" or deployment.address not in self.SUPPORTED_PROGRAMS:
            raise ValueError("unsupported Solana LP-lock program")
        self.deployment, self.reader = deployment, reader
        self.provider_id = "solana-uncx-locker-inventory"

    def fetch_inventory(self, token_address: str) -> LiquidityInventoryBatch:
        batch = self.reader.fetch_inventory("solana", token_address)
        if not isinstance(batch, LiquidityInventoryBatch) or batch.chain != "solana":
            raise ValueError("Solana locker reader returned malformed inventory")
        if not any(
            d.address == self.deployment.address
            and d.program_executable_hash == self.deployment.program_executable_hash
            and d.observed_code_hash == self.deployment.expected_code_hash
            and d.expected_code_hash == self.deployment.expected_code_hash
            for d in batch.deployments
        ):
            raise ValueError("Solana locker executable identity was not verified by reader")
        if any(position.account_owner != self.deployment.address for position in batch.positions):
            raise ValueError("Solana locker account is not owned by the pinned program")
        return batch
