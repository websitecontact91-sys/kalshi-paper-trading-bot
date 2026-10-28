from bot import evaluate, choose_candidate

class DummyVol:
    def sigma_remaining(self, *args):
        raise AssertionError("No volatility model should be needed")

def market(yes=0.85, no=0.15):
    return {"ticker":"TEST","status":"open","close_time":1000,"yes_ask_dollars":str(yes),"no_ask_dollars":str(no)}

def config():
    return {"markets":{"TEST":{"settlement_source_verified":True,"interval_seconds":900,"product_id":"BTC-USD","target_price":100,"yes_when":"above"}},"stake_usd":5,"min_payout":1.10,"max_payout":1.20,"min_contract_price":0.80,"max_contract_price":0.90,"min_seconds_left":5,"max_seconds_left":60}

def test_qualifies_on_market_price_not_model_probability():
    c,reason=evaluate(market(),config(),(101,950),DummyVol(),{"orderbook":{"no":[[15,10]]}},950)
    assert reason=="qualified" and c["side"]=="YES" and c["ask"]==0.85

def test_80_cent_fails_payout_cap():
    c,_=evaluate(market(0.80,0.20),config(),(101,950),DummyVol(),{"orderbook":{"no":[[20,10]]}},950)
    assert c is None

def test_90_cent_qualifies():
    c,_=evaluate(market(0.90,0.10),config(),(101,950),DummyVol(),{"orderbook":{"no":[[10,10]]}},950)
    assert c is not None

def test_no_side_when_price_on_wrong_side():
    c,_=evaluate(market(),config(),(99,950),DummyVol(),{"orderbook":{"no":[[15,10]]}},950)
    assert c is None
