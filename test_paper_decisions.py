
"""Offline tests for the Kalshi paper bot's trading decisions."""
import time
from unittest.mock import patch

from paper_runner import (
    choose,
    default_state,
    evaluate_market,
    load_state,
    settle,
)


def market(yes_ask=0.85, no_ask=0.15, target=100):
    return {
        'ticker': 'KXBTC15M-TEST',
        'series_ticker': 'KXBTC15M',
        'status': 'open',
        'strike_type': 'greater_or_equal',
        'floor_strike': target,
        'close_time': '2030-01-01T00:15:00Z',
        'yes_ask_dollars': str(yes_ask),
        'no_ask_dollars': str(no_ask),
    }


def orderbook():
    return {
        'orderbook': {
            'no': [[15, 20]],
            'yes': [[85, 20]],
        }
    }


def test_price_and_payout_filter():
    m = market()
    t = 1893456845.0  # 55 seconds before close
    with patch('paper_runner.best_bid_depth', return_value=20):
        candidates = evaluate_market(m, 105, t, orderbook(), 1000)
    assert len(candidates) == 1
    assert candidates[0]['side'] == 'YES'
    assert 0.80 <= candidates[0]['ask'] <= 0.90
    assert 1.10 <= candidates[0]['gross_payout'] <= 1.20


def test_no_entry_before_final_minute():
    with patch('paper_runner.best_bid_depth', return_value=20):
        candidates = evaluate_market(
            market(), 105, 1893456780.0, orderbook(), 1000
        )
    assert candidates == []


def test_total_cost_never_exceeds_five_dollars():
    with patch('paper_runner.best_bid_depth', return_value=20):
        candidates = evaluate_market(
            market(), 105, 1893456845.0, orderbook(), 1000
        )
    assert candidates
    assert all(c['cost'] <= 5.0 for c in candidates)


def test_largest_distance_wins():
    candidates = [
        {'ticker': 'A', 'side': 'YES', 'distance_pct': 2,
         'gross_payout': 1.18},
        {'ticker': 'B', 'side': 'YES', 'distance_pct': 5,
         'gross_payout': 1.12},
    ]
    assert choose(candidates)['ticker'] == 'B'


def test_loss_stops_bot():
    state = default_state()
    state['mode'] = 'WAITING_FOR_SETTLEMENT'
    state['trade'] = {
        'ticker': 'TEST', 'side': 'YES',
        'count': 5, 'cost': 4.75
    }

    class Client:
        def market(self, ticker):
            return {'result': 'no'}

    assert settle(state, Client()) is True
    assert state['mode'] == 'STOPPED'
    assert state['history'][0]['won'] is False


def test_stopped_state_survives_restart(tmp_path):
    path = tmp_path / 'paper_state.json'
    state = default_state()
    state['mode'] = 'STOPPED'
    state['history'] = [{'won': False}]
    import json
    path.write_text(json.dumps(state))
    restored = load_state(path)
    assert restored['mode'] == 'STOPPED'


def test_losing_side_is_never_selected():
    m = market(yes_ask=0.85, no_ask=0.15)
    with patch('paper_runner.best_bid_depth', return_value=20):
        assert evaluate_market(m, 95, 1893456845.0, orderbook(), 1000) == []


def test_no_side_can_qualify_at_exact_strike():
    m = market(yes_ask=0.85, no_ask=0.85)
    with patch('paper_runner.best_bid_depth', return_value=20):
        assert evaluate_market(m, 100, 1893456845.0, orderbook(), 1000) == []


def test_global_round_id_groups_nearby_close_times():
    from paper_runner import round_id
    assert round_id('2030-01-01T00:15:00Z') == round_id('2030-01-01T00:14:59Z')
    assert round_id('2030-01-01T00:15:01Z') != round_id('2030-01-01T00:15:00Z')


def test_evaluated_trade_has_global_round_id():
    from paper_runner import round_id
    with patch('paper_runner.best_bid_depth', return_value=20):
        trades = evaluate_market(market(), 105, 1893456845.0, orderbook(), 1000)
    assert trades[0]['round_id'] == round_id(market()['close_time'])


def test_win_credits_payout_and_resumes_scanning():
    state = default_state()
    state['balance'] = 995.25
    state['mode'] = 'WAITING_FOR_SETTLEMENT'
    state['trade'] = {'ticker': 'TEST', 'side': 'YES',
                      'count': 5, 'cost': 4.75, 'round_id': '10'}

    class Client:
        def market(self, ticker):
            return {'result': 'yes'}

    assert settle(state, Client()) is True
    assert state['mode'] == 'SCANNING'
    assert state['trade'] is None
    assert state['balance'] == 1000.25
    assert state['history'][0]['won'] is True


def test_unsettled_market_preserves_pending_position():
    state = default_state()
    state['balance'] = 995.25
    state['mode'] = 'WAITING_FOR_SETTLEMENT'
    state['trade'] = {'ticker': 'TEST', 'side': 'YES',
                      'count': 5, 'cost': 4.75}

    class Client:
        def market(self, ticker):
            return {'result': ''}

    assert settle(state, Client()) is False
    assert state['mode'] == 'WAITING_FOR_SETTLEMENT'
    assert state['balance'] == 995.25
    assert state['history'] == []


def test_loss_stops_even_if_balance_remains():
    state = default_state()
    state['balance'] = 995.25
    state['mode'] = 'WAITING_FOR_SETTLEMENT'
    state['trade'] = {'ticker': 'TEST', 'side': 'NO',
                      'count': 5, 'cost': 4.75}

    class Client:
        def market(self, ticker):
            return {'result': 'yes'}

    assert settle(state, Client()) is True
    assert state['mode'] == 'STOPPED'
    assert state['balance'] == 995.25
    assert state['history'][0]['pnl'] == -4.75


def test_freshness_at_selection():
    from paper_runner import candidate_is_fresh
    assert candidate_is_fresh({'index_time': 100}, 110)
    assert not candidate_is_fresh({'index_time': 100}, 116)
    assert not candidate_is_fresh({'index_time': 100}, 94)
    assert not candidate_is_fresh({}, 110)


def test_legacy_round_id_cannot_bypass_global_limit():
    from paper_runner import prior_round_matches, round_id
    close = '2030-01-01T00:15:00Z'
    assert prior_round_matches({'round_id': close}, close)
    assert prior_round_matches({'round_id': round_id(close)}, close)
    assert prior_round_matches({'close_time': close}, close)
    assert not prior_round_matches({'round_id': '2030-01-01T00:30:00Z'}, close)


def test_complete_paper_lifecycle_win_then_loss(tmp_path):
    from paper_runner import persist
    state = default_state()
    state['balance'] = 995.25
    state['mode'] = 'WAITING_FOR_SETTLEMENT'
    state['trade'] = {'ticker': 'ROUND1', 'side': 'YES', 'count': 5,
                      'cost': 4.75, 'round_id': '1'}
    path = tmp_path / 'state.json'
    persist(state, path)
    restored = load_state(path)

    class Results:
        def __init__(self, outcome):
            self.outcome = outcome

        def market(self, ticker):
            return {'result': self.outcome}

    assert not settle(restored, Results(''))
    assert restored['mode'] == 'WAITING_FOR_SETTLEMENT'
    assert settle(restored, Results('yes'))
    assert restored['mode'] == 'SCANNING'
    assert restored['balance'] == 1000.25

    restored['balance'] -= 4.75
    restored['mode'] = 'WAITING_FOR_SETTLEMENT'
    restored['trade'] = {'ticker': 'ROUND2', 'side': 'YES', 'count': 5,
                         'cost': 4.75, 'round_id': '2'}
    assert settle(restored, Results('no'))
    persist(restored, path)
    assert load_state(path)['mode'] == 'STOPPED'
    assert len(restored['history']) == 2

def test_live_kalshi_active_status_is_eligible():
    """Kalshi live market API reports status=active, not just open."""
    m = market()
    m['status'] = 'active'
    with patch('paper_runner.best_bid_depth', return_value=20):
        candidates = evaluate_market(m, 105, 1893456845.0, orderbook(), 1000)
    assert len(candidates) == 1

def test_closed_market_still_ineligible():
    m = market()
    m['status'] = 'closed'
    with patch('paper_runner.best_bid_depth', return_value=20):
        assert evaluate_market(m, 105, 1893456845.0, orderbook(), 1000) == []


def test_exact_five_dollar_fractional_paper_notional():
    """The simulator spends exactly $5, not a $5 cap minus invented fees."""
    with patch('paper_runner.best_bid_depth', return_value=20):
        trades = evaluate_market(market(yes_ask=0.86), 105, 1893456845.0, orderbook(), 1000)
    assert len(trades) == 1
    trade = trades[0]
    assert trade['cost'] == 5.0
    assert trade['fee_reserve'] == 0.0
    assert abs(trade['count'] - 5.0 / 0.86) < 1e-10
    assert trade['count'] != int(trade['count'])  # Hypothetical fractional exposure.


def test_exact_five_dollar_paper_win_and_loss():
    with patch('paper_runner.best_bid_depth', return_value=20):
        trade = evaluate_market(market(yes_ask=0.86), 105, 1893456845.0, orderbook(), 1000)[0]
    class Outcome:
        def __init__(self, result):
            self.result = result
        def market(self, ticker):
            return {'result': self.result}
    win = default_state()
    win['balance'] -= 5.0
    win['trade'] = trade.copy()
    win['mode'] = 'WAITING_FOR_SETTLEMENT'
    assert settle(win, Outcome('yes'))
    assert win['balance'] == round(995.0 + 5.0 / 0.86, 8)
    assert win['mode'] == 'SCANNING'
    loss = default_state()
    loss['balance'] -= 5.0
    loss['trade'] = trade.copy()
    loss['mode'] = 'WAITING_FOR_SETTLEMENT'
    assert settle(loss, Outcome('no'))
    assert loss['balance'] == 995.0
    assert loss['mode'] == 'STOPPED'
