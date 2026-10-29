"""Offline live-runner integration: selection, activation and GET-only preflight."""
import copy
from contextlib import contextmanager
from decimal import Decimal as D
import json

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

import live_runner
from live_broker import LiveBroker
from live_execution import CandidateInvalid, iso
from live_persistence import LivePersistenceError
from live_runner import LiveCandidateProvider, LiveSettings, settings
from live_state import new_state, save, load, apply_balance, FUNDS_POLICY
from payout_quote import PayoutQuote
from quote_adapter import QuoteDetails, build_modeled_quote
from timing import round_key


NOW = 1793304870


@pytest.fixture(autouse=True)
def no_actual_network(monkeypatch):
    monkeypatch.delenv('KALSHI_API_KEY_ID', raising=False)
    monkeypatch.delenv('KALSHI_PRIVATE_KEY', raising=False)
    def blocked(*args, **kwargs):
        pytest.fail('A runner test attempted actual network access')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def snapshot(ticker='KXBTC15M-TEST', *, now=NOW, remaining=30, price='.89',
             current='101', side='YES'):
    series = ticker.split('-')[0]
    market = {'ticker': ticker, 'series_ticker': series, 'status': 'active',
              'close_time': now + remaining, 'floor_strike': '100',
              'market_type': 'binary', 'notional_value_dollars': '1.0000'}
    detail = build_modeled_quote(price, '100', {'fee_type': 'quadratic', 'fee_multiplier': '1'},
        {'fee_type_override': None, 'fee_multiplier_override': None}, market,
        balance_precision='0.0001', now=now)
    return {'market': market, 'compatible': True, 'quote_ready': True,
            'observation_time': now, 'current': current, 'asks': {side: D(price)},
            'quotes': {side: detail}, 'ask_sources': {side: 'observed_orderbook'}}


class MarketData:
    def __init__(self, snapshots, clock=None):
        self.snapshots = snapshots
        self.clock = clock or Clock()
        self.refresh_hook = None
        self.refreshed = []
        self.batch_arguments = None

    def discover(self, now):
        return [copy.deepcopy(row['market']) for row in self.snapshots]

    def batch_snapshots(self, markets, now, **kwargs):
        self.batch_arguments = kwargs
        return copy.deepcopy(self.snapshots)

    def snapshot(self, market, now, **kwargs):
        self.refreshed.append(market['ticker'])
        row = copy.deepcopy(next(x for x in self.snapshots if x['market']['ticker'] == market['ticker']))
        return self.refresh_hook(row) if self.refresh_hook else row


def candidates(rows, clock=None):
    clock = clock or Clock()
    client = MarketData(rows, clock)
    return LiveCandidateProvider(client, clock=clock), client


def test_live_sizing_uses_one_dollar_including_conservative_fragmented_fees():
    row = snapshot()
    assert row['quotes']['YES'].quantity > D('1')
    provider, client = candidates([row])
    selected = list(provider.candidates())
    assert len(selected) == 1
    trade = selected[0]
    assert D(trade['budget']) == D('1.00')
    assert D(trade['quantity']) == D('1.00')
    assert D(trade['debit']) == D('.90')
    assert D(trade['payout']) == D('1.00')
    assert D(trade['principal']) + D(trade['fees']) == D(trade['debit'])
    assert D(trade['worst_case_debit']) == D('1.00')
    assert trade['balance_precision'] == '.01' or trade['balance_precision'] == '0.01'
    assert trade['displayed_multiplier'] == '1.11'
    assert trade['ui_parity_verified'] is False
    assert client.batch_arguments == {'include_quotes': True, 'final_window': True}


def test_live_quotes_rank_by_signed_distance_before_payout_multiple():
    provider, _ = candidates([snapshot('KXETH15M-TEST', current='100.5', price='.85'),
                              snapshot('KXBTC15M-TEST', current='101', price='.89')])
    selected = list(provider.candidates())
    assert [x['ticker'] for x in selected] == ['KXBTC15M-TEST', 'KXETH15M-TEST']
    assert D(selected[0]['exact_multiplier']) < D(selected[1]['exact_multiplier'])


def test_live_rank_tie_uses_exact_multiple_and_does_not_require_positive_distance():
    provider, _ = candidates([snapshot('KXBTC15M-TEST', current='99', price='.89'),
                              snapshot('KXETH15M-TEST', current='99', price='.85')])
    selected = list(provider.candidates())
    assert [x['ticker'] for x in selected] == ['KXETH15M-TEST', 'KXBTC15M-TEST']
    assert all(D(x['distance_pct']) == D('-1') for x in selected)


def test_no_distance_uses_target_minus_current_and_retains_outcome_ask():
    provider, _ = candidates([snapshot(side='NO', current='99', price='.89')])
    selected = list(provider.candidates())[0]
    assert selected['side'] == 'NO'
    assert D(selected['ask']) == D('.89')
    assert D(selected['distance_pct']) == D('1')


@pytest.mark.parametrize('remaining,expected', [(61, False), (60, True), (1, True), (0, False), (-1, False)])
def test_provider_obeys_final_minute_boundaries(remaining, expected):
    provider, _ = candidates([snapshot(remaining=remaining)])
    assert bool(list(provider.candidates())) is expected


@pytest.mark.parametrize('change', [
    {'compatible': False}, {'quote_ready': False}, {'observation_time': NOW - 16},
    {'observation_time': NOW + 6}, {'current': 'NaN'},
])
def test_invalid_or_stale_candidate_does_not_reach_selection(change):
    row = snapshot()
    row.update(change)
    provider, _ = candidates([row])
    assert list(provider.candidates()) == []


@pytest.mark.parametrize('price', ['.79', '.80', '.91'])
def test_ask_and_fee_inclusive_payout_bands_both_apply(price):
    # At 80c, the conservatively rounded $1-contract example displays 1.22x,
    # so meeting the ask band alone is not enough.
    provider, _ = candidates([snapshot(price=price)])
    assert list(provider.candidates()) == []


def test_unconfigured_series_and_already_attempted_round_are_excluded():
    provider, _ = candidates([snapshot('UNKNOWN15M-TEST')])
    assert list(provider.candidates()) == []
    row = snapshot()
    provider, client = candidates([row])
    assert list(provider.candidates([round_key(row['market']['close_time'])])) == []
    assert client.refreshed == []


def test_old_five_dollar_quote_cannot_be_reused_as_a_live_entry():
    row = snapshot()
    row['quotes']['YES'] = QuoteDetails(PayoutQuote('5', '4.99', '5.6'), D('5.6'),
        D('.006'), D('4.984'), 'documented_fee_model', evidence={'fee_multiplier': '1'})
    provider, _ = candidates([row])
    assert list(provider.candidates()) == []


@pytest.mark.parametrize('reason', ['expired', 'stale', 'identity', 'price_changed'])
def test_final_refresh_revalidates_time_reference_identity_and_executable_price(reason):
    clock = Clock()
    row = snapshot(remaining=50)
    provider, client = candidates([row], clock)
    def refresh(fresh):
        if reason == 'expired':
            clock.now = fresh['market']['close_time']
        elif reason == 'stale':
            clock.now += 16
        elif reason == 'identity':
            fresh['market']['ticker'] = 'KXBTC15M-OTHER'
        else:
            fresh = snapshot(price='.91', remaining=50)
        return fresh
    client.refresh_hook = refresh
    assert list(provider.candidates()) == []


def test_changed_best_quote_falls_back_to_next_ranked_market():
    provider, client = candidates([snapshot('KXBTC15M-TEST', current='102'),
                                   snapshot('KXETH15M-TEST', current='101')])
    def refresh(fresh):
        if fresh['market']['ticker'] == 'KXBTC15M-TEST':
            return snapshot('KXBTC15M-TEST', price='.91', current='102')
        return fresh
    client.refresh_hook = refresh
    assert [x['ticker'] for x in provider.candidates()] == ['KXETH15M-TEST']


def test_one_market_refresh_timeout_falls_back_without_mutating_an_account():
    provider, client = candidates([snapshot('KXBTC15M-TEST', current='102'),
                                   snapshot('KXETH15M-TEST', current='101')])
    def refresh(fresh):
        if fresh['market']['ticker'] == 'KXBTC15M-TEST':
            raise requests.Timeout('synthetic read timeout')
        return fresh
    client.refresh_hook = refresh
    assert [x['ticker'] for x in provider.candidates()] == ['KXETH15M-TEST']


def config_file(tmp_path, **overrides):
    data = {'schema': 'kalshi-live-config.v1', 'enabled': False, 'max_total_cost_usd': '1.00',
        'balance_precision': '0.01', 'subaccount': 0, 'duration_seconds': 600,
        'interval_seconds': 2, 'scheduled_cron': '*/5 * * * *'}
    data.update(overrides)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize('changes', [{'enabled': 'true'}, {'max_total_cost_usd': '5.00'},
    {'balance_precision': '0.0001'}, {'subaccount': True}, {'subaccount': -1},
    {'duration_seconds': 601}, {'interval_seconds': 5}, {'scheduled_cron': '*/10 * * * *'}])
def test_settings_fail_closed_on_unsafe_or_inconsistent_configuration(tmp_path, changes):
    with pytest.raises((TypeError, ValueError)):
        settings(config_file(tmp_path, **changes))


@pytest.mark.parametrize('enabled,args', [
    (False, ['--execute-live']), (False, ['--execute-live', '--confirm-real-money']),
    (True, ['--execute-live']), (True, ['--preflight', '--confirm-real-money']),
    (True, ['--reconcile-only', '--confirm-real-money']),
])
def test_activation_gates_run_before_account_client_is_created(tmp_path, monkeypatch, enabled, args):
    def forbidden(**kwargs):
        pytest.fail('Invalid activation reached the account client')
    monkeypatch.setattr(live_runner, 'LiveBroker', forbidden)
    with pytest.raises(SystemExit) as error:
        live_runner.main(['--config', str(config_file(tmp_path, enabled=enabled)), *args])
    assert error.value.code == 2


def test_remote_persistence_requires_actions_environment_before_account_reads(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setattr(live_runner, 'LiveBroker', lambda **kw: pytest.fail('Account client created'))
    with pytest.raises(SystemExit):
        live_runner.main(['--config', str(config_file(tmp_path)), '--persist-to-github'])


@pytest.mark.parametrize('actions_environment', [False, True])
def test_execution_requires_shared_remote_journal_before_account_client(
        tmp_path, monkeypatch, capsys, actions_environment):
    if actions_environment:
        monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    else:
        monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setattr(live_runner, 'LiveBroker', lambda **kw: pytest.fail('Account client created'))
    with pytest.raises(SystemExit) as error:
        live_runner.main(['--config', str(config_file(tmp_path, enabled=True)),
                          '--execute-live', '--confirm-real-money'])
    assert error.value.code == 2
    assert 'shared GitHub live journal' in capsys.readouterr().err


def inspection_setup(tmp_path, monkeypatch, *, mode, pending=False, other_account=False,
                     cash=None, legacy=False):
    class NoAccountHTTP:
        closed = False
        reads = 0
        def get(self, *args, **kwargs):
            self.reads += 1
            if cash is None:
                pytest.fail('This state inspection must not read the account over HTTP')
            if isinstance(cash, Exception):
                raise cash
            class Response:
                status_code, headers = 200, {}
                text = json.dumps({'balance_dollars': cash, 'portfolio_value': 0, 'updated_ts': NOW})
            return Response()
        def post(self, *args, **kwargs):
            pytest.fail('State inspection attempted an account mutation')
        def close(self):
            self.closed = True
    transport = NoAccountHTTP()
    broker = LiveBroker(session=transport, api_key_id='inspection-test-key',
                        private_key=ed25519.Ed25519PrivateKey.generate())
    def factory(**kwargs):
        assert kwargs == {'subaccount': 0, 'allow_trading': False}
        assert broker.writes_enabled is False
        return broker
    monkeypatch.setattr(live_runner, 'LiveBroker', factory)
    state = new_state(session_id='inspection-test',
                      account_scope='different-account' if other_account else broker.account_scope)
    state['mode'] = mode
    if mode == 'STOPPED':
        if legacy:
            state.pop('funds_policy')
            state.pop('stop_reason')
        else:
            state = apply_balance(state, '.25', iso(NOW - 60))
    if mode == 'HALTED':
        state['halt_reason'] = 'synthetic_manual_review_required'
    if pending:
        key = round_key(NOW + 30)
        state['attempted_rounds'] = [key]
        state['pending'] = {'ticker': 'KXBTC15M-TEST', 'side': 'YES', 'round_key': key,
            'client_order_id': 'e9d23ab6-d649-4ce9-abef-d28d7c187db0', 'close_time': NOW + 30,
            'budget': '1.00', 'quoted_debit': '0.90', 'quantity': '1.00', 'ask': '0.89',
            'fee_multiplier': '1', 'balance_precision': '0.01', 'execution': None}
    state_path = tmp_path / 'state.json'
    save(state_path, state)
    original = state_path.read_bytes()
    github_output = tmp_path / 'github_output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(github_output))
    args = ['--inspect-state', '--config', str(config_file(tmp_path)),
            '--state', str(state_path), '--report-dir', str(tmp_path / 'reports')]
    return args, transport, state_path, original, github_output


def test_inspection_blocks_execution_halt_without_reading_account(
        tmp_path, monkeypatch, capsys):
    args, transport, path, original, output = inspection_setup(tmp_path, monkeypatch, mode='HALTED')
    assert live_runner.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        'mode': 'HALTED', 'can_run': False, 'pending': False, 'writes_enabled': False,
        'stop_reason': None, 'funds_check': 'HALTED', 'account_cash': None}
    assert output.read_text() == 'can_run=false\n'
    assert path.read_bytes() == original and transport.closed


@pytest.mark.parametrize('mode', ['RECONCILING', 'HALTED'])
def test_inspection_allows_pending_reconciliation_with_order_writes_disabled(
        tmp_path, monkeypatch, capsys, mode):
    args, transport, path, original, output = inspection_setup(
        tmp_path, monkeypatch, mode=mode, pending=True)
    assert live_runner.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        'mode': mode, 'can_run': True, 'pending': True, 'writes_enabled': False,
        'stop_reason': None, 'funds_check': mode, 'account_cash': None}
    assert output.read_text() == 'can_run=true\n'
    assert path.read_bytes() == original and transport.closed


def test_inspection_rejects_wrong_account_scope_before_emitting_run_permission(
        tmp_path, monkeypatch, capsys):
    args, transport, path, original, output = inspection_setup(
        tmp_path, monkeypatch, mode='SCANNING', other_account=True)
    with pytest.raises(ValueError, match='different account/key scope'):
        live_runner.main(args)
    assert not output.exists() and capsys.readouterr().out == ''
    assert path.read_bytes() == original and transport.closed


@pytest.mark.parametrize('cash,mode,can_run', [('.9999', 'STOPPED', False), ('1.00', 'SCANNING', True)])
def test_inspection_refreshes_funds_stop_and_resumes_only_with_enough_cash(
        tmp_path, monkeypatch, capsys, cash, mode, can_run):
    args, transport, path, original, output = inspection_setup(
        tmp_path, monkeypatch, mode='STOPPED', cash=cash)
    before = load(path)
    assert live_runner.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['mode'] == mode and report['can_run'] is can_run
    assert D(report['account_cash']) == D(cash)
    assert report['stop_reason'] == ('insufficient_funds' if not can_run else None)
    assert report['writes_enabled'] is False and transport.reads == 1 and transport.closed
    assert output.read_text() == 'can_run=' + str(can_run).lower() + '\n'
    after = load(path)
    for field in ('session_id', 'history', 'attempts', 'attempted_rounds', 'realized_pnl'):
        assert after[field] == before[field]
    if can_run:
        assert after['mode'] == 'SCANNING' and after['stop_reason'] is None


def test_inspection_migrates_legacy_stop_with_get_only_balance_read(tmp_path, monkeypatch, capsys):
    args, transport, path, original, output = inspection_setup(
        tmp_path, monkeypatch, mode='STOPPED', cash='1.00', legacy=True)
    assert live_runner.main(args) == 0
    assert json.loads(capsys.readouterr().out)['can_run'] is True
    assert load(path)['funds_policy'] == FUNDS_POLICY
    assert load(path)['mode'] == 'SCANNING' and transport.reads == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_inspection_balance_outage_cannot_restart_a_stopped_journal(
        tmp_path, monkeypatch, capsys, legacy):
    args, transport, path, original, output = inspection_setup(
        tmp_path, monkeypatch, mode='STOPPED', cash=TimeoutError('synthetic'), legacy=legacy)
    assert live_runner.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['mode'] == 'STOPPED' and report['can_run'] is False
    assert report['funds_check'] == 'BLOCKED_BALANCE_UNAVAILABLE'
    assert path.read_bytes() == original and output.read_text() == 'can_run=false\n'
    assert transport.closed


@pytest.mark.parametrize('enabled', [False, True])
def test_default_main_uses_only_authenticated_get_and_does_not_initialize_state(tmp_path, monkeypatch, enabled):
    class Response:
        status_code, headers = 200, {}
        def __init__(self, value):
            self.text = json.dumps(value)
    class ReadsOnly:
        def __init__(self):
            self.calls, self.closed = [], False
            self.responses = [Response({'balance_dollars': '19.8499', 'portfolio_value': 9, 'updated_ts': NOW}),
                Response({'exchange_active': True, 'trading_active': True}),
                Response({'orders': [], 'cursor': ''}),
                Response({'market_positions': [], 'cursor': ''})]
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)
        def post(self, *args, **kwargs):
            pytest.fail('Default preflight attempted an account mutation')
        def close(self):
            self.closed = True
    transport = ReadsOnly()
    def factory(**kwargs):
        assert kwargs == {'subaccount': 0, 'allow_trading': False}
        return LiveBroker(session=transport, api_key_id='test-key',
            private_key=ed25519.Ed25519PrivateKey.generate(), **kwargs)
    monkeypatch.setattr(live_runner, 'LiveBroker', factory)
    monkeypatch.setattr(live_runner, 'LiveStore', lambda *a, **kw: pytest.fail('Preflight touched live state'))
    report_dir = tmp_path / 'reports'
    assert live_runner.main(['--config', str(config_file(tmp_path, enabled=enabled)),
                             '--report-dir', str(report_dir)]) == 0
    assert len(transport.calls) == 4 and transport.closed
    report = json.loads((report_dir / 'preflight.json').read_text())
    assert report['live_enabled'] is enabled
    assert report['writes_enabled'] is False and report['real_order_execution_tested'] is False
    assert report['account']['available_balance_usd'] == '19.8499'
    assert report['minimum_available_cash_usd'] == '1.00'
    assert report['sufficient_funds_for_new_trade'] is True


@pytest.mark.parametrize('cash,mode', [('.9999', 'STOPPED'), ('1.00', 'SCANNING')])
def test_explicit_initialization_records_funds_mode_without_orders(tmp_path, monkeypatch, cash, mode):
    class Account:
        account_scope = 'synthetic-initialization'
        def balance(self):
            return {'available_balance_usd': cash}
        def close(self):
            pass
    def factory(**kwargs):
        assert kwargs['allow_trading'] is False
        return Account()
    monkeypatch.setattr(live_runner, 'LiveBroker', factory)
    path = tmp_path / 'initialized.json'
    assert live_runner.main(['--initialize-state', '--config', str(config_file(tmp_path)),
        '--state', str(path), '--report-dir', str(tmp_path / 'reports')]) == 0
    state = load(path)
    assert state['mode'] == mode and D(state['account_cash']) == D(cash)
    assert state['stop_reason'] == ('insufficient_funds' if mode == 'STOPPED' else None)
    assert state['history'] == [] and state['pending'] is None


@pytest.mark.parametrize('cash,expected_scan,expected_mode', [
    ('.9999', False, 'STOPPED'), ('1.00', True, 'SCANNING'),
    (TimeoutError('synthetic'), False, 'SCANNING')])
def test_runner_checks_cash_before_market_scan_even_without_candidates(
        tmp_path, monkeypatch, cash, expected_scan, expected_mode):
    import live_client
    events, saved = [], []
    class Account:
        account_scope = 'synthetic-loop-account'
        writes_enabled = True
        def __init__(self, **kwargs):
            pass
        def balance(self):
            events.append('balance')
            if isinstance(cash, Exception):
                raise cash
            return {'available_balance_usd': cash}
        def close(self):
            pass
    class Store:
        def __init__(self, *args, **kwargs):
            pass
        @contextmanager
        def locked(self):
            yield self
        def read(self):
            return new_state(account_scope=Account.account_scope)
        def persist(self, state):
            saved.append(copy.deepcopy(state))
            return {'durable': True, 'remote': True}
    class Market:
        readiness_reasons = []
        def __init__(self, *args):
            pass
        def official_market(self, ticker):
            pytest.fail('No pending order exists')
        def close(self):
            pass
    class Provider:
        def __init__(self, *args, **kwargs):
            pass
        def candidates(self, attempted):
            events.append('scan')
            return iter(())
    ticks = iter(range(100, 200))
    monkeypatch.setattr(live_runner.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(live_runner.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(live_runner, 'settings', lambda path: LiveSettings(enabled=True, duration=3))
    monkeypatch.setattr(live_runner, 'LiveBroker', Account)
    monkeypatch.setattr(live_runner, 'LiveStore', Store)
    monkeypatch.setattr(live_runner, 'LiveCandidateProvider', Provider)
    monkeypatch.setattr(live_client, 'LiveKalshiClient', Market)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    report_dir = tmp_path / 'reports'
    assert live_runner.main(['--execute-live', '--confirm-real-money', '--persist-to-github',
        '--report-dir', str(report_dir)]) == 0
    assert events == (['balance', 'scan'] if expected_scan else ['balance'])
    report = json.loads((report_dir / 'execution-summary.json').read_text())
    assert report['mode'] == expected_mode
    if expected_mode == 'STOPPED':
        assert saved[-1]['stop_reason'] == report['stop_reason'] == 'insufficient_funds'


def isolated_loop(tmp_path, monkeypatch, raised, *, quote_blockers=()):
    import live_client
    calls = []
    class Account:
        def __init__(self, **kwargs):
            self.account_scope = 'synthetic-account'
        def close(self):
            calls.append('account_closed')
    class Store:
        def __init__(self, *args, **kwargs):
            self.held = False
        @contextmanager
        def locked(self):
            self.held = True
            calls.append('locked')
            try:
                yield self
            finally:
                self.held = False
                calls.append('unlocked')
        def read(self):
            assert self.held
            return new_state(account_scope='synthetic-account')
        def persist(self, state):
            pytest.fail('This failure-path test must not persist or submit')
    class Market:
        readiness_reasons = list(quote_blockers)
        def __init__(self, *args):
            pass
        def official_market(self, ticker):
            pytest.fail('No owned order exists in this test')
        def close(self):
            calls.append('market_closed')
    class Engine:
        source_update = None
        def __init__(self, broker, state, **kwargs):
            self.state = state
        def submit(self, candidate):
            calls.append('candidate_rejected')
            raise raised
        def check_funds(self):
            return 'SCANNING'
        def reconcile(self):
            calls.append('reconcile')
            return self.state['mode']
    class Provider:
        def __init__(self, *args, **kwargs):
            pass
        def candidates(self, attempted):
            yield {'synthetic': True}
    ticks = iter(range(100, 200))
    monkeypatch.setattr(live_runner.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(live_runner.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(live_runner, 'settings', lambda path: LiveSettings(enabled=True, duration=3))
    monkeypatch.setattr(live_runner, 'LiveBroker', Account)
    monkeypatch.setattr(live_runner, 'LiveStore', Store)
    monkeypatch.setattr(live_client, 'LiveKalshiClient', Market)
    monkeypatch.setattr(live_runner, 'LiveExecution', Engine)
    monkeypatch.setattr(live_runner, 'LiveCandidateProvider', Provider)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    args = ['--execute-live', '--confirm-real-money', '--persist-to-github',
            '--report-dir', str(tmp_path / 'reports')]
    return calls, args


def test_expiry_during_account_checks_skips_candidate_and_releases_store(tmp_path, monkeypatch):
    calls, args = isolated_loop(tmp_path, monkeypatch, CandidateInvalid('reference_expired'))
    assert live_runner.main(args) == 0
    assert calls == ['locked', 'candidate_rejected', 'unlocked', 'market_closed', 'account_closed']


def test_durability_failure_is_fatal_and_not_treated_as_a_temporary_scan_failure(tmp_path, monkeypatch):
    calls, args = isolated_loop(tmp_path, monkeypatch, LivePersistenceError('remote receipt missing'))
    with pytest.raises(LivePersistenceError):
        live_runner.main(args)
    assert calls == ['locked', 'candidate_rejected', 'unlocked', 'market_closed', 'account_closed']


def test_reconcile_only_remains_available_when_new_entry_quote_policy_is_unready(tmp_path, monkeypatch):
    calls, args = isolated_loop(tmp_path, monkeypatch, None, quote_blockers=['quote_policy_unavailable'])
    args = ['--reconcile-only', *args[2:]]
    assert live_runner.main(args) == 0
    assert calls == ['locked', 'reconcile', 'unlocked', 'market_closed', 'account_closed']
