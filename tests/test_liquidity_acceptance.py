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



def test_acceptance_requires_live_lock_positions_for_every_configured_locker():
    from types import SimpleNamespace
    batch = SimpleNamespace(
        chain="solana",
        deployments=(
            SimpleNamespace(address="uncx-locker"),
            SimpleNamespace(address="raydium-burn-earn"),
        ),
        positions=(SimpleNamespace(locker_address="uncx-locker"),),
    )
    blockers = liquidity_acceptance._locker_sample_blockers(batch)
    assert len(blockers) == 1
    assert "raydium-burn-earn" in blockers[0]


def test_acceptance_rejects_a_token_without_any_live_lock_position():
    from types import SimpleNamespace
    batch = SimpleNamespace(
        chain="solana",
        deployments=(SimpleNamespace(address="uncx-locker"),),
        positions=(),
    )
    blockers = liquidity_acceptance._locker_sample_blockers(batch)
    assert blockers == [
        "acceptance token has no live locker positions; use a token with active locks"
    ]


def test_acceptance_sample_passes_only_when_all_configured_lockers_are_exercised():
    from types import SimpleNamespace
    batch = SimpleNamespace(
        chain="solana",
        deployments=(
            SimpleNamespace(address="uncx-locker"),
            SimpleNamespace(address="raydium-burn-earn"),
        ),
        positions=(
            SimpleNamespace(locker_address="uncx-locker"),
            SimpleNamespace(locker_address="raydium-burn-earn"),
        ),
    )
    assert liquidity_acceptance._locker_sample_blockers(batch) == []



def test_acceptance_sample_normalizes_evm_locker_address_case():
    from types import SimpleNamespace
    batch = SimpleNamespace(
        chain="base",
        deployments=(SimpleNamespace(address="0xAbCd"),),
        positions=(SimpleNamespace(locker_address="0xabcd"),),
    )
    assert liquidity_acceptance._locker_sample_blockers(batch) == []
