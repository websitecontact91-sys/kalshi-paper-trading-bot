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
    assert engine.state['balance'] == '1000.00'
    assert engine.state['history'][-1]['result'] == 'VOID'
    assert engine.state['history'][-1]['pnl'] == '0.00'
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
    assert trade['payout'] == '5.69'
    assert trade['displayed_multiplier'] == '1.14'
    assert D(trade['payout']) != D(trade['displayed_multiplier'])*D(trade['budget'])
    assert trade['quote_source'] == 'controlled_synthetic_fixture'
    assert trade['ui_parity_verified'] is False

def test_coverage_and_round_reasons_are_durable(live_engine):
    engine, client, _, path = live_engine
    client.quote_ready = False
    engine.tick()
    report = engine.finish()
    row = report['rounds'][0]
    assert row['final_window_scanned'] is True
    assert row['close_time'] and row['rejections']
    assert load(path)['round_reports'][-1]['rounds'] == report['rounds']

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
