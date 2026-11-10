"""Adversarial tests for Pump.fun v1/v2/v3 Jupiter CPI decoding."""
import dataclasses
import hashlib
import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_instruction_decoder import DecodedJupiterRouteStep, JupiterSwapInstructionResult
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_swap_effects import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    CLASSIC_TOKEN_PROGRAM_ID,
    derive_associated_token_address,
)
from veltrix.pumpfun_cpi_decoder import (
    PUMP_FEE_PROGRAM_ID,
    PUMP_GLOBAL_DISCRIMINATOR,
    PUMP_INSTRUCTION_DISCRIMINATORS,
    PUMP_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    WRAPPED_SOL_MINT,
    PumpFunCpiDecodeError,
    PumpFunCpiPolicy,
    decode_pumpfun_global_config,
    decode_pumpfun_route_cpis,
)
from veltrix.simulation_gate import SimulatedAccountSnapshot, SimulatedInnerInstruction, SimulationEvidence
from veltrix.transaction_validator import TransactionValidationResult


JUPITER_PROGRAM_ID = str(Pubkey.new_unique())


def pda(seeds, program):
    address, _ = Pubkey.find_program_address(list(seeds), Pubkey.from_string(program))
    return str(address)


def encode(name, *values):
    discriminator = next(key for key, spec in PUMP_INSTRUCTION_DISCRIMINATORS.items() if spec[0] == name)
    payload = values[:2][0].to_bytes(8, "little") + values[:2][1].to_bytes(8, "little")
    if name in {"buy", "buy_exact_sol_in"}:
        payload += bytes([values[2], values[3]])
    elif name in {"buy_v2", "buy_exact_quote_in_v2", "buy_v3", "buy_exact_quote_in_v3"}:
        payload += bytes([values[2]])
    return discriminator + payload



def global_account_data(fee_recipient, buyback_recipient, *, initialized=True):
    default = bytes(Pubkey.default())
    authority = bytes(Pubkey.new_unique())
    return b"".join([
        PUMP_GLOBAL_DISCRIMINATOR,
        bytes([1 if initialized else 0]),
        authority,
        bytes(Pubkey.from_string(fee_recipient)),
        (0).to_bytes(8, "little") * 5,
        bytes(Pubkey.new_unique()),
        bytes([0]),
        (0).to_bytes(8, "little"),
        (0).to_bytes(8, "little"),
        bytes(Pubkey.from_string(fee_recipient)),
        default * 6,
        bytes(Pubkey.new_unique()),
        bytes(Pubkey.new_unique()),
        bytes([0]),
        bytes(Pubkey.new_unique()),
        default,
        bytes([0]),
        default * 7,
        bytes([0]),
        bytes(Pubkey.from_string(buyback_recipient)),
        default * 7,
        (0).to_bytes(8, "little"),
        (0).to_bytes(8, "little"),
        default,
        bytes([0]),
        (0).to_bytes(8, "little"),
        bytes(Pubkey.new_unique()),
        bytes([0]),
        bytes([0]),
    ])

def build_case(*, version="v1", name="buy_exact_sol_in", direction="buy", data_override=None):
    taker = str(Pubkey.new_unique())
    base_mint = str(Pubkey.new_unique())
    quote_mint = WRAPPED_SOL_MINT if version == "v1" else str(Pubkey.new_unique())
    input_mint = quote_mint if direction == "buy" else base_mint
    output_mint = base_mint if direction == "buy" else quote_mint
    amm_key = str(Pubkey.new_unique())
    fee_recipient = str(Pubkey.new_unique())
    buyback_recipient = str(Pubkey.new_unique())
    creator_vault = str(Pubkey.new_unique())
    global_pda = pda((b"global",), PUMP_PROGRAM_ID)
    event_pda = pda((b"__event_authority",), PUMP_PROGRAM_ID)
    global_volume_pda = pda((b"global_volume_accumulator",), PUMP_PROGRAM_ID)
    user_volume_pda = pda((b"user_volume_accumulator", bytes(Pubkey.from_string(taker))), PUMP_PROGRAM_ID)
    fee_config_pda = pda((b"fee_config", bytes(Pubkey.from_string(PUMP_PROGRAM_ID))), PUMP_FEE_PROGRAM_ID)
    token_program = CLASSIC_TOKEN_PROGRAM_ID
    system_program = SYSTEM_PROGRAM_ID
    ata_program = ASSOCIATED_TOKEN_PROGRAM_ID

    if version == "v1":
        if direction == "buy":
            accounts = [
                global_pda, fee_recipient, base_mint, amm_key,
                derive_associated_token_address(amm_key, base_mint),
                derive_associated_token_address(taker, base_mint),
                taker, system_program, token_program, creator_vault,
                event_pda, PUMP_PROGRAM_ID, global_volume_pda,
                user_volume_pda, fee_config_pda, PUMP_FEE_PROGRAM_ID,
            ]
            writable_positions, signer_positions = {1, 3, 4, 5, 6, 9, 13}, {6}
        else:
            accounts = [
                global_pda, fee_recipient, base_mint, amm_key,
                derive_associated_token_address(amm_key, base_mint),
                derive_associated_token_address(taker, base_mint),
                taker, system_program, creator_vault, token_program,
                event_pda, PUMP_PROGRAM_ID, fee_config_pda, PUMP_FEE_PROGRAM_ID,
            ]
            writable_positions, signer_positions = {1, 3, 4, 5, 6, 8}, {6}
    elif version == "v2":
        sharing_config = pda((b"sharing-config", bytes(Pubkey.from_string(base_mint))), PUMP_FEE_PROGRAM_ID)
        if direction == "buy":
            accounts = [
                global_pda, base_mint, quote_mint, token_program, token_program, ata_program,
                fee_recipient, derive_associated_token_address(fee_recipient, quote_mint),
                buyback_recipient, derive_associated_token_address(buyback_recipient, quote_mint),
                amm_key, derive_associated_token_address(amm_key, base_mint),
                derive_associated_token_address(amm_key, quote_mint), taker,
                derive_associated_token_address(taker, base_mint),
                derive_associated_token_address(taker, quote_mint), creator_vault,
                derive_associated_token_address(creator_vault, quote_mint), sharing_config,
                global_volume_pda, user_volume_pda,
                derive_associated_token_address(user_volume_pda, quote_mint),
                fee_config_pda, PUMP_FEE_PROGRAM_ID, system_program, event_pda, PUMP_PROGRAM_ID,
            ]
            writable_positions, signer_positions = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 20, 21}, {13}
        else:
            accounts = [
                global_pda, base_mint, quote_mint, token_program, token_program, ata_program,
                fee_recipient, derive_associated_token_address(fee_recipient, quote_mint),
                buyback_recipient, derive_associated_token_address(buyback_recipient, quote_mint),
                amm_key, derive_associated_token_address(amm_key, base_mint),
                derive_associated_token_address(amm_key, quote_mint), taker,
                derive_associated_token_address(taker, base_mint),
                derive_associated_token_address(taker, quote_mint), creator_vault,
                derive_associated_token_address(creator_vault, quote_mint), sharing_config,
                user_volume_pda, derive_associated_token_address(user_volume_pda, quote_mint),
                fee_config_pda, PUMP_FEE_PROGRAM_ID, system_program, event_pda, PUMP_PROGRAM_ID,
            ]
            writable_positions, signer_positions = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20}, {13}
    else:
        accounts = [
            global_pda, base_mint, quote_mint, token_program, token_program, amm_key,
            derive_associated_token_address(amm_key, base_mint),
            derive_associated_token_address(amm_key, quote_mint), taker,
            derive_associated_token_address(taker, base_mint),
            derive_associated_token_address(taker, quote_mint), user_volume_pda,
            fee_config_pda, buyback_recipient, system_program, event_pda, PUMP_PROGRAM_ID,
        ]
        writable_positions, signer_positions = {5, 6, 7, 8, 9, 10, 11, 13}, {8}

    if data_override is not None:
        instruction_data = data_override
    elif name == "buy_exact_sol_in":
        instruction_data = encode(name, 1000, 1900, 1, 0)
    elif name == "buy_exact_quote_in_v2" or name == "buy_exact_quote_in_v3":
        instruction_data = encode(name, 1000, 1900, 0)
    elif name in {"buy", "buy_v2", "buy_v3"}:
        instruction_data = encode(name, 2000, 1010, 1, 0) if name == "buy" else encode(name, 2000, 1010, 0)
    else:
        instruction_data = encode(name, 1000, 1900)

    roles = {taker: (True, True)}
    for index, address in enumerate(accounts):
        old_signer, old_writable = roles.get(address, (False, False))
        roles[address] = (
            old_signer or index in signer_positions,
            old_writable or index in writable_positions,
        )
    roles[JUPITER_PROGRAM_ID] = (False, False)
    account_keys = tuple(roles)
    digest = hashlib.sha256(b"pumpfun-exact-order").hexdigest()
    message_bytes = b"pumpfun-exact-order"
    input_ata = derive_associated_token_address(taker, input_mint)
    output_ata = derive_associated_token_address(taker, output_mint)
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="pump-order",
        quote_id="pump-quote",
        observed_at_epoch_ms=1_000_000,
        taker=taker,
        fee_payer=taker,
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1000,
        output_amount_raw=2000,
        minimum_output_amount_raw=1900,
        price_impact_pct=0.1,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=200,
        recent_blockhash="pump-blockhash",
        message_bytes=message_bytes,
        serialized_transaction_bytes=b"unsigned-pump-tx",
        route_plan=(
            JupiterRouteStep(
                amm_key=amm_key,
                label="Pump.Fun",
                input_mint=input_mint,
                output_mint=output_mint,
                input_amount_raw=1000,
                output_amount_raw=2000,
                percent=100.0,
                bps=10_000,
            ),
        ),
        signature_fee_lamports=5000,
        prioritization_fee_lamports=1000,
        rent_fee_lamports=0,
        slippage_bps=100,
    )
    jupiter_instruction = JupiterSwapInstructionResult(
        message_sha256=digest,
        instruction_name="route",
        program_id=JUPITER_PROGRAM_ID,
        transfer_authority=taker,
        input_token_account=input_ata,
        output_token_account=output_ata,
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1000,
        quoted_output_amount_raw=2000,
        slippage_bps=100,
        platform_fee_bps=0,
        shared_route_id=None,
        top_level_instruction_index=0,
        route_plan=(
            DecodedJupiterRouteStep(
                swap_variant="PumpdotfunWrappedBuy" if direction == "buy" else "PumpdotfunWrappedSell",
                dex_label="Pump.Fun",
                variant_fields=(),
                percent=100,
                input_index=0,
                output_index=1,
            ),
        ),
    )
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=len(message_bytes),
        fee_payer=taker,
        required_signers=(taker,),
        program_ids=(JUPITER_PROGRAM_ID,),
        instruction_count=1,
        account_keys=account_keys,
        account_signers=tuple(roles[key][0] for key in account_keys),
        account_writable=tuple(roles[key][1] for key in account_keys),
    )
    inner = SimulatedInnerInstruction(
        top_level_instruction_index=0,
        program_id=PUMP_PROGRAM_ID,
        account_keys=tuple(accounts),
        data=instruction_data,
        stack_height=2,
    )
    global_snapshot = SimulatedAccountSnapshot(
        address=global_pda,
        owner=PUMP_PROGRAM_ID,
        lamports=1_000_000,
        executable=False,
        rent_epoch=0,
        data=global_account_data(fee_recipient, buyback_recipient),
        context_slot=100,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="pump-blockhash",
        cluster="mainnet-beta",
        genesis_hash="mainnet-genesis",
        provider_id="rpc-test",
        response_id="pump-sim",
        observed_at_epoch_ms=1_000_000,
        context_slot=100,
        current_block_height=150,
        last_valid_block_height=200,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5000,
        token_balances=(),
        top_level_program_ids=(JUPITER_PROGRAM_ID,),
        inner_program_ids=(PUMP_PROGRAM_ID,),
        inner_instruction_count=1,
        inner_instructions=(inner,),
        account_snapshots=(global_snapshot,),
    )
    route_gate = JupiterRouteProgramGateResult(
        passed=True,
        message_sha256=digest,
        router="metis",
        route_labels=("Pump.Fun",),
        dex_program_ids=(PUMP_PROGRAM_ID,),
        top_level_program_ids=(JUPITER_PROGRAM_ID,),
        inner_program_ids=(PUMP_PROGRAM_ID,),
        registry_sha256="a" * 64,
        cpi_instruction_count=1,
        route_pool_accounts_verified=1,
        parent_instruction_index=0,
    )
    policy = PumpFunCpiPolicy(max_slippage_bps=500)
    return order, validation, jupiter_instruction, simulation, route_gate, policy, instruction_data


def decode(values=None):
    order, validation, jupiter_instruction, simulation, route_gate, policy, _ = values or build_case()
    return decode_pumpfun_route_cpis(
        order,
        validation=validation,
        jupiter_instruction=jupiter_instruction,
        simulation=simulation,
        route_gate=route_gate,
        policy=policy,
    )


def test_decodes_v1_exact_sol_in_buy_with_pinned_pdas():
    result = decode()
    assert result.passed is True
    assert result.decoded_instructions[0].instruction_name == "buy_exact_sol_in"
    assert result.decoded_instructions[0].amount_mode == "buy_exact_in"
    assert result.decoded_instructions[0].quote_limit_raw == 1000
    assert result.decoded_instructions[0].base_amount_raw == 1900


def test_decodes_v2_exact_quote_in_buy():
    result = decode(build_case(version="v2", name="buy_exact_quote_in_v2"))
    assert result.decoded_instructions[0].version == "v2"
    assert result.decoded_instructions[0].quote_mint != WRAPPED_SOL_MINT
    assert result.decoded_instructions[0].amount_mode == "buy_exact_in"


def test_decodes_v3_sell_with_exact_input_and_minimum_output():
    result = decode(build_case(version="v3", name="sell_v3", direction="sell"))
    decoded = result.decoded_instructions[0]
    assert decoded.version == "v3"
    assert decoded.direction == "sell"
    assert decoded.base_amount_raw == 1000
    assert decoded.quote_limit_raw == 1900
    assert decoded.quote_limit_kind == "min_output"


def test_decodes_v1_exact_output_buy():
    result = decode(build_case(version="v1", name="buy"))
    assert result.decoded_instructions[0].amount_mode == "buy_exact_out"
    assert result.decoded_instructions[0].quote_limit_raw == 1010


def test_rejects_fee_recipient_not_in_same_slot_global_account_allowlist():
    values = build_case()
    order, validation, jupiter_instruction, simulation, route_gate, policy, _ = values
    other_fee = str(Pubkey.new_unique())
    buyback = str(Pubkey.new_unique())
    changed_snapshot = dataclasses.replace(
        simulation.account_snapshots[0],
        data=global_account_data(other_fee, buyback),
    )
    simulation = dataclasses.replace(simulation, account_snapshots=(changed_snapshot,))
    with pytest.raises(PumpFunCpiDecodeError, match="fee-recipient allowlist"):
        decode((order, validation, jupiter_instruction, simulation, route_gate, policy, None))


def test_rejects_partial_fill_enabled():
    values = build_case(data_override=encode("buy_exact_sol_in", 1000, 1900, 1, 1))
    with pytest.raises(PumpFunCpiDecodeError, match="partial fills are disabled"):
        decode(values)


def test_rejects_max_quote_spend_above_input_slippage_ceiling():
    values = build_case(version="v1", name="buy", data_override=encode("buy", 2000, 1200, 1, 0))
    with pytest.raises(PumpFunCpiDecodeError, match="max quote spend exceeds"):
        decode(values)


def test_rejects_wrong_global_pda():
    values = list(build_case())
    order, validation, jupiter_instruction, simulation, route_gate, policy, data = values
    inner = simulation.inner_instructions[0]
    bad_accounts = list(inner.account_keys)
    bad_accounts[0] = str(Pubkey.new_unique())
    simulation = dataclasses.replace(simulation, inner_instructions=(dataclasses.replace(inner, account_keys=tuple(bad_accounts)),))
    with pytest.raises(PumpFunCpiDecodeError, match="global config account key"):
        decode((order, validation, jupiter_instruction, simulation, route_gate, policy, data))


def test_rejects_invalid_discriminator_and_trailing_data():
    values = build_case(data_override=b"bad-pump")
    with pytest.raises(PumpFunCpiDecodeError, match="discriminator"):
        decode(values)
    values = build_case(data_override=encode("buy_exact_sol_in", 1000, 1900, 1, 0) + b"junk")
    with pytest.raises(PumpFunCpiDecodeError, match="trailing or unknown"):
        decode(values)


def test_rejects_unrelated_message_digest():
    values = list(build_case())
    order, validation, jupiter_instruction, simulation, route_gate, policy, data = values
    simulation = dataclasses.replace(simulation, message_sha256="f" * 64)
    with pytest.raises(PumpFunCpiDecodeError, match="different transaction messages"):
        decode((order, validation, jupiter_instruction, simulation, route_gate, policy, data))


def test_decodes_global_config_and_exposes_slot_bound_fee_allowlists():
    values = build_case()
    simulation = values[3]
    global_config = decode_pumpfun_global_config(
        simulation.account_snapshots[0],
        expected_context_slot=simulation.context_slot,
    )
    assert global_config.context_slot == 100
    assert global_config.fee_recipient_allowlist
    assert global_config.buyback_fee_recipient_allowlist


def test_rejects_global_config_from_different_slot():
    values = build_case()
    snapshot = dataclasses.replace(values[3].account_snapshots[0], context_slot=99)
    with pytest.raises(PumpFunCpiDecodeError, match="simulation context slot"):
        decode_pumpfun_global_config(snapshot, expected_context_slot=100)


def test_rejects_uninitialized_global_config():
    values = build_case()
    snapshot = dataclasses.replace(
        values[3].account_snapshots[0],
        data=global_account_data(str(Pubkey.new_unique()), str(Pubkey.new_unique()), initialized=False),
    )
    with pytest.raises(PumpFunCpiDecodeError, match="not initialized"):
        decode_pumpfun_global_config(snapshot, expected_context_slot=100)
