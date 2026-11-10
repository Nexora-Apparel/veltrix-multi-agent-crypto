"""Tests for Jupiter's program ID to label registry ingestion."""
import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_program_trace import (
    JUPITER_PROGRAM_LABEL_URL,
    JupiterProgramRegistryAdapter,
    JupiterProgramTraceError,
)


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, *, headers, timeout, allow_redirects):
        self.calls.append((url, headers, timeout, allow_redirects))
        assert url == JUPITER_PROGRAM_LABEL_URL
        assert allow_redirects is False
        return FakeResponse(self.body)


def test_fetches_and_hashes_canonical_program_label_registry():
    jupiter = str(Pubkey.new_unique())
    raydium = str(Pubkey.new_unique())
    session = FakeSession({jupiter: "Jupiter V6", raydium: "Raydium CLMM"})
    adapter = JupiterProgramRegistryAdapter(
        api_key="jupiter-api-key",
        session=session,
        clock_ms=lambda: 1_005_000,
    )
    snapshot = adapter.fetch()
    assert snapshot.label_for(jupiter) == "Jupiter V6"
    assert snapshot.program_ids_for_label("Raydium CLMM") == (raydium,)
    assert snapshot.sha256
    assert session.calls[0][1]["x-api-key"] == "jupiter-api-key"


def test_supports_keyless_registry_fetch():
    program = str(Pubkey.new_unique())
    session = FakeSession({program: "Jupiter V6"})
    snapshot = JupiterProgramRegistryAdapter(
        session=session,
        clock_ms=lambda: 1_005_000,
    ).fetch()
    assert snapshot.label_for(program) == "Jupiter V6"
    assert "x-api-key" not in session.calls[0][1]


@pytest.mark.parametrize("body", [{}, {"not-a-public-key": "Raydium"}, {"11111111111111111111111111111111": ""}])
def test_rejects_empty_or_malformed_registry(body):
    adapter = JupiterProgramRegistryAdapter(
        session=FakeSession(body),
        clock_ms=lambda: 1_005_000,
    )
    with pytest.raises(JupiterProgramTraceError):
        adapter.fetch()


def test_rejects_invalid_local_registry_clock():
    program = str(Pubkey.new_unique())
    adapter = JupiterProgramRegistryAdapter(
        session=FakeSession({program: "Jupiter V6"}),
        clock_ms=lambda: 0,
    )
    with pytest.raises(JupiterProgramTraceError, match="timestamp"):
        adapter.fetch()
