from discover import looks_eligible, discover_open, discover_series

class FakeClient:
    def get(self,path,params):
        if path == '/series':
            return {'series':[{'ticker':'KXBTC15M','title':'Bitcoin 15 minute'},{'ticker':'KXSPORT15M','title':'Football'}],'cursor':''}
        assert params.get('series_ticker')
        if params['series_ticker']=='KXBTC15M':
            return {'markets':[{'ticker':'KXBTC15M-1','status':'open','close_time':'2030-01-01T00:00:00Z','title':'Bitcoin'}],'cursor':''}
        return {'markets':[],'cursor':''}

def test_candidate_filter():
    assert looks_eligible({'ticker':'KXBTC15M-1','status':'open','close_time':'2030-01-01T00:00:00Z','title':'Bitcoin'})
    assert not looks_eligible({'ticker':'KXSPORT15M-1','status':'open','close_time':'2030-01-01T00:00:00Z','title':'Sports'})
    assert not looks_eligible({'ticker':'KXBTCDAY-1','status':'open','close_time':'2030-01-01T00:00:00Z','title':'Bitcoin'})

def test_targeted_discovery():
    stats={}
    results=discover_open(FakeClient(),clock=lambda:0,stats=stats)
    assert len(results)==1
    assert stats['series_complete']
    assert stats['series_checked']>=7
    assert not stats['failed_series']

def test_catalog_discovers_known_and_filters_sports():
    series,stats=discover_series(FakeClient())
    assert 'KXBTC15M' in series
    assert 'KXSPORT15M' not in series
    assert stats['series_complete']

def test_failed_series_visible():
    class Broken(FakeClient):
        def get(self,path,params):
            if path=='/markets':
                raise RuntimeError('network down')
            return super().get(path,params)
    stats={}
    assert discover_open(Broken(),stats=stats)==[]
    assert stats['failed_series']
    assert not stats['complete_pagination']
