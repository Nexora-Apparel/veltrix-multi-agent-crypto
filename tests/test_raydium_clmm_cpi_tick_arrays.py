"""Adversarial tests for CPI-ordered Raydium CLMM tick-array evidence."""
import base64
import dataclasses
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence, DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_bitmap_traversal import (
    RaydiumClmmBitmapTraversalError,
    verify_raydium_clmm_bitmap_traversal,
)
from veltrix.raydium_clmm_cpi_tick_arrays import (
    TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
    TICK_ARRAY_BITMAP_EXTENSION_DATA_LENGTH,
    RaydiumClmmCpiTickArrayError,
    derive_raydium_clmm_tick_array_bitmap_extension_address,
    fetch_raydium_clmm_cpi_tick_array_snapshot,
    validate_raydium_clmm_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_POOL_STATE_LENGTH,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET,
    RaydiumClmmPoolRouteCheck,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    derive_raydium_clmm_tick_array_address,
    RaydiumClmmTickArray,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, accounts, *, slot=503):
        self.accounts = accounts
        self.slot = slot
        self.calls = []

    def post(self, endpoint, *, json, headers, timeout, allow_redirects):
        self.calls.append(json)
        if json["method"] == "getGenesisHash":
            result = "genesis"
        elif json["method"] == "getMultipleAccounts":
            result = {
                "context": {"slot": self.slot},
                "value": [self.accounts.get(address) for address in json["params"][0]],
            }
        else:
            raise AssertionError(json["method"])
        return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": result})


def tick_array_data(pool, start):
    data = bytearray(RAYDIUM_TICK_ARRAY_DATA_LENGTH)
    data[:8] = RAYDIUM_TICK_ARRAY_DISCRIMINATOR
    data[8:40] = bytes(Pubkey.from_string(pool))
    data[40:44] = start.to_bytes(4, "little", signed=True)
    data[44:48] = start.to_bytes(4, "little", signed=True)
    data[64:80] = (1).to_bytes(16, "little")
    data[10_124] = 1
    data[10_125:10_133] = (1).to_bytes(8, "little")
    return bytes(data)


def account(data):
    return {
        "owner": RAYDIUM_CLMM_PROGRAM_ID,
        "lamports": 1,
        "executable": False,
        "data": [base64.b64encode(data).decode(), "base64"],
        "space": len(data),
    }


def fixture(*, reverse=False, include_bitmap=True, slot=503, bad_bitmap_pool=False):
    key = lambda: str(Pubkey.new_unique())
    taker, mint0, mint1, pool_address = key(), key(), key(), key()
    amm_config, observation = key(), key()
    tick0 = derive_raydium_clmm_tick_array_address(pool_address=pool_address, start_tick_index=0)
    tick1 = derive_raydium_clmm_tick_array_address(pool_address=pool_address, start_tick_index=-600)
    bitmap = derive_raydium_clmm_tick_array_bitmap_extension_address(pool_address=pool_address)
    message = b"raydium-cpi-traversal-message"
    digest = hashlib.sha256(message).hexdigest()
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2", request_id="order", quote_id="quote",
        observed_at_epoch_ms=1000, taker=taker, fee_payer=taker,
        input_mint=mint0, output_mint=mint1, input_amount_raw=100,
        output_amount_raw=190, minimum_output_amount_raw=180,
        price_impact_pct=0.1, fee_bps=0.0, router="metis", transaction_version=0,
        last_valid_block_height=950, recent_blockhash="blockhash",
        message_bytes=message, serialized_transaction_bytes=b"unsigned",
        route_plan=(JupiterRouteStep(pool_address, "Raydium CLMM", mint0, mint1, 100, 190, 100.0, 10_000),),
        signature_fee_lamports=5000, prioritization_fee_lamports=0, rent_fee_lamports=0,
    )
    pool_words = (0,) * 16
    pool_data_mut = bytearray(RAYDIUM_POOL_STATE_LENGTH)
    pool_data_mut[73:105] = bytes(Pubkey.from_string(mint0))
    pool_data_mut[105:137] = bytes(Pubkey.from_string(mint1))
    pool_data_mut[235:237] = (10).to_bytes(2, "little")
    pool_data_mut[269:273] = (5).to_bytes(4, "little", signed=True)
    for index, word in enumerate(pool_words):
        offset = RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + index * 8
        pool_data_mut[offset:offset + 8] = word.to_bytes(8, "little")
    pool_data = bytes(pool_data_mut)
    pool_hash = hashlib.sha256(pool_data).hexdigest()
    pool = RaydiumClmmPoolState(
        pool_address=pool_address, bump=1, amm_config=amm_config,
        pool_creator=key(), token_mint_0=mint0, token_mint_1=mint1,
        token_vault_0=key(), token_vault_1=key(), observation_key=observation,
        mint_decimals_0=6, mint_decimals_1=6, tick_spacing=10,
        liquidity=1000, sqrt_price_x64=1 << 64, tick_current=5,
        status=0, fee_on=0, seed_index=0, account_data_sha256=pool_hash,
        tick_array_bitmap=pool_words,
    )
    pool_check = RaydiumClmmPoolRouteCheck(True, pool, mint0, mint1)
    pool_account = DexPoolAccountEvidence(
        amm_key=pool_address, dex_label="Raydium CLMM",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1, executable=False,
        data_length=len(pool_data), data_sha256=pool_hash, data_bytes=pool_data,
    )
    pool_evidence = DexPoolEvidenceSnapshot(
        route_message_sha256=digest, registry_sha256="registry",
        simulation_response_id="sim-cpi", simulation_context_slot=500,
        provider_id="rpc-primary", cluster="mainnet-beta", genesis_hash="genesis",
        context_slot=501, accounts=(pool_account,),
    )
    vaults = RaydiumClmmVaultSnapshot(
        route_message_sha256=digest, simulation_response_id="sim-cpi",
        simulation_context_slot=500, provider_id="rpc-primary",
        cluster="mainnet-beta", genesis_hash="genesis", context_slot=502, vaults=(),
    )
    simulation = SimulationEvidence(
        message_sha256=digest, recent_blockhash="blockhash",
        cluster="mainnet-beta", genesis_hash="genesis", provider_id="rpc-primary",
        response_id="sim-cpi", observed_at_epoch_ms=1000, context_slot=500,
        current_block_height=900, last_valid_block_height=950, simulation_error=None,
        units_consumed=100_000, fee_lamports=5000, token_balances=(),
    )
    bitmap_data = bytearray(TICK_ARRAY_BITMAP_EXTENSION_DATA_LENGTH)
    bitmap_data[:8] = TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR
    bitmap_data[8:40] = bytes(Pubkey.from_string(key() if bad_bitmap_pool else pool_address))
    accounts = {
        tick0: account(tick_array_data(pool_address, 0)),
        tick1: account(tick_array_data(pool_address, -600)),
    }
    cpi_order = (tick0, bitmap, tick1) if include_bitmap else (tick0, tick1)
    if include_bitmap:
        accounts[bitmap] = account(bytes(bitmap_data))
    if reverse:
        cpi_order = tuple(reversed(cpi_order))
    cpi = RaydiumClmmCpiResult(
        passed=True, message_sha256=digest, program_id=RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
        outer_instruction_index=0, cpi_instruction_index=0, payer=taker,
        input_token_account=key(), output_token_account=key(),
        amm_config=amm_config, pool_state=pool_address, input_vault=pool.token_vault_0,
        output_vault=pool.token_vault_1, observation_state=observation,
        tick_array_accounts=cpi_order, amount_raw=100, minimum_output_raw=180,
        sqrt_price_limit_x64=1, is_base_input=True, stack_height=2,
        source_commit=RAYDIUM_CLMM_SOURCE_COMMIT,
    )
    session = FakeSession(accounts, slot=slot)
    adapter = SolanaRpcSimulationAdapter(
        endpoint="https://rpc.example.test", provider_id="rpc-primary",
        cluster="mainnet-beta", expected_genesis_hash="genesis",
        allowed_hosts=("rpc.example.test",), session=session, clock_ms=lambda: 1000,
    )
    return order, cpi, pool_evidence, pool_check, vaults, simulation, adapter, session, bitmap


def fetch(values=None):
    order, cpi, pool_evidence, pool_check, vaults, simulation, adapter, *_ = values or fixture()
    return fetch_raydium_clmm_cpi_tick_array_snapshot(
        order, cpi=cpi, pool_evidence=pool_evidence, pool_check=pool_check,
        vault_snapshot=vaults, simulation=simulation, rpc_adapter=adapter,
    )


def test_fetches_all_cpi_tick_arrays_in_exact_order_and_bitmap_extension():
    values = fixture()
    result = fetch(values)
    assert result.execution_ready is False
    assert result.cpi_account_order == values[1].tick_array_accounts
    assert tuple(item.start_tick_index for item in result.tick_arrays) == (0, -600)
    assert result.bitmap_extension_address == values[8]
    assert result.bitmap_extension_sha256 is not None
    assert len(result.bitmap_extension_sha256) == 64
    request = next(call for call in values[7].calls if call["method"] == "getMultipleAccounts")
    assert request["params"][1]["minContextSlot"] == 502


def test_fetches_all_tick_arrays_when_no_bitmap_extension_is_required():
    values = fixture(include_bitmap=False)
    result = fetch(values)
    assert result.bitmap_extension_address is None
    assert len(result.tick_arrays) == 2


def test_rejects_nonmonotonic_cpi_tick_array_order():
    values = fixture(reverse=True, include_bitmap=False)
    with pytest.raises(RaydiumClmmCpiTickArrayError, match="strictly ordered"):
        fetch(values)


def test_rejects_wrong_bitmap_pool_and_stale_account_snapshot():
    values = fixture(bad_bitmap_pool=True)
    with pytest.raises(RaydiumClmmCpiTickArrayError, match="different pool"):
        fetch(values)
    values = fixture(slot=501)
    with pytest.raises(Exception, match="older than the required simulation slot"):
        fetch(values)


def test_rejects_wrong_bitmap_owner_length_and_discriminator():
    values = fixture()
    with pytest.raises(RaydiumClmmCpiTickArrayError, match="owner"):
        validate_raydium_clmm_tick_array_bitmap_extension(
            address=values[8], owner=str(Pubkey.new_unique()), lamports=1,
            executable=False, data=bytes(TICK_ARRAY_BITMAP_EXTENSION_DATA_LENGTH),
            expected_pool_address=values[3].pool.pool_address,
        )
    with pytest.raises(RaydiumClmmCpiTickArrayError, match="discriminator"):
        validate_raydium_clmm_tick_array_bitmap_extension(
            address=values[8], owner=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=bytes(TICK_ARRAY_BITMAP_EXTENSION_DATA_LENGTH),
            expected_pool_address=values[3].pool.pool_address,
        )


def _sync_pool_account(values, pool_check):
    pool = pool_check.pool
    evidence = values[2]
    account = evidence.accounts[0]
    raw = bytearray(account.data_bytes)
    raw[73:105] = bytes(Pubkey.from_string(pool.token_mint_0))
    raw[105:137] = bytes(Pubkey.from_string(pool.token_mint_1))
    raw[235:237] = pool.tick_spacing.to_bytes(2, "little")
    raw[269:273] = pool.tick_current.to_bytes(4, "little", signed=True)
    for index, word in enumerate(pool.tick_array_bitmap):
        offset = RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + index * 8
        raw[offset:offset + 8] = word.to_bytes(8, "little")
    data = bytes(raw)
    digest = hashlib.sha256(data).hexdigest()
    pool = dataclasses.replace(pool, account_data_sha256=digest)
    pool_check = dataclasses.replace(pool_check, pool=pool)
    updated_account = dataclasses.replace(
        account, data_bytes=data, data_length=len(data), data_sha256=digest
    )
    evidence = dataclasses.replace(evidence, accounts=(updated_account,))
    return pool_check, evidence


def _pool_check_with_initialized_arrays(values, starts):
    pool_check = values[3]
    words = [0] * 16
    tick_count = 60 * pool_check.pool.tick_spacing
    for start in starts:
        bit = start // tick_count + 512
        words[bit // 64] |= 1 << (bit % 64)
    pool = dataclasses.replace(pool_check.pool, tick_array_bitmap=tuple(words))
    return _sync_pool_account(values, dataclasses.replace(pool_check, pool=pool))


def _synthetic_array(pool_address, start, tick_spacing=10):
    address = derive_raydium_clmm_tick_array_address(
        pool_address=pool_address, start_tick_index=start
    )
    return RaydiumClmmTickArray(
        address=address, pool_address=pool_address, start_tick_index=start,
        tick_spacing=tick_spacing, initialized_tick_count=0, recent_epoch=1,
        ticks=(), data_sha256="a" * 64,
    )


def test_bitmap_traversal_proves_every_initialized_boundary_in_cpi_span():
    values = fixture(include_bitmap=False)
    snapshot = fetch(values)
    pool_check, pool_evidence = _pool_check_with_initialized_arrays(values, (0, -600))
    result = verify_raydium_clmm_bitmap_traversal(
        values[0], pool_check=pool_check, pool_evidence=pool_evidence,
        tick_array_snapshot=snapshot
    )
    assert result.passed is True
    assert result.current_array_start_index == 0
    assert result.first_initialized_array_start_index == 0
    assert result.expected_initialized_array_starts == (0, -600)
    assert result.bounded_interval_complete is True
    assert result.execution_ready is False


def test_bitmap_traversal_rejects_skipped_initialized_array_between_cpi_accounts():
    values = fixture(include_bitmap=False)
    snapshot = fetch(values)
    pool_check, pool_evidence = _pool_check_with_initialized_arrays(values, (0, -600, -1200))
    first = _synthetic_array(pool_check.pool.pool_address, 0)
    last = _synthetic_array(pool_check.pool.pool_address, -1200)
    changed = dataclasses.replace(
        snapshot,
        tick_arrays=(first, last),
        cpi_account_order=(first.address, last.address),
    )
    with pytest.raises(RaydiumClmmBitmapTraversalError, match="omit initialized"):
        verify_raydium_clmm_bitmap_traversal(
            values[0], pool_check=pool_check, pool_evidence=pool_evidence,
            tick_array_snapshot=changed
        )


def _extension_with_bitmaps(extension, *, positive=None, negative=None):
    positive = tuple(positive if positive is not None else extension.positive_bitmaps)
    negative = tuple(negative if negative is not None else extension.negative_bitmaps)
    raw = bytearray(extension.data_bytes)
    words_per_side = len(positive)
    for index, word in enumerate(positive):
        raw[40 + index * 8:48 + index * 8] = word.to_bytes(8, "little")
    negative_start = 40 + words_per_side * 8
    for index, word in enumerate(negative):
        raw[negative_start + index * 8:negative_start + (index + 1) * 8] = word.to_bytes(8, "little")
    raw = bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return dataclasses.replace(
        extension, positive_bitmaps=positive, negative_bitmaps=negative,
        data_bytes=raw, data_sha256=digest,
    )


def test_bitmap_traversal_uses_positive_extension_bitmap_outside_default_range():
    values = fixture()
    order, _, _, pool_check, *_ = values
    pool = dataclasses.replace(pool_check.pool, tick_current=307200)
    pool_check = dataclasses.replace(
        pool_check, pool=pool, route_input_mint=pool.token_mint_1,
        route_output_mint=pool.token_mint_0,
    )
    order = dataclasses.replace(order, input_mint=pool.token_mint_1, output_mint=pool.token_mint_0)
    pool_check, pool_evidence = _sync_pool_account(values, pool_check)
    snapshot = fetch(values)
    extension = snapshot.bitmap_extension
    positive = [0] * len(extension.positive_bitmaps)
    positive[0] = 0b11  # +307200 and +307800
    extension = _extension_with_bitmaps(extension, positive=positive)
    first = _synthetic_array(pool.pool_address, 307200)
    second = _synthetic_array(pool.pool_address, 307800)
    snapshot = dataclasses.replace(
        snapshot, tick_arrays=(first, second),
        cpi_account_order=(first.address, snapshot.bitmap_extension_address, second.address),
        bitmap_extension=extension,
        bitmap_extension_sha256=extension.data_sha256,
    )
    result = verify_raydium_clmm_bitmap_traversal(
        order, pool_check=pool_check, pool_evidence=pool_evidence,
        tick_array_snapshot=snapshot
    )
    assert result.expected_initialized_array_starts == (307200, 307800)
    assert result.zero_for_one is False


def test_bitmap_traversal_uses_negative_extension_bitmap_outside_default_range():
    values = fixture()
    order, _, _, pool_check, *_ = values
    pool = dataclasses.replace(pool_check.pool, tick_current=-307800)
    pool_check = dataclasses.replace(
        pool_check, pool=pool, route_input_mint=pool.token_mint_0,
        route_output_mint=pool.token_mint_1,
    )
    order = dataclasses.replace(order, input_mint=pool.token_mint_0, output_mint=pool.token_mint_1)
    pool_check, pool_evidence = _sync_pool_account(values, pool_check)
    snapshot = fetch(values)
    extension = snapshot.bitmap_extension
    negative = [0] * len(extension.negative_bitmaps)
    negative[7] = 1 << (511 % 64)  # block 0, bit 511: -307800
    negative[7] |= 1 << (510 % 64)  # block 0, bit 510: -308400
    extension = _extension_with_bitmaps(extension, negative=negative)
    first = _synthetic_array(pool.pool_address, -307800)
    second = _synthetic_array(pool.pool_address, -308400)
    snapshot = dataclasses.replace(
        snapshot, tick_arrays=(first, second),
        cpi_account_order=(first.address, snapshot.bitmap_extension_address, second.address),
        bitmap_extension=extension,
        bitmap_extension_sha256=extension.data_sha256,
    )
    result = verify_raydium_clmm_bitmap_traversal(
        order, pool_check=pool_check, tick_array_snapshot=snapshot
    )
    assert result.expected_initialized_array_starts == (-307800, -308400)
    assert result.zero_for_one is True
