from discover import looks_eligible, discover_open

def test_known_btc():
    assert looks_eligible({'ticker':'KXBTC15M-TEST','status':'open','close_time':'2026-10-28T12:00:00Z','title':'Bitcoin price up/down'})

def test_non_15m_rejected():
    assert not looks_eligible({'ticker':'KXBTCDAILY-TEST','status':'open','close_time':'2026-10-28T12:00:00Z','title':'Bitcoin price'})

def test_unknown_category_rejected():
    assert not looks_eligible({'ticker':'KXSPORT15M-TEST','status':'open','close_time':'2026-10-28T12:00:00Z','title':'Sports'})

def test_pagination():
    class Client:
        def get(self, path, params):
            if 'cursor' not in params:
                return {'markets':[{'ticker':'KXBTC15M-1','status':'open','close_time':'2030-01-01T00:00:00Z','title':'Bitcoin'}], 'cursor':'next'}
            return {'markets':[{'ticker':'KXGOLD15M-1','status':'open','close_time':'2030-01-01T00:00:00Z','title':'Gold'}], 'cursor':''}
    assert len(discover_open(Client(),clock=lambda:0)) == 2
