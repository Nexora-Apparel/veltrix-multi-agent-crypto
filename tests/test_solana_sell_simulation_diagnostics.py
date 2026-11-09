from veltrix.providers.solana_sell_simulation import SolanaSellSimulationObservation
from veltrix.solana_meme.sell_simulation_diagnostics import diagnose_sell_simulation

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TAKER = "11111111111111111111111111111111"


def observation(state, error=None, logs=()):
    return SolanaSellSimulationObservation(
        mint=MINT, taker=TAKER, request_id="req", observed_at_ms=100,
        simulated_slot=10, units_consumed=100, logs=tuple(logs),
        state=state, error=error,
    )


def test_success_is_point_in_time_diagnostic_not_sellability_certification():
    result = diagnose_sell_simulation(observation("SIMULATION_SUCCEEDED"))
    assert result.category == "NO_SIMULATION_ERROR"
    assert result.honeypot_verdict is False
    assert result.token_specific_signal is False


def test_rpc_context_failure_is_retryable():
    result = diagnose_sell_simulation(observation(
        "SIMULATION_FAILED", '{"BlockhashNotFound":null}',
    ))
    assert result.category == "RPC_OR_BLOCKHASH_CONTEXT"
    assert result.retryable is True
    assert result.honeypot_verdict is False


def test_insufficient_balance_is_not_misclassified_as_token_restriction():
    result = diagnose_sell_simulation(observation(
        "SIMULATION_FAILED", "Insufficient funds for fee",
    ))
    assert result.category == "TEST_WALLET_BALANCE_OR_FEES"
    assert result.token_specific_signal is False


def test_missing_associated_token_account_is_setup_issue():
    result = diagnose_sell_simulation(observation(
        "SIMULATION_FAILED", logs=("Associated token account does not exist",),
    ))
    assert result.category == "TOKEN_ACCOUNT_SETUP"
    assert result.token_specific_signal is False


def test_transfer_hook_is_only_a_review_signal_not_a_honeypot_verdict():
    result = diagnose_sell_simulation(observation(
        "SIMULATION_FAILED", logs=("Transfer hook program rejected transfer",),
    ))
    assert result.category == "TRANSFER_RESTRICTION_SIGNAL"
    assert result.token_specific_signal is True
    assert result.honeypot_verdict is False


def test_unknown_custom_program_error_stays_unclassified():
    result = diagnose_sell_simulation(observation(
        "SIMULATION_FAILED", "InstructionError: custom program error: 0x1771",
    ))
    assert result.category == "UNCLASSIFIED_PROGRAM_ERROR"
    assert result.honeypot_verdict is False
