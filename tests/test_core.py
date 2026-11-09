from veltrix.agents import Signal
from veltrix.core import Core
from veltrix.ledger import PaperBroker

class FixedAgent:
    def __init__(self, name, side="BUY", token_override=None):
        self.name, self.side, self.token_override = name, side, token_override
    def evaluate(self, token):
        address = self.token_override or token["address"]
        return Signal(self.name, address, self.side, "test signal")

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
