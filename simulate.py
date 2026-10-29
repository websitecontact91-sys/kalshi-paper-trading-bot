"""Controlled synthetic fixtures driving the actual PaperEngine; never live data."""
import argparse
import copy
import json
from decimal import Decimal as D
from pathlib import Path
import tempfile

from paper_runner import PaperEngine, iso
from payout_quote import PayoutQuote
from persistence import persist
from quote_adapter import QuoteDetails
from state_store import can_enter, load, new_state
from timing import round_key
from trade_config import TRADE_BUDGET

SCENARIOS = ('win', 'loss', 'tie', 'no_candidate', 'late_expiry', 'delayed_result')


class FakeClock:
    def __init__(self, now=1793294947.1):
        self.now = now
    def __call__(self):
        return self.now


class FakeKalshiClient:
    """A deterministic injected client, not a copy of the trading strategy."""
    readiness_reasons = []
    discovery_errors = {}
    def __init__(self, clock, *, close=1793295000, tickers=('KXSOL15M-TEST',), ask='0.87', current='101'):
        self.clock, self.close_time = clock, close
        self.ask, self.current = ask, current
        self.markets = [{'ticker': t, 'series_ticker': t.split('-')[0], 'close_time': iso(close),
            'status': 'active', 'floor_strike': '100', 'market_type': 'binary',
            'notional_value_dollars': '1.00', 'strike_type': 'greater_or_equal',
            'yes_ask_dollars': ask, 'no_ask_dollars': '0.14'} for t in tickers]
        self.result, self.status = '', 'closed'
        self.snapshot_hook = None
        self.snapshots, self.discoveries = [], 0
        self.quote_ready, self.compatible, self.stale = True, True, False

    def discover(self, now=None):
        self.discoveries += 1
        return copy.deepcopy(self.markets)

    def _snapshot(self, market):
        q = PayoutQuote.from_kalshi(TRADE_BUDGET, '0.9921', '1.13')
        detail = QuoteDetails(q, D('1.13'), D('0.0090'), D('0.9831'),
            'controlled_synthetic_fixture', False, {'synthetic': True})
        return {'market': copy.deepcopy(market), 'current': D(self.current),
            'observation_time': self.clock() - (16 if self.stale else 0),
            'compatible': self.compatible, 'compatibility_reason': 'synthetic_contract',
            'quotes': {'YES': detail, 'NO': detail},
            'asks': {'YES': D(market['yes_ask_dollars']), 'NO': D('0.14')},
            'rejections': {}, 'quote_ready': self.quote_ready,
            'quote_blocker': None if self.quote_ready else 'synthetic_is_not_live_quote_proof'}

    def batch_snapshots(self, markets, now=None, include_quotes=True, final_window=False):
        return [self._snapshot(m) for m in markets]

    def snapshot(self, market, now=None, include_quotes=True):
        self.snapshots.append(market['ticker'])
        result = self._snapshot(market)
        if self.snapshot_hook:
            self.snapshot_hook(result)
        return result

    def official_market(self, ticker):
        return {'ticker': ticker, 'status': self.status, 'result': self.result}

    def close(self):
        pass


def run_scenario(name):
    if name not in SCENARIOS:
        raise ValueError('unknown scenario')
    with tempfile.TemporaryDirectory(prefix='kalshi-paper-simulation-') as directory:
        path = Path(directory) / 'state.json'
        clock = FakeClock()
        tickers = ('KXSOL15M-TEST', 'KXBTC15M-TEST') if name == 'tie' else ('KXSOL15M-TEST',)
        client = FakeKalshiClient(clock, tickers=tickers, ask='0.79' if name == 'no_candidate' else '0.87')
        def durable(state):
            return persist(state, path, source_sha='controlled_simulation')
        durable(new_state(session_id='simulation-' + name))
        engine = PaperEngine(client, load(path), persist_state=durable, paper=True,
                             clock=clock, source='controlled_simulation', report_dir=Path(directory)/'reports')
        if name == 'late_expiry':
            def expire(_):
                clock.now = client.close_time
            client.snapshot_hook = expire
        engine.tick()
        if name in ('no_candidate', 'late_expiry'):
            assert engine.state['pending'] is None and not engine.state['rounds_traded']
            assert D(engine.state['balance']) == D('1000')
            assert not engine.state['history']
        else:
            assert engine.state['pending'] is not None
            assert len(engine.state['rounds_traded']) == 1
            assert D(engine.state['balance']) == D('999.0079')
            assert abs(engine.state['pending']['seconds_to_close']-52.9) < 0.001
            if name == 'tie':
                assert engine.state['pending']['ticker'] == 'KXBTC15M-TEST'
                assert len(client.snapshots) == 1
            clock.now = client.close_time + 1
            if name == 'delayed_result':
                engine.tick()
                assert engine.state['mode'] == 'WAITING_FOR_SETTLEMENT'
                assert load(path)['pending'] is not None
                engine = PaperEngine(client, load(path), persist_state=durable, paper=True,
                                     clock=clock, source='controlled_simulation')
            client.status, client.result = 'settled', 'no' if name == 'loss' else 'yes'
            engine.tick()
            assert engine.state['pending'] is None
            assert len(engine.state['history']) == 1
            expected = D('999.0079') if name == 'loss' else D('1000.1379')
            assert D(engine.state['balance']) == expected
            if name == 'loss':
                restarted = PaperEngine(client, load(path), persist_state=durable, paper=True, clock=clock)
                discoveries = client.discoveries
                assert restarted.tick() == 'STOPPED'
                assert client.discoveries == discoveries
            else:
                assert engine.state['mode'] == 'SCANNING'
                assert can_enter(engine.state, round_key(client.close_time+900))
        coverage = engine.finish()
        persisted = load(path)
        assert persisted['balance'] == engine.state['balance']
        return {'scenario': name, 'status': 'PASS', 'evidence_type': 'controlled_synthetic_simulation',
                'mode': persisted['mode'], 'balance': persisted['balance'],
                'entries': len(persisted['rounds_traded']), 'settled_trades': len(persisted['history']),
                'payout': persisted['history'][0]['payout'] if persisted['history'] else None,
                'coverage_rounds': len(coverage['rounds'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', required=True, choices=(*SCENARIOS, 'all'))
    args = parser.parse_args(argv)
    for name in SCENARIOS if args.scenario == 'all' else (args.scenario,):
        print(json.dumps(run_scenario(name), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
