"""Conservative classification of Solana sell-simulation failures.

Diagnostics describe likely failure domains only. They never label a token a
honeypot or certify that a token is safe or sellable.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..providers.solana_sell_simulation import SolanaSellSimulationObservation


@dataclass(frozen=True)
class SolanaSellSimulationDiagnostic:
    category: str
    retryable: bool
    token_specific_signal: bool
    explanation: str
    honeypot_verdict: bool = False


def diagnose_sell_simulation(
    observation: SolanaSellSimulationObservation,
) -> SolanaSellSimulationDiagnostic:
    if not isinstance(observation, SolanaSellSimulationObservation):
        raise TypeError("observation must be a typed SolanaSellSimulationObservation")
    if observation.state == "SIMULATION_SUCCEEDED" and observation.error is None:
        return SolanaSellSimulationDiagnostic(
            "NO_SIMULATION_ERROR", False, False,
            "The unsigned transaction simulated without an instruction error at the recorded slot.",
        )

    text = " ".join((observation.error or "", *observation.logs)).lower()
    if any(term in text for term in (
        "blockhash not found", "block height exceeded", "node is unhealthy",
        "temporarily unavailable", "gateway timeout", "request timed out",
    )):
        return SolanaSellSimulationDiagnostic(
            "RPC_OR_BLOCKHASH_CONTEXT", True, False,
            "The failure appears related to RPC availability or transaction blockhash context; retry with fresh state.",
        )
    if any(term in text for term in (
        "insufficient funds", "insufficient token balance", "not enough tokens",
    )):
        return SolanaSellSimulationDiagnostic(
            "TEST_WALLET_BALANCE_OR_FEES", False, False,
            "The configured test wallet may not hold the requested token amount or required SOL for fees.",
        )
    if any(term in text for term in (
        "accountnotfound", "could not find account", "associated token account",
        "account does not exist",
    )):
        return SolanaSellSimulationDiagnostic(
            "TOKEN_ACCOUNT_SETUP", False, False,
            "A required account may be missing or uninitialised for this test wallet.",
        )
    if any(term in text for term in (
        "transfer hook", "non-transferable", "transfer is disabled",
        "transfers are disabled", "transfer restricted",
    )):
        return SolanaSellSimulationDiagnostic(
            "TRANSFER_RESTRICTION_SIGNAL", False, True,
            "Logs contain a transfer restriction or transfer-hook signal; inspect the mint extensions and program logs independently.",
        )
    if "custom program error" in text or "instructionerror" in text:
        return SolanaSellSimulationDiagnostic(
            "UNCLASSIFIED_PROGRAM_ERROR", False, False,
            "A program instruction failed, but the available evidence is insufficient to identify the cause.",
        )
    return SolanaSellSimulationDiagnostic(
        "UNKNOWN_SIMULATION_FAILURE", False, False,
        "The simulation failed without a recognised diagnostic signature; retain raw logs for manual analysis.",
    )
