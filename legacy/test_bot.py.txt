import math
from bot import init_db, state, open_paper, settle_pending, choose_candidate, affordable_single, evaluate, Volatility

class FakeKalshi:
    def __init__(self, result): self.result=result
    def market(self, ticker): return {"result": self.result}

def candidate(ticker="TEST", distance=0.3, payout=1.15):
    ask=1/payout
    return dict(ticker=ticker, side="YES", ask=ask, fees_per_contract=0,
                contracts=5, stake=5*ask, fees=0, probability=.88,
                z=.8, roi=.012, payout=payout, distance_pct=distance,
                depth=100, close_time=2000000000)

def test_one_market_selected_by_distance_not_payout():
    assert choose_candidate([candidate("A",.5,1.11),candidate("B",.2,1.2)])["ticker"]=="A"

def test_tie_breaks_by_payout_but_still_one():
    assert choose_candidate([candidate("A",.5,1.11),candidate("B",.5,1.2)])["ticker"]=="B"

def test_no_candidate():
    assert choose_candidate([]) is None

def test_single_position_and_stop_after_loss(tmp_path):
    db=init_db(str(tmp_path/"x.db"),20)
    c=affordable_single(candidate(),5)
    assert c["stake"]<=5
    assert open_paper(db,c,5)
    assert not open_paper(db,c,5)
    assert db.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]==1
    settle_pending(db,FakeKalshi("no"))
    assert state(db,"mode")=="STOPPED"
    assert not open_paper(db,c,5)

def test_win_resumes(tmp_path):
    db=init_db(str(tmp_path/"x.db"),20)
    c=affordable_single(candidate(),5)
    assert open_paper(db,c,5)
    settle_pending(db,FakeKalshi("yes"))
    assert state(db,"mode")=="SCANNING"

def test_unverified_reference_fails_closed():
    cfg={"markets":{"TEST":{"settlement_source_verified":False}}}
    result,reason=evaluate({"ticker":"TEST"},cfg,(100,100),Volatility(3600),{},100)
    assert result is None and "verified" in reason

def test_budget_and_liquidity():
    c=candidate()
    c["depth"]=2
    out=affordable_single(c,5)
    assert out["contracts"]==2
    assert out["stake"]<=5

def test_no_affordable_contract():
    assert affordable_single(candidate(),.1) is None
