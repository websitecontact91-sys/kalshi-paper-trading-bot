from paper_runner import choose, evaluate_market, default_state, load_state, settle, persist

class Fake:
    def market(self, ticker):
        return {'result': 'no'}

def test_distance_only_ranking():
    assert choose([{'ticker':'A','side':'NO','distance_pct':-1,'gross_payout':1.2},
                   {'ticker':'B','side':'YES','distance_pct':2,'gross_payout':1.1}])['ticker'] == 'B'

def test_no_candidates():
    assert choose([]) is None

def test_stop_persists(tmp_path):
    p = tmp_path / 'state.json'
    s = default_state()
    s['mode'] = 'WAITING_FOR_SETTLEMENT'
    s['trade'] = {'ticker':'T','side':'YES','count':5,'cost':4.5}
    s['balance'] = 995.5
    assert settle(s, Fake())
    assert s['mode'] == 'STOPPED'
    persist(s, path=p)
    assert load_state(p)['mode'] == 'STOPPED'

def test_requires_final_minute():
    m = {'ticker':'X','floor_strike':100,'close_time':'2030-01-01T00:00:00Z',
         'status':'open','strike_type':'greater_or_equal','yes_ask_dollars':'0.85'}
    assert evaluate_market(m, 110, 1893455000, {}, 1000) == []
