import pytest
from veltrix.ledger import PaperBroker

def test_paper_ledger_open_partial_and_close(tmp_path):
    broker=PaperBroker(str(tmp_path/"paper.db"),500,10,50)
    opened=broker.open_position("mint1",2.0,50,"two-agent consensus")
    assert opened["quantity"]==25
    half=broker.close_fraction("mint1",4.0,0.5,"2x partial")
    assert half["realized_pnl"]==25
    assert half["remaining_quantity"]==12.5
    final=broker.close_fraction("mint1",6.0,1.0,"trailing exit")
    assert final["realized_pnl"]==50
    assert broker.snapshot()["cash"]==575
    assert broker.totals()["realized_pnl"]==75
    assert broker.snapshot()["open_positions"]==[]

def test_paper_ledger_enforces_position_cap(tmp_path):
    broker=PaperBroker(str(tmp_path/"paper.db"),500,1,50)
    broker.open_position("mint1",1,50,"test")
    with pytest.raises(ValueError): broker.open_position("mint2",1,50,"test")
    with pytest.raises(ValueError): broker.open_position("mint1",1,50,"duplicate")

def test_exit_milestones_are_persisted_and_only_trigger_once(tmp_path):
    path=str(tmp_path/"exits.db")
    broker=PaperBroker(path,500,10,50)
    broker.open_position("mint",10,50,"consensus")
    first=broker.process_market_price("mint",20)
    assert len(first)==1 and first[0]["reason"]=="take_profit_2x"
    assert broker.process_market_price("mint",20)==[]
    # Reopen broker to prove milestone state survives process restarts.
    broker=PaperBroker(path,500,10,50)
    second=broker.process_market_price("mint",30)
    assert len(second)==1 and second[0]["reason"]=="take_profit_3x"
    assert broker.process_market_price("mint",30)==[]
    # 6% trailing drawdown from the $30 high closes the remaining quarter.
    closed=broker.process_market_price("mint",28.1)
    assert len(closed)==1 and closed[0]["reason"]=="trailing_stop"
    assert broker.snapshot()["open_positions"]==[]
    assert broker.totals()["closed_sell_fills"]==3

def test_initial_stop_loss_closes_entire_position(tmp_path):
    broker=PaperBroker(str(tmp_path/"stop.db"),500,10,50)
    broker.open_position("mint",100,50,"consensus")
    result=broker.process_market_price("mint",94)
    assert len(result)==1 and result[0]["reason"]=="stop_loss"
    assert result[0]["remaining_quantity"]==0
    assert broker.snapshot()["open_positions"]==[]

def test_snapshot_marks_open_positions_to_market(tmp_path):
    broker=PaperBroker(str(tmp_path/"mark.db"),500,10,50)
    broker.open_position("mint",10,50,"consensus")
    snap=broker.snapshot({"mint":12})
    assert snap["unrealized_pnl"]==10
    assert snap["equity"]==510

def test_session_report_summarizes_closed_positions(tmp_path):
    broker=PaperBroker(str(tmp_path/"report.db"),500,10,50)
    broker.open_position("winner",10,50,"consensus")
    broker.close_fraction("winner",20,1,"take profit")
    broker.open_position("loser",10,50,"consensus")
    broker.close_fraction("loser",5,1,"stop")
    report=broker.session_report()
    assert report["closed_positions"]==2
    assert report["win_rate_pct"]==50
    assert report["best_trade"]["token"]=="winner"
    assert report["worst_trade"]["token"]=="loser"

def test_price_gap_to_3x_does_not_fabricate_2x_fill(tmp_path):
    broker=PaperBroker(str(tmp_path/"gap.db"),500,10,50)
    broker.open_position("gap-token",10,50,"consensus")
    result=broker.process_market_price("gap-token",30)
    assert len(result)==1 and result[0]["reason"]=="take_profit_3x"
    assert result[0]["quantity_sold"]==50/10/2
    # A retrace to 2x is below the 6% trailing stop from the observed 3x peak.
    # It must close the remainder as a trailing stop, not fabricate a late 2x fill.
    retrace=broker.process_market_price("gap-token",20)
    assert len(retrace)==1 and retrace[0]["reason"]=="trailing_stop"
    with broker._connect() as db:
        reasons=[row[0] for row in db.execute("SELECT reason FROM trades WHERE side='SELL'").fetchall()]
    assert "take_profit_2x" not in reasons

@pytest.mark.parametrize(
    "kwargs",
    [
        {"starting_balance": float("nan")},
        {"starting_balance": float("inf")},
        {"max_position_usd": float("nan")},
        {"stop_loss_pct": float("nan")},
        {"trailing_pct": float("inf")},
        {"max_positions": True},
        {"max_positions": 1.5},
    ],
)
def test_paper_broker_rejects_non_finite_or_untyped_limits(tmp_path, kwargs):
    limits = dict(
        starting_balance=500,
        max_positions=10,
        max_position_usd=50,
        stop_loss_pct=0.06,
        trailing_pct=0.06,
    )
    limits.update(kwargs)
    with pytest.raises(ValueError):
        PaperBroker(str(tmp_path / "invalid-limits.db"), **limits)


@pytest.mark.parametrize(
    "price,amount",
    [
        (float("nan"), 10),
        (float("inf"), 10),
        (True, 10),
        (1, float("nan")),
        (1, float("inf")),
        (1, True),
    ],
)
def test_paper_broker_rejects_invalid_entry_values(tmp_path, price, amount):
    broker = PaperBroker(str(tmp_path / "invalid-entry.db"))
    with pytest.raises(ValueError):
        broker.open_position("mint", price, amount, "test")


def test_paper_broker_rejects_invalid_exit_and_mark_values(tmp_path):
    broker = PaperBroker(str(tmp_path / "invalid-exit.db"))
    broker.open_position("mint", 1, 10, "test")
    for price in (float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            broker.process_market_price("mint", price)
        with pytest.raises(ValueError):
            broker.close_fraction("mint", price, 1.0, "invalid")
    with pytest.raises(ValueError):
        broker.close_fraction("mint", 1.0, float("nan"), "invalid")
    with pytest.raises(ValueError):
        broker.snapshot({"mint": float("nan")})


def test_paper_broker_closes_sqlite_connections(tmp_path):
    broker = PaperBroker(str(tmp_path / "connection.db"))
    with broker._connect() as db:
        connection = db
        assert db.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(Exception):
        connection.execute("SELECT 1")


def test_paper_broker_requires_persistent_database(tmp_path):
    with pytest.raises(ValueError, match="persistent SQLite"):
        PaperBroker(":memory:")
