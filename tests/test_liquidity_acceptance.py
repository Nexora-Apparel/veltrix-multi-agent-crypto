import json
import sys

from veltrix import liquidity_acceptance


def test_acceptance_cli_persists_machine_readable_report(monkeypatch, tmp_path):
    output = tmp_path / "liquidity.json"
    expected = {
        "reader_acceptance": "PASS",
        "chain": "base",
        "locker_deployments": [{"address": "0x" + "11" * 20}],
    }
    monkeypatch.setattr(liquidity_acceptance, "run", lambda chain, token: expected)
    monkeypatch.setattr(
        sys, "argv",
        ["liquidity_acceptance", "--chain", "base",
         "--token-address", "0x" + "22" * 20, "--output", str(output)],
    )

    liquidity_acceptance.main()

    raw = output.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw) == expected


def test_acceptance_cli_fails_closed_when_reader_acceptance_is_blocked(monkeypatch):
    monkeypatch.setattr(
        liquidity_acceptance, "run",
        lambda chain, token: {"reader_acceptance": "BLOCKED"},
    )
    monkeypatch.setattr(
        sys, "argv",
        ["liquidity_acceptance", "--chain", "bsc",
         "--token-address", "0x" + "22" * 20],
    )
    try:
        liquidity_acceptance.main()
    except RuntimeError as exc:
        assert "acceptance failed" in str(exc)
    else:
        raise AssertionError("blocked reader acceptance must return a failure")
