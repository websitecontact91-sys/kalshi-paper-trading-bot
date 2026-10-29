import copy
from decimal import Decimal as D
from pathlib import Path
import pytest
from paper_runner import PaperEngine, main
from persistence import PersistenceError, persist
from simulate import FakeClock, FakeKalshiClient, SCENARIOS, run_scenario
from state_store import load, new_state, save
from timing import round_key

@pytest.fixture
def live_engine(tmp_path):
    clock = FakeClock()
    client = FakeKalshiClient(clock)
    path = tmp_path / 'state.json'
    def durable(state): return persist(state, path)
    durable(new_state(session_id='runner-test'))
    engine = PaperEngine(client, load(path), persist_state=durable, paper=True, clock=clock,
                         source='synthetic_test', report_dir=tmp_path/'reports')
    return engine, client, clock, path

@pytest.mark.parametrize('name', SCENARIOS)
def test_controlled_scenarios_drive_production_runner(name):
    assert run_scenario(name)['status'] == 'PASS'

@pytest.mark.parametrize('remaining,entered', [(61,False),(60,True),(52.9,True),(1,True),(0,False),(-1,False)])
def test_runner_timing_boundaries(live_engine, remaining, entered):
    engine, client, clock, _ = live_engine
    clock.now = client.close_time-remaining
    engine.tick()
    assert (engine.state['pending'] is not None) is entered

@pytest.mark.parametrize('current', ['99','100'])
def test_distance_never_becomes_an_eligibility_gate(live_engine, current):
    engine, client, _, _ = live_engine
    client.current = current
    engine.tick()
    assert engine.state['pending'] is not None
    assert D(engine.state['pending']['distance_pct']) <= 0

@pytest.mark.parametrize('blocked', ['quote_ready','compatible','stale'])
def test_blocked_candidate_cannot_enter(live_engine, blocked):
    engine, client, _, _ = live_engine
    setattr(client, blocked, blocked == 'stale')
    engine.tick()
    assert engine.state['pending'] is None
    assert engine.rounds[round_key(client.close_time)]['rejections']

def test_final_refresh_ask_change_falls_back_to_next_rank(live_engine):
    engine, _, clock, _ = live_engine
    client = FakeKalshiClient(clock, tickers=('KXBTC15M-TEST','KXSOL15M-TEST'))
    def change(result):
        if result['market']['ticker'].startswith('KXBTC'):
            result['asks']['YES'] = D('0.91')
    client.snapshot_hook = change
    engine.client = client
    engine.tick()
    assert engine.state['pending']['ticker'] == 'KXSOL15M-TEST'
    assert client.snapshots == ['KXBTC15M-TEST','KXSOL15M-TEST']

def test_same_round_stays_used_after_a_win(live_engine):
    engine, client, _, path = live_engine
    engine.tick()
    client.status, client.result = 'settled','yes'
    engine.tick()
    assert engine.state['mode'] == 'SCANNING'
    engine.tick()
    assert engine.state['pending'] is None
    assert len(load(path)['rounds_traded']) == 1

def test_provisional_yes_is_not_settlement(live_engine):
    engine, client, _, _ = live_engine
    engine.tick()
    client.status, client.result = 'closed','yes'
    engine.tick()
    assert engine.state['mode'] == 'WAITING_FOR_SETTLEMENT'
    assert not engine.state['history']

def test_mismatched_official_market_cannot_settle(live_engine):
    engine, client, _, _ = live_engine
    engine.tick()
    client.official_market = lambda ticker: {'ticker':'WRONG', 'status':'settled', 'result':'yes'}
    engine.tick()
    assert engine.state['pending'] is not None

@pytest.mark.parametrize('status', ['voided','cancelled','canceled'])
def test_void_refunds_actual_debit(live_engine, status):
    engine, client, _, path = live_engine
    engine.tick()
    client.status, client.result = status,'void'
    engine.tick()
    assert D(engine.state['balance']) == D('1000.00')
    assert engine.state['history'][-1]['result'] == 'VOID'
    assert D(engine.state['history'][-1]['pnl']) == D('0.00')
    assert load(path)['mode'] == 'SCANNING'

def test_persistence_failure_aborts_without_announcing_entry(live_engine):
    engine, _, _, _ = live_engine
    def broken(state): raise PersistenceError('synthetic failed remote push')
    engine.persist_state = broken
    before = copy.deepcopy(engine.state)
    with pytest.raises(PersistenceError): engine.tick()
    assert engine.state == before
    assert not any(e['kind']=='PAPER_ENTRY' for e in engine.events)

def test_read_only_monitor_never_changes_state(live_engine):
    engine, _, _, path = live_engine
    engine.paper = False
    before = path.read_bytes()
    engine.tick(); engine.finish()
    assert path.read_bytes() == before
    assert engine.state['pending'] is None

def test_quote_amount_is_not_rebuilt_from_displayed_multiplier(live_engine):
    engine, _, _, _ = live_engine
    engine.tick()
    trade = engine.state['pending']
    assert trade['payout'] == '1.13'
    assert trade['displayed_multiplier'] == '1.14'
    assert D(trade['payout']) != D(trade['displayed_multiplier'])*D(trade['budget'])
    assert trade['quote_source'] == 'controlled_synthetic_fixture'
    assert trade['ui_parity_verified'] is False


@pytest.mark.parametrize('budget,debit,payout', [('5.00','5.00','5.69'),
                                               ('5.00','0.50','0.57')])
def test_old_or_oversized_cached_quote_cannot_create_a_new_trade(live_engine, budget, debit, payout):
    from payout_quote import PayoutQuote
    from quote_adapter import QuoteDetails
    engine, client, _, _ = live_engine
    original = client._snapshot

    def old_quote(market):
        snapshot = original(market)
        snapshot['quotes']['YES'] = QuoteDetails(PayoutQuote(budget, debit, payout),
            D(payout), D('0.01'), D(debit)-D('0.01'), 'synthetic_old_budget')
        return snapshot

    client._snapshot = old_quote
    engine.tick()
    assert engine.state['pending'] is None
    assert engine.state['balance'] == '1000.00'
    assert engine.rounds[round_key(client.close_time)]['rejections'][client.markets[0]['ticker']+':YES'] == 'trade_budget_exceeds_limit'

def test_coverage_and_round_reasons_are_durable(live_engine):
    engine, client, _, path = live_engine
    client.quote_ready = False
    engine.tick()
    report = engine.finish()
    row = report['rounds'][0]
    assert row['final_window_scanned'] is True
    assert row['close_time'] and row['rejections']
    assert load(path)['round_reports'][-1]['rounds'] == report['rounds']


@pytest.mark.parametrize('transition', ['entry', 'win', 'loss', 'audit'])
def test_source_update_handoff_retains_durable_transition_and_stops_io(live_engine, transition):
    engine, client, _, path = live_engine
    if transition in ('win', 'loss'):
        engine.tick()
        client.status, client.result = 'settled', 'yes' if transition == 'win' else 'no'
    elif transition == 'audit':
        client.quote_ready = False
        engine.tick()
    writes = []

    def saved_before_handoff(state):
        writes.append(copy.deepcopy(state))
        return {**persist(state, path), 'remote': True, 'restart_required': True,
                'remote_source_sha': 'new-tested-by-next-job', 'state_commit': 'durable-transition'}

    engine.persist_state = saved_before_handoff
    if transition == 'audit':
        report = engine.finish()
    else:
        engine.tick()
        report = engine.finish()
    assert len(writes) == 1
    assert load(path) == engine.state
    assert report['completion_reason'] == 'source_update_handoff'
    assert report['failure'] is None
    assert report['source_update']['state_commit'] == 'durable-transition'
    assert any(e['kind'] == 'SOURCE_UPDATE_HANDOFF' for e in engine.events)
    if transition == 'entry':
        assert engine.state['pending'] is not None
        assert engine.state['balance'] == '999.0079'
        assert any(e['kind'] == 'PAPER_ENTRY' for e in engine.events)
    elif transition in ('win', 'loss'):
        assert engine.state['pending'] is None
        assert len(engine.state['history']) == 1
        assert engine.state['mode'] == ('SCANNING' if transition == 'win' else 'STOPPED')
    else:
        assert engine.state['pending'] is None and len(engine.state['round_reports']) == 1

    def forbidden(*args, **kwargs):
        raise AssertionError('A source handoff must not perform another scan or settlement')

    client.discover = client.official_market = forbidden
    assert engine.tick() == 'SOURCE_UPDATE_HANDOFF'
    engine.finish()
    assert len(writes) == 1


def test_cli_source_handoff_exits_successfully_after_one_durable_entry(tmp_path, monkeypatch):
    import live_client
    import paper_runner
    import time
    clock = FakeClock(time.time())
    client = FakeKalshiClient(clock, close=clock() + 30)
    closed, writes = [], []
    client.close = lambda: closed.append(True)
    path = tmp_path / 'handoff.json'
    save(path, new_state(session_id='handoff-cli'))

    def durable(state, destination, **kwargs):
        writes.append(copy.deepcopy(state))
        return {**persist(state, destination), 'remote': True, 'restart_required': True,
                'remote_source_sha': 'updated-source', 'state_commit': 'saved-paper-entry'}

    monkeypatch.setenv('PAPER_SOURCE_SHA', 'tested-running-source')
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(live_client, 'LiveKalshiClient', lambda *a, **kw: client)
    monkeypatch.setattr(paper_runner, 'persist', durable)
    assert main(['--paper', '--persist-to-github', '--state', str(path),
                 '--report-dir', str(tmp_path / 'reports'), '--duration', '600']) == 0
    assert closed == [True] and len(writes) == client.discoveries == 1
    assert load(path)['pending']['source_sha'] == 'tested-running-source'


def test_cli_unconfirmed_persistence_still_fails(tmp_path, monkeypatch):
    import live_client
    import paper_runner
    import time
    clock = FakeClock(time.time())
    client = FakeKalshiClient(clock, close=clock() + 30)
    path = tmp_path / 'failed-save.json'
    save(path, new_state(session_id='failure-cli'))

    def failed(*args, **kwargs):
        raise PersistenceError('Remote paper state was not saved after 4 attempts')

    monkeypatch.setenv('PAPER_SOURCE_SHA', 'tested-running-source')
    monkeypatch.setattr(live_client, 'LiveKalshiClient', lambda *a, **kw: client)
    monkeypatch.setattr(paper_runner, 'persist', failed)
    with pytest.raises(PersistenceError, match='not saved'):
        main(['--paper', '--state', str(path), '--report-dir', str(tmp_path / 'reports')])
    assert load(path)['pending'] is None


def test_explicit_reset_handoff_exits_before_initializing_live_client(tmp_path, monkeypatch):
    import live_client
    import paper_runner
    path = tmp_path / 'reset-handoff.json'
    save(path, new_state(session_id='archived-on-explicit-request'))
    before = path.read_bytes()

    def durable(state, destination, **kwargs):
        assert len(kwargs['extra_paths']) == 1
        assert kwargs['extra_paths'][0].read_bytes() == before
        return {**persist(state, destination), 'remote': True, 'restart_required': True,
                'remote_source_sha': 'updated-source', 'state_commit': 'saved-explicit-reset'}

    def forbidden(*args, **kwargs):
        raise AssertionError('A reset handoff must exit before creating a live client')

    monkeypatch.setenv('PAPER_SOURCE_SHA', 'tested-running-source')
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(live_client, 'LiveKalshiClient', forbidden)
    monkeypatch.setattr(paper_runner, 'persist', durable)
    assert main(['--paper', '--persist-to-github', '--new-session', '--confirm-reset',
                 '--state', str(path), '--report-dir', str(tmp_path / 'reports')]) == 0
    assert load(path)['session_id'] != 'archived-on-explicit-request'

def test_cli_stopped_exits_without_live_client(tmp_path, monkeypatch):
    import live_client
    clock = FakeClock(); client = FakeKalshiClient(clock)
    path = tmp_path/'stopped.json'
    engine = PaperEngine(client, new_state(session_id='stop'),
        persist_state=lambda s: persist(s,path), paper=True, clock=clock)
    engine.tick(); client.status,client.result='settled','no'; engine.tick()
    before=path.read_bytes()
    monkeypatch.setenv('PAPER_SOURCE_SHA','synthetic')
    def forbidden(*a,**kw): raise AssertionError('STOPPED must not initialize live network')
    monkeypatch.setattr(live_client,'LiveKalshiClient',forbidden)
    assert main(['--paper','--state',str(path)]) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize('official_result,mode', [('yes', 'SCANNING'), ('no', 'STOPPED')])
def test_configured_api_paper_quotes_enter_and_settle_without_ui_capture(tmp_path, official_result, mode):
    """Controlled API-shaped data crosses the real adapter, engine and persistence.

    These inputs exercise the authorized paper model; they are not a captured
    retail quote or evidence of a live fill. Only one of the 30 series is open.
    """
    from live_client import LiveKalshiClient
    from universe import UNIVERSE

    close = 1793295000
    clock = FakeClock(close - 61)
    ticker, event_ticker = 'KXBTC15M-CONTROLLED', 'KXBTC15M-CONTROLLED-EVENT'
    observed_market = {
        'ticker': ticker, 'series_ticker': 'KXBTC15M', 'event_ticker': event_ticker,
        'market_type': 'binary', 'notional_value_dollars': '1.0000',
        'settlement_bounds_type': 'default', 'strike_type': 'greater_or_equal',
        'floor_strike': '100', 'status': 'active', 'open_time': close - 900,
        # The independent market summary differs from the executable book.
        'close_time': close, 'yes_ask_dollars': '0.79', 'no_ask_dollars': '0.14',
    }
    book = {'orderbook_fp': {'no_dollars': [['0.13', '100.00']],
                             'yes_dollars': [['0.86', '100.00']]}}

    class ReadOnlyAPI:
        def __init__(self):
            self.discovered, self.settled = set(), False

        def open_markets(self, series, now):
            self.discovered.add(series)
            return [copy.deepcopy(observed_market)] if series == 'KXBTC15M' else []

        def series(self, series):
            return {'ticker': series, 'fee_type': 'quadratic', 'fee_multiplier': '1',
                    'settlement_sources': [{'name': 'CF Benchmarks',
                                            'url': 'https://www.cfbenchmarks.com/'}]}

        def event(self, event):
            # Kalshi may omit unused event fee overrides; series terms apply.
            return {'event_ticker': event, 'series_ticker': 'KXBTC15M'}

        def market(self, requested):
            assert requested == ticker
            result = copy.deepcopy(observed_market)
            if self.settled:
                result.update(status='settled', result=official_result)
            return result

        def refresh_markets(self, tickers):
            return {requested: self.market(requested) for requested in tickers}

        def orderbook(self, requested):
            assert requested == ticker
            return copy.deepcopy(book)

        def orderbooks(self, tickers):
            return {requested: self.orderbook(requested) for requested in tickers}

    class KalshiReferenceInputs:
        def price(self, series):
            assert series == 'KXBTC15M'
            return D('101'), clock()

        def close(self):
            pass

    api = ReadOnlyAPI()
    client = LiveKalshiClient(Path(__file__).resolve().parents[1] / 'quote_policy.json',
                             api=api, references=KalshiReferenceInputs(), clock=clock)
    assert client.readiness_reasons == []
    path = tmp_path / 'api-paper-state.json'

    def durable(state):
        return persist(state, path)

    engine = PaperEngine(client, new_state(session_id='controlled-api-paper'),
                         persist_state=durable, paper=True, clock=clock)
    engine.tick()
    assert engine.state['pending'] is None  # Still outside the final minute.
    assert api.discovered == set(UNIVERSE)

    clock.now = close - 59
    engine.tick()
    trade = load(path)['pending']
    assert trade['ticker'] == ticker and trade['side'] == 'YES'
    assert trade['quote_source'] == 'documented_fee_model'
    assert trade['ui_parity_verified'] is False
    assert trade['quote_policy_ready'] is True
    assert trade['quote_policy_mode'] == 'documented_api'
    assert 'quote_policy_verified' not in trade
    assert trade['probability_field'] == 'kalshi_orderbook:1-best_NO_bid'
    assert D(trade['ask']) == D('0.87')
    assert D(trade['summary_ask']) == D('0.79')
    assert D(trade['payout']) == D('1.13')
    assert trade['displayed_multiplier'] == '1.14'
    assert D('0') < D(trade['debit']) <= D('1')
    assert D(trade['principal']) + D(trade['fees']) == D(trade['debit'])
    assert engine.rounds[round_key(close)]['rejections']['KXDJIA15M:*'] == 'no_markets'

    # Restart from the recorded pending position, then use an official result.
    restarted = PaperEngine(client, load(path), persist_state=durable, paper=True, clock=clock)
    api.settled, clock.now = True, close + 1
    restarted.tick()
    saved = load(path)
    assert saved['pending'] is None and saved['mode'] == mode
    assert len(saved['history']) == len(saved['rounds_traded']) == 1
    expected = D('1000') - D(trade['debit'])
    if official_result == 'yes':
        expected += D(trade['payout'])
    assert D(saved['balance']) == expected
    restarted.tick()
    assert restarted.state['pending'] is None
    assert len(restarted.state['history']) == 1
