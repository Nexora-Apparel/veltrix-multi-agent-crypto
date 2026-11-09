from veltrix.agents import Sniper, Shield, Exit, Signal
from veltrix.safety import SafetyEvidence, TokenSafetyValidator
from veltrix.composite_safety import CompositeTokenSafetyValidator, SafetyEvidenceBundle
from veltrix.launch_bundles import LaunchBundleReport

def passing_report():
    now = 1_001
    bundle = SafetyEvidenceBundle(
        liquidity=SafetyEvidence("x", "lock", "lock-1", 1000, True, None, None),
        developer_holdings=SafetyEvidence("x", "dev", "dev-1", 1000, None, 4.5, None),
        launch_bundle=LaunchBundleReport(
            chain="solana", token_address="x", provider="bundle",
            launch_at_ms=0, window_end_ms=1000, coverage_complete=True,
            bundled_supply_pct=0.0, shared_funder_clusters=0,
            participating_wallets=0, accepted_transactions=1,
            rejected=(), notes=(),
        ),
    )
    return CompositeTokenSafetyValidator(
        ["lock"], ["dev"], ["bundle"], max_age_ms=10_000
    ).validate("x", bundle, now, chain="solana")

def test_sniper_fails_closed_when_report_missing():
    assert Sniper().evaluate({"address":"x"}) is None

def test_sniper_requires_validator_report():
    assert Sniper().evaluate({"address":"x", "liquidity_locked":True, "dev_hold_pct":1, "bundled_supply_pct":0}) is None
    assert Sniper().evaluate({"address":"x", "safety_report":passing_report()}).side == "BUY"

def test_sniper_rejects_report_for_another_token():
    assert Sniper().evaluate({"address":"y", "safety_report":passing_report()}) is None

def test_shield_limits_and_same_token_consensus():
    signals=[Signal("scout","x","BUY","a"),Signal("whale","x","BUY","b")]
    gate=Shield()
    assert gate.approve(signals,0,50,10,50)
    assert not gate.approve(signals,10,50,10,50)
    assert not gate.approve(signals,0,50.01,10,50)
    assert not gate.approve(signals[:1],0,50,10,50)
    assert not gate.approve([signals[0],Signal("whale","different","BUY","b")],0,50,10,50)

def test_exit_rules_and_invalid_fraction():
    e=Exit()
    assert e.decisions(100,94)==[("stop_loss",1.0)]
    assert e.decisions(100,200)==[("take_2x",0.5)]
    assert e.decisions(100,300)==[("take_3x_and_trail",0.5)]
    assert e.decisions(100,200,0.5)==[("take_2x",0.25)]
    assert e.decisions(100,200,0)==[]



def test_sniper_rejects_single_provider_legacy_safety_report():
    evidence = SafetyEvidence("x", "trusted", "evidence-1", 1000, True, 4.5, 0.0)
    legacy = TokenSafetyValidator(["trusted"]).validate("x", evidence, 1001)
    assert legacy.passed
    assert not legacy.composite_verified
    assert Sniper().evaluate({"address": "x", "safety_report": legacy}) is None
