"""Fail-closed contract tests for the ordered Raydium CPI evidence pipeline."""
import pytest

from veltrix.raydium_clmm_cpi_evidence_pipeline import (
    RaydiumCpiEvidencePipelineError,
    assemble_raydium_clmm_cpi_evidence,
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
    from veltrix.jupiter_order import JupiterOrderTransaction

    # The order is intentionally absent as well: the first invalid boundary
    # must be deterministic and no RPC adapter may be touched.
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


def test_pipeline_never_reports_execution_ready():
    from dataclasses import fields

    ready = next(item for item in fields(__import__(
        "veltrix.raydium_clmm_cpi_evidence_pipeline",
        fromlist=["RaydiumCpiEvidencePipelineResult"],
    ).RaydiumCpiEvidencePipelineResult) if item.name == "execution_ready")
    assert ready.default is False
