"""Fail-closed contract tests for the ordered Raydium CPI evidence pipeline."""
from dataclasses import fields

import pytest

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_cpi_evidence_pipeline import (
    RaydiumCpiEvidencePipelineError,
    RaydiumCpiEvidencePipelineResult,
    assemble_raydium_clmm_cpi_evidence,
)


def _minimal_order():
    return JupiterOrderTransaction(
        provider_id="test",
        request_id="request",
        quote_id=None,
        observed_at_epoch_ms=1,
        taker="taker",
        fee_payer="taker",
        input_mint="input",
        output_mint="output",
        input_amount_raw=1,
        output_amount_raw=1,
        minimum_output_amount_raw=1,
        price_impact_pct=0.0,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=1,
        recent_blockhash="blockhash",
        message_bytes=b"unsigned-message",
        serialized_transaction_bytes=b"unsigned-transaction",
        route_plan=(),
        signature_fee_lamports=0,
        prioritization_fee_lamports=0,
        rent_fee_lamports=0,
    )


def test_pipeline_rejects_missing_unsigned_order_before_rpc():
    with pytest.raises(RaydiumCpiEvidencePipelineError, match="unsigned Jupiter order"):
        assemble_raydium_clmm_cpi_evidence(
            None,
            cpi=None,
            pool_check=None,
            pool_evidence=None,
            vault_snapshot=None,
            simulation=None,
            rpc_adapter=None,
        )


def test_pipeline_rejects_missing_successful_cpi_before_rpc():
    with pytest.raises(RaydiumCpiEvidencePipelineError, match="successful Raydium CLMM CPI"):
        assemble_raydium_clmm_cpi_evidence(
            _minimal_order(),
            cpi=None,
            pool_check=None,
            pool_evidence=None,
            vault_snapshot=None,
            simulation=None,
            rpc_adapter=None,
        )


def test_pipeline_result_is_permanently_non_execution_ready():
    field = next(item for item in fields(RaydiumCpiEvidencePipelineResult) if item.name == "execution_ready")
    assert field.default is False
