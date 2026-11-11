"""Read-only Raydium CLMM PoolState decoder pinned to an upstream source commit.

This validates the fixed PoolState header, mint pair, vault identities, swap
status and canonical PDA. It does not decode tick arrays, vault token balances,
fee configuration, liquidity depth, oracle state, or DEX CPI account semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence, DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_v6_route import JupiterV6RouteInspection


RAYDIUM_CLMM_PROGRAM_ID = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CLMM_SOURCE_COMMIT = "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"
RAYDIUM_POOL_STATE_DISCRIMINATOR = hashlib.sha256(b"account:PoolState").digest()[:8]
RAYDIUM_POOL_STATE_LENGTH = 1544
RAYDIUM_POOL_HEADER_LENGTH = 397
RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET = 904
RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS = 16
RAYDIUM_POOL_REWARD_INFO_OFFSET = 397
RAYDIUM_POOL_REWARD_INFO_LENGTH = 169
RAYDIUM_POOL_REWARD_COUNT = 3
RAYDIUM_POOL_DYNAMIC_FEE_INFO_OFFSET = 1096
RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH = 80
RAYDIUM_POOL_SEED = b"pool"
RAYDIUM_FEE_RATE_DENOMINATOR = 1_000_000
RAYDIUM_DYNAMIC_FEE_CONTROL_DENOMINATOR = 100_000
RAYDIUM_DYNAMIC_FEE_REDUCTION_DENOMINATOR = 10_000
RAYDIUM_MIN_TICK = -443636
RAYDIUM_MAX_TICK = 443636
RAYDIUM_SWAP_DISABLED_BIT = 1 << 4
RAYDIUM_FEE_ON_MAX = 2


class RaydiumClmmPoolStateError(ValueError):
    """Raised when a Raydium CLMM pool account is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmDynamicFeeInfo:
    filter_period: int = 0
    decay_period: int = 0
    reduction_factor: int = 0
    dynamic_fee_control: int = 0
    max_volatility_accumulator: int = 0
    tick_spacing_index_reference: int = 0
    volatility_reference: int = 0
    volatility_accumulator: int = 0
    last_update_timestamp: int = 0
    padding: bytes = bytes(46)

    @property
    def enabled(self) -> bool:
        return self.dynamic_fee_control > 0


@dataclass(frozen=True, slots=True)
class RaydiumClmmRewardInfo:
    reward_state: int = 0
    open_time: int = 0
    end_time: int = 0
    last_update_time: int = 0
    emissions_per_second_x64: int = 0
    reward_total_emitted: int = 0
    reward_claimed: int = 0
    token_mint: str = "11111111111111111111111111111111"
    token_vault: str = "11111111111111111111111111111111"
    authority: str = "11111111111111111111111111111111"
    reward_growth_global_x64: int = 0

    @property
    def initialized(self) -> bool:
        return self.token_mint != "11111111111111111111111111111111"


@dataclass(frozen=True, slots=True)
class RaydiumClmmPoolState:
    pool_address: str
    bump: int
    amm_config: str
    pool_creator: str
    token_mint_0: str
    token_mint_1: str
    token_vault_0: str
    token_vault_1: str
    observation_key: str
    mint_decimals_0: int
    mint_decimals_1: int
    tick_spacing: int
    liquidity: int
    sqrt_price_x64: int
    tick_current: int
    status: int
    fee_on: int
    seed_index: int
    account_data_sha256: str
    fee_growth_global_0_x64: int = 0
    fee_growth_global_1_x64: int = 0
    protocol_fees_token_0: int = 0
    protocol_fees_token_1: int = 0
    fund_fees_token_0: int = 0
    fund_fees_token_1: int = 0
    tick_array_bitmap: tuple[int, ...] = (0,) * RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT
    dynamic_fee_info: RaydiumClmmDynamicFeeInfo = RaydiumClmmDynamicFeeInfo()
    reward_infos: tuple[RaydiumClmmRewardInfo, ...] = (RaydiumClmmRewardInfo(),) * RAYDIUM_POOL_REWARD_COUNT
    account_data: bytes = b""
    source_account_data_sha256: str = ""

    @property
    def swaps_enabled(self) -> bool:
        return not bool(self.status & RAYDIUM_SWAP_DISABLED_BIT)


@dataclass(frozen=True, slots=True)
class RaydiumClmmPoolRouteCheck:
    passed: bool
    pool: RaydiumClmmPoolState
    route_input_mint: str
    route_output_mint: str


def _pubkey(data: bytes, start: int, field: str) -> str:
    raw = data[start:start + 32]
    if len(raw) != 32:
        raise RaydiumClmmPoolStateError(f"pool account is truncated at {field}")
    try:
        key = Pubkey.from_bytes(raw)
    except Exception as exc:
        raise RaydiumClmmPoolStateError(f"pool account has an invalid {field} public key") from exc
    return str(key)


def derive_raydium_clmm_pool_address(
    *,
    amm_config: str,
    token_mint_0: str,
    token_mint_1: str,
    bump: int,
    seed_index: int = 0,
) -> str:
    """Re-derive Raydium PoolState PDA using the exact upstream PoolState::seeds order."""
    if isinstance(bump, bool) or not isinstance(bump, int) or not 0 <= bump <= 255:
        raise RaydiumClmmPoolStateError("Raydium pool bump must be a byte")
    if isinstance(seed_index, bool) or not isinstance(seed_index, int) or not 0 <= seed_index <= 65535:
        raise RaydiumClmmPoolStateError("Raydium pool seed index must be a u16")
    try:
        config_key = Pubkey.from_string(amm_config)
        mint0_key = Pubkey.from_string(token_mint_0)
        mint1_key = Pubkey.from_string(token_mint_1)
        program_key = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        seeds = [
            RAYDIUM_POOL_SEED,
            bytes(config_key),
            bytes(mint0_key),
            bytes(mint1_key),
            b"" if seed_index == 0 else seed_index.to_bytes(2, "little"),
            bytes((bump,)),
        ]
        address, derived_bump = Pubkey.find_program_address(seeds[:-1], program_key)
        if derived_bump != bump:
            raise RaydiumClmmPoolStateError("Raydium pool PDA bump does not match the canonical derivation")
        return str(address)
    except RaydiumClmmPoolStateError:
        raise
    except Exception as exc:
        raise RaydiumClmmPoolStateError(f"Raydium pool PDA derivation failed: {exc}") from exc


def decode_raydium_clmm_pool_state(
    account: DexPoolAccountEvidence,
    *,
    expected_pool_address: str,
) -> RaydiumClmmPoolState:
    """Decode the fixed 397-byte PoolState header and verify its PDA and owner evidence."""
    if not isinstance(account, DexPoolAccountEvidence):
        raise RaydiumClmmPoolStateError("verified DEX pool account evidence is required")
    try:
        expected_key = str(Pubkey.from_string(expected_pool_address))
        actual_key = str(Pubkey.from_string(account.amm_key))
    except Exception as exc:
        raise RaydiumClmmPoolStateError("expected or observed pool address is invalid") from exc
    if actual_key != expected_key:
        raise RaydiumClmmPoolStateError("pool account evidence address differs from the route AMM key")
    if account.dex_label != "Raydium CLMM":
        raise RaydiumClmmPoolStateError("pool evidence is not labelled Raydium CLMM")
    if account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID or account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID:
        raise RaydiumClmmPoolStateError("pool account owner is not the pinned Raydium CLMM program")
    data = account.data_bytes
    if account.executable is not False:
        raise RaydiumClmmPoolStateError("Raydium pool account must be non-executable")
    if isinstance(account.lamports, bool) or not isinstance(account.lamports, int) or account.lamports <= 0:
        raise RaydiumClmmPoolStateError("Raydium pool account must have positive lamports")
    if not isinstance(data, bytes) or account.data_length != len(data) or len(data) != RAYDIUM_POOL_STATE_LENGTH:
        raise RaydiumClmmPoolStateError("Raydium PoolState account length differs from the pinned 1544-byte layout")
    if hashlib.sha256(data).hexdigest() != account.data_sha256:
        raise RaydiumClmmPoolStateError("pool account data hash differs from owner evidence")
    if data[:8] != RAYDIUM_POOL_STATE_DISCRIMINATOR:
        raise RaydiumClmmPoolStateError("Raydium PoolState Anchor discriminator is invalid")

    # Offsets are byte-packed exactly as the upstream #[repr(C, packed)] struct.
    bump = data[8]
    amm_config = _pubkey(data, 9, "amm_config")
    pool_creator = _pubkey(data, 41, "owner")
    mint_0 = _pubkey(data, 73, "token_mint_0")
    mint_1 = _pubkey(data, 105, "token_mint_1")
    vault_0 = _pubkey(data, 137, "token_vault_0")
    vault_1 = _pubkey(data, 169, "token_vault_1")
    observation = _pubkey(data, 201, "observation_key")
    decimals_0 = data[233]
    decimals_1 = data[234]
    tick_spacing = struct.unpack_from("<H", data, 235)[0]
    liquidity = int.from_bytes(data[237:253], "little")
    sqrt_price_x64 = int.from_bytes(data[253:269], "little")
    tick_current = struct.unpack_from("<i", data, 269)[0]
    fee_growth_global_0_x64 = int.from_bytes(data[277:293], "little")
    fee_growth_global_1_x64 = int.from_bytes(data[293:309], "little")
    protocol_fees_token_0 = int.from_bytes(data[309:317], "little")
    protocol_fees_token_1 = int.from_bytes(data[317:325], "little")
    status = data[389]
    fee_on = data[390]
    seed_index = int.from_bytes(data[391:393], "little")
    bitmap_start = RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET
    tick_array_bitmap = tuple(
        int.from_bytes(data[bitmap_start + index * 8:bitmap_start + (index + 1) * 8], "little")
        for index in range(RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS)
    )
    fund_fees_token_0 = int.from_bytes(data[1064:1072], "little")
    fund_fees_token_1 = int.from_bytes(data[1072:1080], "little")

    reward_infos = []
    for reward_index in range(RAYDIUM_POOL_REWARD_COUNT):
        reward_offset = RAYDIUM_POOL_REWARD_INFO_OFFSET + reward_index * RAYDIUM_POOL_REWARD_INFO_LENGTH
        reward_data = data[reward_offset:reward_offset + RAYDIUM_POOL_REWARD_INFO_LENGTH]
        if len(reward_data) != RAYDIUM_POOL_REWARD_INFO_LENGTH:
            raise RaydiumClmmPoolStateError("Raydium RewardInfo is truncated")
        reward_state = reward_data[0]
        if reward_state not in (0, 1, 2, 3):
            raise RaydiumClmmPoolStateError("Raydium RewardInfo state enum is invalid")
        reward = RaydiumClmmRewardInfo(
            reward_state=reward_state,
            open_time=int.from_bytes(reward_data[1:9], "little"),
            end_time=int.from_bytes(reward_data[9:17], "little"),
            last_update_time=int.from_bytes(reward_data[17:25], "little"),
            emissions_per_second_x64=int.from_bytes(reward_data[25:41], "little"),
            reward_total_emitted=int.from_bytes(reward_data[41:49], "little"),
            reward_claimed=int.from_bytes(reward_data[49:57], "little"),
            token_mint=_pubkey(reward_data, 57, "reward_token_mint"),
            token_vault=_pubkey(reward_data, 89, "reward_token_vault"),
            authority=_pubkey(reward_data, 121, "reward_authority"),
            reward_growth_global_x64=int.from_bytes(reward_data[153:169], "little"),
        )
        if reward.initialized:
            if reward.reward_state == 0 or reward.token_vault == str(Pubkey.default()) or reward.authority == str(Pubkey.default()):
                raise RaydiumClmmPoolStateError("initialized RewardInfo has invalid state, vault or authority")
            if reward.end_time < reward.open_time or reward.reward_claimed > reward.reward_total_emitted:
                raise RaydiumClmmPoolStateError("Raydium RewardInfo time or claimed-total invariant is invalid")
        elif reward.reward_state != 0:
            raise RaydiumClmmPoolStateError("uninitialized RewardInfo has a non-uninitialized state")
        reward_infos.append(reward)

    dynamic_offset = RAYDIUM_POOL_DYNAMIC_FEE_INFO_OFFSET
    dynamic_data = data[dynamic_offset:dynamic_offset + RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH]
    if len(dynamic_data) != RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH:
        raise RaydiumClmmPoolStateError("Raydium dynamic fee information is truncated")
    dynamic_fee_info = RaydiumClmmDynamicFeeInfo(
        filter_period=int.from_bytes(dynamic_data[0:2], "little"),
        decay_period=int.from_bytes(dynamic_data[2:4], "little"),
        reduction_factor=int.from_bytes(dynamic_data[4:6], "little"),
        dynamic_fee_control=int.from_bytes(dynamic_data[6:10], "little"),
        max_volatility_accumulator=int.from_bytes(dynamic_data[10:14], "little"),
        tick_spacing_index_reference=int.from_bytes(dynamic_data[14:18], "little", signed=True),
        volatility_reference=int.from_bytes(dynamic_data[18:22], "little"),
        volatility_accumulator=int.from_bytes(dynamic_data[22:26], "little"),
        last_update_timestamp=int.from_bytes(dynamic_data[26:34], "little"),
        padding=dynamic_data[34:80],
    )
    dynamic_values = (
        dynamic_fee_info.filter_period, dynamic_fee_info.decay_period,
        dynamic_fee_info.reduction_factor, dynamic_fee_info.dynamic_fee_control,
        dynamic_fee_info.max_volatility_accumulator,
        dynamic_fee_info.tick_spacing_index_reference,
        dynamic_fee_info.volatility_reference, dynamic_fee_info.volatility_accumulator,
        dynamic_fee_info.last_update_timestamp,
    )
    if not dynamic_fee_info.enabled:
        if any(dynamic_values) or any(dynamic_fee_info.padding):
            raise RaydiumClmmPoolStateError("disabled dynamic-fee state contains non-zero configuration")
    else:
        if (
            dynamic_fee_info.filter_period == 0
            or dynamic_fee_info.decay_period <= dynamic_fee_info.filter_period
            or not 0 < dynamic_fee_info.reduction_factor < RAYDIUM_DYNAMIC_FEE_REDUCTION_DENOMINATOR
            or dynamic_fee_info.dynamic_fee_control >= RAYDIUM_DYNAMIC_FEE_CONTROL_DENOMINATOR
            or dynamic_fee_info.max_volatility_accumulator * tick_spacing > 0xFFFFFFFF
            or dynamic_fee_info.volatility_reference > dynamic_fee_info.max_volatility_accumulator
            or dynamic_fee_info.volatility_accumulator > dynamic_fee_info.max_volatility_accumulator
            or any(dynamic_fee_info.padding)
        ):
            raise RaydiumClmmPoolStateError("dynamic-fee parameters fail the pinned Raydium validation rules")

    default_key = str(Pubkey.default())
    if default_key in {amm_config, pool_creator, mint_0, mint_1, vault_0, vault_1, observation}:
        raise RaydiumClmmPoolStateError("Raydium pool header contains a default public key")
    if mint_0 == mint_1:
        raise RaydiumClmmPoolStateError("Raydium pool token mints must be distinct")
    if bytes(Pubkey.from_string(mint_0)) >= bytes(Pubkey.from_string(mint_1)):
        raise RaydiumClmmPoolStateError("Raydium pool mint order is not canonical")
    if vault_0 == vault_1 or vault_0 in {mint_0, mint_1} or vault_1 in {mint_0, mint_1}:
        raise RaydiumClmmPoolStateError("Raydium pool vault identities are invalid")
    if observation in {mint_0, mint_1, vault_0, vault_1}:
        raise RaydiumClmmPoolStateError("Raydium observation account aliases a mint or vault")
    if fee_on not in (0, 1, 2):
        raise RaydiumClmmPoolStateError("Raydium pool fee_on mode is invalid")
    if tick_spacing <= 0:
        raise RaydiumClmmPoolStateError("Raydium pool tick spacing must be positive")
    if liquidity <= 0 or sqrt_price_x64 <= 0:
        raise RaydiumClmmPoolStateError("Raydium pool liquidity and sqrt price must be positive")
    if not RAYDIUM_MIN_TICK <= tick_current <= RAYDIUM_MAX_TICK:
        raise RaydiumClmmPoolStateError("Raydium current tick is outside the protocol boundary")
    if status & RAYDIUM_SWAP_DISABLED_BIT:
        raise RaydiumClmmPoolStateError("Raydium pool has swaps disabled")
    if fee_on > RAYDIUM_FEE_ON_MAX:
        raise RaydiumClmmPoolStateError("Raydium pool fee_on mode is unknown")

    derived = derive_raydium_clmm_pool_address(
        amm_config=amm_config,
        token_mint_0=mint_0,
        token_mint_1=mint_1,
        bump=bump,
        seed_index=seed_index,
    )
    if derived != actual_key:
        raise RaydiumClmmPoolStateError("Raydium PoolState PDA does not match its config, mint pair, seed index and bump")

    return RaydiumClmmPoolState(
        pool_address=actual_key,
        bump=bump,
        amm_config=amm_config,
        pool_creator=pool_creator,
        token_mint_0=mint_0,
        token_mint_1=mint_1,
        token_vault_0=vault_0,
        token_vault_1=vault_1,
        observation_key=observation,
        mint_decimals_0=decimals_0,
        mint_decimals_1=decimals_1,
        tick_spacing=tick_spacing,
        liquidity=liquidity,
        sqrt_price_x64=sqrt_price_x64,
        tick_current=tick_current,
        status=status,
        fee_on=fee_on,
        seed_index=seed_index,
        account_data_sha256=account.data_sha256,
        fee_growth_global_0_x64=fee_growth_global_0_x64,
        fee_growth_global_1_x64=fee_growth_global_1_x64,
        protocol_fees_token_0=protocol_fees_token_0,
        protocol_fees_token_1=protocol_fees_token_1,
        fund_fees_token_0=fund_fees_token_0,
        fund_fees_token_1=fund_fees_token_1,
        tick_array_bitmap=tick_array_bitmap,
        dynamic_fee_info=dynamic_fee_info,
        reward_infos=tuple(reward_infos),
        account_data=data,
        source_account_data_sha256=account.data_sha256,
    )



def encode_raydium_clmm_pool_state(pool: RaydiumClmmPoolState) -> bytes:
    """Patch modeled PoolState fields into the verified source account bytes.

    All unmodeled and reserved bytes (including open_time, recent_epoch, and
    padding arrays) are preserved exactly from the verified source account.
    """
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmPoolStateError("decoded Raydium PoolState is required")
    if pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumClmmPoolStateError("PoolState source commit differs from the pinned Raydium source")
    if not isinstance(pool.account_data, bytes) or len(pool.account_data) != RAYDIUM_POOL_STATE_LENGTH:
        raise RaydiumClmmPoolStateError("verified original PoolState account bytes are required")
    source_hash = pool.source_account_data_sha256 or pool.account_data_sha256
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or hashlib.sha256(pool.account_data).hexdigest() != source_hash
    ):
        raise RaydiumClmmPoolStateError("original PoolState account bytes do not match their source commitment")
    if pool.account_data[:8] != RAYDIUM_POOL_STATE_DISCRIMINATOR:
        raise RaydiumClmmPoolStateError("original PoolState discriminator is invalid")

    def unsigned(value: int, size: int, field: str) -> bytes:
        if (
            isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value < (1 << (size * 8))
        ):
            raise RaydiumClmmPoolStateError(f"{field} is outside its unsigned serialized domain")
        return value.to_bytes(size, "little")

    def signed(value: int, size: int, field: str) -> bytes:
        if (
            isinstance(value, bool) or not isinstance(value, int)
            or not -(1 << (size * 8 - 1)) <= value < (1 << (size * 8 - 1))
        ):
            raise RaydiumClmmPoolStateError(f"{field} is outside its signed serialized domain")
        return value.to_bytes(size, "little", signed=True)

    def public_key(value: str, field: str) -> bytes:
        try:
            key = Pubkey.from_string(value)
            if str(key) != value:
                raise ValueError("non-canonical public key")
            return bytes(key)
        except Exception as exc:
            raise RaydiumClmmPoolStateError(f"{field} is not a canonical public key") from exc

    if derive_raydium_clmm_pool_address(
        amm_config=pool.amm_config,
        token_mint_0=pool.token_mint_0,
        token_mint_1=pool.token_mint_1,
        bump=pool.bump,
        seed_index=pool.seed_index,
    ) != pool.pool_address:
        raise RaydiumClmmPoolStateError("PoolState identity no longer matches its canonical PDA")
    if len(pool.tick_array_bitmap) != RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS:
        raise RaydiumClmmPoolStateError("PoolState bitmap must contain exactly 16 u64 words")
    if not isinstance(pool.reward_infos, tuple) or len(pool.reward_infos) != RAYDIUM_POOL_REWARD_COUNT:
        raise RaydiumClmmPoolStateError("PoolState must contain exactly three RewardInfo records")
    if not isinstance(pool.dynamic_fee_info, RaydiumClmmDynamicFeeInfo):
        raise RaydiumClmmPoolStateError("decoded dynamic-fee state is required")

    data = bytearray(pool.account_data)
    data[8:9] = unsigned(pool.bump, 1, "pool bump")
    for offset, value, field in (
        (9, pool.amm_config, "amm_config"),
        (41, pool.pool_creator, "pool_creator"),
        (73, pool.token_mint_0, "token_mint_0"),
        (105, pool.token_mint_1, "token_mint_1"),
        (137, pool.token_vault_0, "token_vault_0"),
        (169, pool.token_vault_1, "token_vault_1"),
        (201, pool.observation_key, "observation_key"),
    ):
        data[offset:offset + 32] = public_key(value, field)
    data[233:234] = unsigned(pool.mint_decimals_0, 1, "mint_decimals_0")
    data[234:235] = unsigned(pool.mint_decimals_1, 1, "mint_decimals_1")
    data[235:237] = unsigned(pool.tick_spacing, 2, "tick_spacing")
    data[237:253] = unsigned(pool.liquidity, 16, "liquidity")
    data[253:269] = unsigned(pool.sqrt_price_x64, 16, "sqrt_price_x64")
    data[269:273] = signed(pool.tick_current, 4, "tick_current")
    data[277:293] = unsigned(pool.fee_growth_global_0_x64, 16, "fee_growth_global_0_x64")
    data[293:309] = unsigned(pool.fee_growth_global_1_x64, 16, "fee_growth_global_1_x64")
    data[309:317] = unsigned(pool.protocol_fees_token_0, 8, "protocol_fees_token_0")
    data[317:325] = unsigned(pool.protocol_fees_token_1, 8, "protocol_fees_token_1")
    data[389:390] = unsigned(pool.status, 1, "status")
    data[390:391] = unsigned(pool.fee_on, 1, "fee_on")
    data[391:393] = unsigned(pool.seed_index, 2, "seed_index")

    for index, reward in enumerate(pool.reward_infos):
        if not isinstance(reward, RaydiumClmmRewardInfo):
            raise RaydiumClmmPoolStateError(f"reward_infos[{index}] is malformed")
        offset = RAYDIUM_POOL_REWARD_INFO_OFFSET + index * RAYDIUM_POOL_REWARD_INFO_LENGTH
        data[offset:offset + 1] = unsigned(reward.reward_state, 1, "reward_state")
        data[offset + 1:offset + 9] = unsigned(reward.open_time, 8, "reward open_time")
        data[offset + 9:offset + 17] = unsigned(reward.end_time, 8, "reward end_time")
        data[offset + 17:offset + 25] = unsigned(reward.last_update_time, 8, "reward last_update_time")
        data[offset + 25:offset + 41] = unsigned(reward.emissions_per_second_x64, 16, "reward emissions")
        data[offset + 41:offset + 49] = unsigned(reward.reward_total_emitted, 8, "reward total emitted")
        data[offset + 49:offset + 57] = unsigned(reward.reward_claimed, 8, "reward claimed")
        data[offset + 57:offset + 89] = public_key(reward.token_mint, "reward token mint")
        data[offset + 89:offset + 121] = public_key(reward.token_vault, "reward token vault")
        data[offset + 121:offset + 153] = public_key(reward.authority, "reward authority")
        data[offset + 153:offset + 169] = unsigned(reward.reward_growth_global_x64, 16, "reward growth")

    for index, word in enumerate(pool.tick_array_bitmap):
        data[RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + index * 8:
             RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + (index + 1) * 8] = unsigned(
            word, 8, "tick-array bitmap word"
        )
    data[1064:1072] = unsigned(pool.fund_fees_token_0, 8, "fund_fees_token_0")
    data[1072:1080] = unsigned(pool.fund_fees_token_1, 8, "fund_fees_token_1")

    dynamic = pool.dynamic_fee_info
    dynamic_offset = RAYDIUM_POOL_DYNAMIC_FEE_INFO_OFFSET
    dynamic_values = (
        (dynamic.filter_period, 2, "dynamic filter period", False),
        (dynamic.decay_period, 2, "dynamic decay period", False),
        (dynamic.reduction_factor, 2, "dynamic reduction factor", False),
        (dynamic.dynamic_fee_control, 4, "dynamic fee control", False),
        (dynamic.max_volatility_accumulator, 4, "maximum volatility accumulator", False),
        (dynamic.tick_spacing_index_reference, 4, "tick spacing index reference", True),
        (dynamic.volatility_reference, 4, "volatility reference", False),
        (dynamic.volatility_accumulator, 4, "volatility accumulator", False),
        (dynamic.last_update_timestamp, 8, "dynamic fee timestamp", False),
    )
    cursor = dynamic_offset
    for value, size, field, is_signed in dynamic_values:
        data[cursor:cursor + size] = (
            signed(value, size, field) if is_signed else unsigned(value, size, field)
        )
        cursor += size
    if not isinstance(dynamic.padding, bytes) or len(dynamic.padding) != 46:
        raise RaydiumClmmPoolStateError("dynamic-fee padding must contain exactly 46 bytes")
    data[cursor:cursor + 46] = dynamic.padding
    if len(data) != RAYDIUM_POOL_STATE_LENGTH:
        raise RaydiumClmmPoolStateError("serialized PoolState length differs from the pinned layout")
    return bytes(data)


def validate_raydium_clmm_pool_for_route(
    snapshot: DexPoolEvidenceSnapshot,
    route_inspection: JupiterV6RouteInspection,
    order: JupiterOrderTransaction,
) -> tuple[RaydiumClmmPoolRouteCheck, ...]:
    """Bind each Raydium CLMM quote hop to its owner-verified pool mint pair and swap state."""
    if not isinstance(snapshot, DexPoolEvidenceSnapshot):
        raise RaydiumClmmPoolStateError("verified DEX pool evidence snapshot is required")
    if not isinstance(route_inspection, JupiterV6RouteInspection):
        raise RaydiumClmmPoolStateError("Jupiter V6 route inspection is required")
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmPoolStateError("Jupiter order quote is required to bind route-hop mints")
    order_digest = hashlib.sha256(order.message_bytes).hexdigest()
    if (
        snapshot.route_message_sha256 != route_inspection.message_sha256
        or order_digest != route_inspection.message_sha256
    ):
        raise RaydiumClmmPoolStateError("pool evidence, Jupiter order and route inspection message digests differ")
    by_address = {account.amm_key: account for account in snapshot.accounts}
    if len(by_address) != len(snapshot.accounts):
        raise RaydiumClmmPoolStateError("pool evidence contains duplicate AMM addresses")
    if len(route_inspection.route_steps) != len(order.route_plan):
        raise RaydiumClmmPoolStateError("decoded route steps differ in count from the Jupiter quote")
    checks = []
    for step, quote_step in zip(route_inspection.route_steps, order.route_plan):
        if step.amm_key != quote_step.amm_key or step.dex_label != quote_step.label:
            raise RaydiumClmmPoolStateError("decoded route step AMM key or DEX label differs from the Jupiter quote")
        if step.dex_label != "Raydium CLMM":
            continue
        account = by_address.get(step.amm_key)
        if account is None:
            raise RaydiumClmmPoolStateError("Raydium route step has no matching pool account evidence")
        pool = decode_raydium_clmm_pool_state(account, expected_pool_address=step.amm_key)
        if {quote_step.input_mint, quote_step.output_mint} != {pool.token_mint_0, pool.token_mint_1}:
            raise RaydiumClmmPoolStateError("Raydium pool token mints do not match the quoted route hop")
        checks.append(RaydiumClmmPoolRouteCheck(
            passed=True,
            pool=pool,
            route_input_mint=quote_step.input_mint,
            route_output_mint=quote_step.output_mint,
        ))
    if not checks:
        raise RaydiumClmmPoolStateError("route contains no supported Raydium CLMM hop")
    return tuple(checks)
