"""Adversarial tests for the pinned Raydium CLMM PoolState header decoder."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence, DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_v6_route import DecodedJupiterRouteStep, JupiterV6RouteInspection
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolStateError,
    decode_raydium_clmm_pool_state,
    derive_raydium_clmm_pool_address,
    validate_raydium_clmm_pool_for_route,
)


def fixture():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint_a, mint_b = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    creator = Pubkey.new_unique()
    vault_a, vault_b = Pubkey.new_unique(), Pubkey.new_unique()
    observation = Pubkey.new_unique()
    seeds = [b"pool", bytes(config), bytes(mint_a), bytes(mint_b), b""]
    pool_key, bump = Pubkey.find_program_address(seeds, program)
    data = bytearray(RAYDIUM_POOL_STATE_LENGTH)
    data[:8] = RAYDIUM_POOL_STATE_DISCRIMINATOR
    data[8] = bump
    for offset, key in (
        (9, config), (41, creator), (73, mint_a), (105, mint_b),
        (137, vault_a), (169, vault_b), (201, observation),
    ):
        data[offset:offset + 32] = bytes(key)
    data[233] = 6
    data[234] = 9
    struct.pack_into("<H", data, 235, 10)
    data[237:253] = (1_000_000).to_bytes(16, "little")
    data[253:269] = (1 << 64).to_bytes(16, "little")
    struct.pack_into("<i", data, 269, 0)
    data[389] = 0
    data[390] = 0
    data[391:393] = (0).to_bytes(2, "little")
    raw = bytes(data)
    address = str(pool_key)
    evidence = DexPoolAccountEvidence(
        amm_key=address,
        dex_label="Raydium CLMM",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=2_000_000,
        executable=False,
        data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(),
        data_bytes=raw,
    )
    digest = hashlib.sha256(b"test-message").hexdigest()
    route_step = DecodedJupiterRouteStep(
        swap_variant=26,
        dex_label="Raydium CLMM",
        percent=100,
        input_index=0,
        output_index=1,
        amm_key=address,
    )
    inspection = JupiterV6RouteInspection(
        message_sha256=digest,
        instruction_name="route",
        program_id="JupiterV6Program",
        shared_authority_id=None,
        route_steps=(route_step,),
        input_amount_raw=1_000,
        quoted_output_amount_raw=2_000,
        slippage_bps=50,
        platform_fee_bps=0,
        remaining_account_count=0,
        fully_decoded=True,
        blocking_reasons=(),
    )
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="test-order",
        quote_id=None,
        observed_at_epoch_ms=1,
        taker="test-taker",
        fee_payer="test-taker",
        input_mint=str(mint_a),
        output_mint=str(mint_b),
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        minimum_output_amount_raw=1_950,
        price_impact_pct=0.0,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=1,
        recent_blockhash="test-blockhash",
        message_bytes=b"test-message",
        serialized_transaction_bytes=b"test-transaction",
        route_plan=(
            JupiterRouteStep(
                amm_key=address,
                label="Raydium CLMM",
                input_mint=str(mint_a),
                output_mint=str(mint_b),
                input_amount_raw=1_000,
                output_amount_raw=2_000,
                percent=100.0,
                bps=10_000,
            ),
        ),
        signature_fee_lamports=1,
        prioritization_fee_lamports=0,
        rent_fee_lamports=0,
    )
    snapshot = DexPoolEvidenceSnapshot(
        route_message_sha256=digest,
        registry_sha256="b" * 64,
        simulation_response_id="sim-1",
        simulation_context_slot=500,
        provider_id="rpc-primary",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        context_slot=500,
        accounts=(evidence,),
        execution_ready=False,
    )
    return raw, evidence, snapshot, inspection, order, address, str(mint_a), str(mint_b)


def test_decodes_pinned_pool_state_header_and_rederives_pda():
    _, evidence, _, _, _, pool_address, mint_a, mint_b = fixture()
    decoded = decode_raydium_clmm_pool_state(evidence, expected_pool_address=pool_address)
    assert decoded.pool_address == pool_address
    assert decoded.token_mint_0 == mint_a
    assert decoded.token_mint_1 == mint_b
    assert decoded.tick_spacing == 10
    assert decoded.liquidity == 1_000_000
    assert decoded.sqrt_price_x64 == 1 << 64
    assert decoded.swaps_enabled is True
    assert decoded.source_commit == "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"


def test_validates_route_hop_mint_pair_in_either_direction():
    _, _, snapshot, inspection, order, *_ = fixture()
    result = validate_raydium_clmm_pool_for_route(snapshot, inspection, order)
    assert len(result) == 1 and result[0].passed is True
    reverse_order = dataclasses.replace(
        order,
        input_mint=order.output_mint,
        output_mint=order.input_mint,
        route_plan=(dataclasses.replace(
            order.route_plan[0],
            input_mint=order.output_mint,
            output_mint=order.input_mint,
        ),),
    )
    assert validate_raydium_clmm_pool_for_route(snapshot, inspection, reverse_order)[0].passed is True


@pytest.mark.parametrize(
    ("offset", "value", "message"),
    [
        (0, b"badmagic", "discriminator"),
        (389, b"\x10", "swaps disabled"),
        (390, b"\x03", "fee_on"),
        (235, b"\x00\x00", "tick spacing"),
        (237, b"\x00" * 16, "liquidity"),
        (253, b"\x00" * 16, "sqrt price"),
        (269, struct.pack("<i", 500_000), "tick"),
    ],
)
def test_rejects_invalid_pool_state_header_fields(offset, value, message):
    _, evidence, _, _, _, pool_address, *_ = fixture()
    raw = bytearray(evidence.data_bytes)
    raw[offset:offset + len(value)] = value
    changed = dataclasses.replace(
        evidence,
        data_bytes=bytes(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(),
        data_length=len(raw),
    )
    with pytest.raises(RaydiumClmmPoolStateError, match=message):
        decode_raydium_clmm_pool_state(changed, expected_pool_address=pool_address)


def test_rejects_wrong_pool_pda_or_owner_evidence():
    _, evidence, _, _, _, pool_address, *_ = fixture()
    changed_data = bytearray(evidence.data_bytes)
    changed_data[8] = (changed_data[8] + 1) % 256
    changed = dataclasses.replace(
        evidence,
        data_bytes=bytes(changed_data),
        data_sha256=hashlib.sha256(changed_data).hexdigest(),
    )
    with pytest.raises(RaydiumClmmPoolStateError, match="bump|PDA"):
        decode_raydium_clmm_pool_state(changed, expected_pool_address=pool_address)
    wrong_owner = dataclasses.replace(evidence, owner_program_id=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumClmmPoolStateError, match="owner"):
        decode_raydium_clmm_pool_state(wrong_owner, expected_pool_address=pool_address)


def test_rejects_data_hash_mismatch_and_wrong_layout_size():
    _, evidence, _, _, _, pool_address, *_ = fixture()
    changed = dataclasses.replace(evidence, data_bytes=evidence.data_bytes + b"x")
    with pytest.raises(RaydiumClmmPoolStateError, match="length"):
        decode_raydium_clmm_pool_state(changed, expected_pool_address=pool_address)
    changed = dataclasses.replace(evidence, data_sha256="0" * 64)
    with pytest.raises(RaydiumClmmPoolStateError, match="hash"):
        decode_raydium_clmm_pool_state(changed, expected_pool_address=pool_address)


def test_rejects_noncanonical_mint_order_and_aliasing_vaults():
    _, evidence, _, _, _, pool_address, *_ = fixture()
    raw = bytearray(evidence.data_bytes)
    mint0 = bytes(Pubkey.new_unique())
    mint1 = bytes(Pubkey.new_unique())
    if mint0 < mint1:
        mint0, mint1 = mint1, mint0
    raw[73:105], raw[105:137] = mint0, mint1
    changed = dataclasses.replace(evidence, data_bytes=bytes(raw), data_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(RaydiumClmmPoolStateError, match="mint order"):
        decode_raydium_clmm_pool_state(changed, expected_pool_address=pool_address)

    raw = bytearray(evidence.data_bytes)
    raw[169:201] = raw[137:169]
    changed = dataclasses.replace(evidence, data_bytes=bytes(raw), data_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(RaydiumClmmPoolStateError, match="vault identities"):
        decode_raydium_clmm_pool_state(changed, expected_pool_address=pool_address)


def test_rejects_route_mints_that_do_not_match_pool():
    _, _, snapshot, inspection, order, *_ = fixture()
    wrong = dataclasses.replace(
        order.route_plan[0],
        input_mint=str(Pubkey.new_unique()),
        output_mint=str(Pubkey.new_unique()),
    )
    with pytest.raises(RaydiumClmmPoolStateError, match="mints do not match"):
        validate_raydium_clmm_pool_for_route(
            snapshot,
            inspection,
            dataclasses.replace(order, route_plan=(wrong,)),
        )


def test_rejects_duplicate_pool_evidence_and_missing_raydium_pool():
    _, evidence, snapshot, inspection, order, *_ = fixture()
    duplicate = dataclasses.replace(snapshot, accounts=(evidence, evidence))
    with pytest.raises(RaydiumClmmPoolStateError, match="duplicate"):
        validate_raydium_clmm_pool_for_route(duplicate, inspection, order)
    missing = dataclasses.replace(snapshot, accounts=())
    with pytest.raises(RaydiumClmmPoolStateError, match="no matching pool"):
        validate_raydium_clmm_pool_for_route(missing, inspection, order)


def test_rejects_mismatched_route_message_digest():
    _, _, snapshot, inspection, order, *_ = fixture()
    with pytest.raises(RaydiumClmmPoolStateError, match="digests differ"):
        validate_raydium_clmm_pool_for_route(
            dataclasses.replace(snapshot, route_message_sha256="0" * 64),
            inspection,
            order,
        )


def test_pool_pda_seed_index_is_included_in_seed_derivation():
    _, evidence, _, _, _, pool_address, *_ = fixture()
    decoded = decode_raydium_clmm_pool_state(evidence, expected_pool_address=pool_address)
    assert decoded.seed_index == 0
    wrong = derive_raydium_clmm_pool_address(
        amm_config=decoded.amm_config,
        token_mint_0=decoded.token_mint_0,
        token_mint_1=decoded.token_mint_1,
        bump=decoded.bump,
        seed_index=1,
    )
    assert wrong != pool_address
