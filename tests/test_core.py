from veltrix.agents import Signal
from veltrix.core import Core
from veltrix.ledger import PaperBroker
from veltrix.safety import SafetyReport
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.sellability import SolanaSellabilityEvidence

MINT = "So11111111111111111111111111111111111111112"

MINT = "So11111111111111111111111111111111111111112"

class FixedAgent:
    def __init__(self, name, side="BUY", token_override=None):
        self.name, self.side, self.token_override = name, side, token_override
    def evaluate(self, token):
        address = self.token_override or token["address"]
        return Signal(self.name, address, self.side, "test signal")

def complete_solana_evidence():
    now = int(__import__("time").time() * 1000)
    return SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=now,
        goplus_provider="goplus", goplus_evidence_id="gp:1",
        goplus_observed_at_ms=now, liquidity_locked=True,
        holder_provider="solana-rpc-holder-inventory",
        holder_evidence_id="holders:1", holder_block_ref="finalized-slot:1",
        holder_count=2, total_supply_raw=1_000_000_000,
        observed_holder_balance_raw=1_000_000_000, holder_coverage_complete=True,
        top_holder_pct=60.0, top5_holder_pct=100.0, top10_holder_pct=100.0,
        missing_claims=(), errors=(),
        mint_evidence_id="mint:core", mint_finalized_slot=1,
        token_program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        mint_supply_raw=1_000_000_000, mint_decimals=6, mint_supply_reconciled=True,
        mint_authority=None, freeze_authority=None,
        token_2022_extension_types=(), token_2022_extension_review_complete=True,
    )

def complete_solana_safety():
    now = int(__import__("time").time() * 1000)
    return SafetyReport(
        token_address=MINT, passed=True, checks=("composite",), reasons=(),
        observed_at_ms=now, composite_verified=True,
    )

def complete_solana_sellability():
    now = int(__import__("time").time() * 1000)
    return SolanaSellabilityEvidence(
        mint=MINT, observed_at_ms=now, provider="jupiter-v2", evidence_id="route:core",
        input_amount_raw=50_000_000, input_supply_fraction=0.05,
        quoted_output_raw=50_000_000, minimum_output_raw=49_000_000,
        route_plan_count=2, price_impact_bps=75.0,
        quote_expires_at_ms=now + 15_000,
        state="ROUTE_OBSERVED_NOT_EXECUTION_PROVEN", errors=(), execution_proven=False,
    )

def test_core_opens_only_after_two_agent_same_token_consensus(tmp_path):
    broker=PaperBroker(str(tmp_path/"core.db"),500,10,50)
    core=Core(broker,agents=[FixedAgent("scout"),FixedAgent("whale")])
    result=core.evaluate({"address":"mint","price":2})
    assert result["side"]=="BUY" and result["amount_usd"]==50
    assert len(broker.snapshot()["open_positions"])==1
    assert broker.snapshot()["cash"]==450

def test_core_rejects_single_agent_and_mixed_token_consensus(tmp_path):
    broker=PaperBroker(str(tmp_path/"reject.db"),500,10,50)
    single=Core(broker,agents=[FixedAgent("scout")])
    assert single.evaluate({"address":"mint","price":2}) is None
    mixed=Core(broker,agents=[FixedAgent("scout"),FixedAgent("whale",token_override="other")])
    assert mixed.evaluate({"address":"mint","price":2}) is None
    assert broker.snapshot()["open_positions"]==[]

def test_core_enforces_max_positions_and_invalid_prices(tmp_path):
    broker=PaperBroker(str(tmp_path/"limits.db"),500,1,50)
    core=Core(broker,max_positions=1,agents=[FixedAgent("scout"),FixedAgent("whale")])
    assert core.evaluate({"address":"one","price":1}) is not None
    assert core.evaluate({"address":"two","price":1}) is None
    assert core.evaluate({"address":"bad","price":0}) is None

def test_core_persists_decision_audit(tmp_path):
    import sqlite3
    path=str(tmp_path/"audit.db")
    broker=PaperBroker(path,500,10,50)
    core=Core(broker,agents=[FixedAgent("scout")])
    core.evaluate({"address":"mint","price":1})
    with sqlite3.connect(path) as db:
        payload=db.execute("SELECT payload FROM events WHERE event='AGENT_DECISION'").fetchone()[0]
    assert '"decision": "REJECT"' in payload

def test_core_rejects_invalid_amount_without_crashing(tmp_path):
    broker = PaperBroker(str(tmp_path / "bad-amount.db"), 500, 10, 50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("whale")])
    assert core.evaluate({"address": "mint", "price": 1}, amount_usd="not-a-number") is None
    assert broker.snapshot()["open_positions"] == []

def test_core_does_not_count_duplicate_agent_identity_as_consensus(tmp_path):
    broker = PaperBroker(str(tmp_path / "duplicate-agent.db"), 500, 10, 50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("scout")])
    assert core.evaluate({"address": "mint", "price": 1}) is None

def test_core_blocks_solana_entry_without_typed_risk_evidence(tmp_path):
    broker = PaperBroker(str(tmp_path / "solana-no-evidence.db"), 500, 10, 50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("whale")])
    token = {"address": MINT, "chain": "solana", "price": 1.0}
    assert core.evaluate(token) is None
    assert broker.snapshot()["open_positions"] == []

def test_core_routes_solana_entries_through_claim_aware_paper_shield(tmp_path):
    broker = PaperBroker(str(tmp_path / "solana-shield.db"), 500, 10, 50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("whale")])
    token = {
        "address": MINT, "chain": "solana", "price": 1.0,
        "risk_evidence_report": complete_solana_evidence(),
        "safety_report": complete_solana_safety(),
        "sellability_evidence": complete_solana_sellability(),
        "sell_amount_raw": 50_000_000,
    }
    result = core.evaluate(token)
    assert result is not None
    assert result["side"] == "BUY"
    assert result["sellability_state"] == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"
    assert result["sellability_evidence_id"] == "route:core"
    assert len(broker.snapshot()["open_positions"]) == 1

def test_core_rejects_non_finite_price(tmp_path):
    broker = PaperBroker(str(tmp_path / "nan-price.db"), 500, 10, 50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("whale")])
    assert core.evaluate({"address": "mint", "price": float("nan")}) is None

def test_core_cannot_bypass_solana_shield_by_omitting_chain_label(tmp_path):
    broker = PaperBroker(str(tmp_path / "solana-unlabeled.db"), 500, 10, 50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("whale")])
    assert core.evaluate({"address": MINT, "price": 1.0}) is None
    assert broker.snapshot()["open_positions"] == []

def test_core_cannot_bypass_solana_shield_with_evm_chain_label(tmp_path):
    broker = PaperBroker(str(tmp_path / "solana-mislabeled.db"), 500, 10, 50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("whale")])
    assert core.evaluate({"address": MINT, "chain": "base", "price": 1.0}) is None
    assert broker.snapshot()["open_positions"] == []
