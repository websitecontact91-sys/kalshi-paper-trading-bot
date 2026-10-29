"""Opt-in live execution; the default command only checks account readiness.

Scheduled paper jobs never import this module. The live ledger is separate,
and real submission requires both enabled configuration and explicit CLI intent.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import time

import requests

from live_broker import LiveBroker, BrokerError, max_affordable_quantity, worst_case_debit
from live_execution import LiveExecution, CandidateInvalid
from live_persistence import LiveStore
from live_state import new_state
from payout_quote import PayoutQuote, as_decimal
from quote_adapter import documented_taker_cost
from selection import Candidate, distance_pct, rank, rejection_reason
from timing import in_entry_window, observation_fresh, round_key, seconds_to_close
from trade_config import TRADE_BUDGET
from universe import UNIVERSE, series_of


LOG = logging.getLogger('live_runner')


@dataclass(frozen=True)
class LiveSettings:
    enabled: bool = False
    balance_precision: str = '0.01'
    subaccount: int = 0
    duration: int = 600
    interval: int = 2


def settings(path='live_config.json'):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or raw.get('schema') != 'kalshi-live-config.v1':
        raise ValueError('Unsupported live configuration')
    if not isinstance(raw.get('enabled'), bool):
        raise ValueError('Live activation must be an explicit boolean')
    if as_decimal(raw.get('max_total_cost_usd')) != TRADE_BUDGET:
        raise ValueError('Live configuration must match the shared $1 fee-inclusive budget')
    # Cent rounding safely bounds either documented account type. A tighter
    # real-account policy needs separately verified account-specific evidence.
    if raw.get('balance_precision') != '0.01':
        raise ValueError('Live sizing currently requires the conservative cent-precision model')
    subaccount = raw.get('subaccount')
    if isinstance(subaccount, bool) or not isinstance(subaccount, int) or not 0 <= subaccount <= 63:
        raise ValueError('Invalid live subaccount')
    if (raw.get('duration_seconds') != 600 or raw.get('interval_seconds') != 2
            or raw.get('scheduled_cron') != '*/5 * * * *'):
        raise ValueError('Live timing must preserve the requested 600-second scan and five-minute schedule')
    return LiveSettings(raw['enabled'], raw['balance_precision'], subaccount)


class LiveCandidateProvider:
    """Reuse the configured universe, observations, rules and ranking for live sizing."""

    def __init__(self, client, *, balance_precision='0.01', clock=time.time):
        self.client, self.balance_precision, self.clock = client, balance_precision, clock

    def _choices(self, snapshot, now):
        market = snapshot.get('market', {})
        if (not snapshot.get('compatible') or not snapshot.get('quote_ready')
                or market.get('status') not in ('open', 'active')
                or series_of(market) not in UNIVERSE
                or not in_entry_window(market['close_time'], now)
                or not observation_fresh(snapshot.get('observation_time'), now)):
            return []
        result = []
        for side in ('YES', 'NO'):
            detail = snapshot.get('quotes', {}).get(side)
            ask = snapshot.get('asks', {}).get(side)
            if detail is None or ask is None or detail.source != 'documented_fee_model':
                continue
            try:
                if detail.quote.budget > TRADE_BUDGET or detail.quote.debit > TRADE_BUDGET:
                    continue
                multiplier = as_decimal(detail.evidence['fee_multiplier'])
                quantity = max_affordable_quantity(ask, detail.quantity, multiplier,
                    self.balance_precision, TRADE_BUDGET)
                if quantity <= 0:
                    continue
                principal, fees, debit = documented_taker_cost(ask, quantity, multiplier,
                                                               self.balance_precision)
                quote = PayoutQuote(TRADE_BUDGET, debit, quantity)
                candidate = Candidate(market['ticker'], side, ask, snapshot['current'],
                                      market['floor_strike'], quote, True)
                if rejection_reason(candidate) is not None:
                    continue
                values = {'ticker': candidate.ticker, 'side': side,
                    'series_ticker': series_of(market), 'round_key': round_key(market['close_time']),
                    'close_time': market['close_time'], 'observation_time': snapshot['observation_time'],
                    'ask': str(candidate.ask), 'quantity': str(quantity), 'budget': str(TRADE_BUDGET),
                    'debit': str(debit), 'principal': str(principal), 'fees': str(fees),
                    'payout': str(quantity), 'fee_multiplier': str(multiplier),
                    'balance_precision': self.balance_precision,
                    'worst_case_debit': str(worst_case_debit(ask, quantity, multiplier, self.balance_precision)),
                    'current': str(candidate.current), 'target': str(candidate.target),
                    'distance_pct': str(distance_pct(candidate)),
                    'displayed_multiplier': str(quote.displayed_multiplier),
                    'exact_multiplier': str(quote.exact_multiplier),
                    'seconds_to_close': seconds_to_close(market['close_time'], now),
                    'probability_field': snapshot.get('ask_sources', {}).get(side),
                    'quote_source': 'documented_live_cost_bound', 'ui_parity_verified': False,
                    'quote_evidence': {**dict(detail.evidence),
                        'balance_precision': self.balance_precision,
                        'balance_precision_basis': 'conservative_bound_for_either_documented_account_type',
                        'fill_assumption': 'single_equivalent_fill_for_expected_multiplier_only',
                        'cost_bound_assumption': 'each_minimum_quantity_increment_rounds_separately_without_rebates',
                        'account_balance_precision_verified': False}}
                result.append((candidate, values))
            except (ValueError, TypeError, KeyError, ArithmeticError, BrokerError,
                    requests.RequestException, TimeoutError):
                continue
        return result

    def candidates(self, attempted_rounds=()):
        now = self.clock()
        markets = self.client.discover(now)
        in_window = any(in_entry_window(m['close_time'], now) for m in markets)
        snapshots = self.client.batch_snapshots(markets, now, include_quotes=True, final_window=in_window)
        choices, lookup = [], {}
        completed = self.clock()
        for snapshot in snapshots:
            try:
                rows = self._choices(snapshot, completed)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                continue
            for candidate, values in rows:
                if values['round_key'] in attempted_rounds:
                    continue
                choices.append(candidate)
                lookup[(candidate.ticker, candidate.side)] = snapshot['market']
        for ranked in rank(choices):
            market = lookup[(ranked.ticker, ranked.side)]
            if not in_entry_window(market['close_time'], self.clock()):
                continue
            try:
                fresh = self.client.snapshot(market, self.clock(), include_quotes=True)
                if fresh.get('market', {}).get('ticker') != ranked.ticker:
                    continue
                for chosen, values in self._choices(fresh, self.clock()):
                    if chosen.side == ranked.side:
                        yield values
                        break
            except (ValueError, TypeError, KeyError, ArithmeticError, BrokerError,
                    requests.RequestException, TimeoutError):
                continue


def preflight(broker, config):
    """Authenticated GETs only; success does not certify a real fill or settlement."""
    if broker.writes_enabled:
        raise ValueError('A preflight broker must have writes disabled')
    account = broker.balance()
    exchange = broker.exchange_status()
    resting = broker.orders(status='resting')
    positions = broker.positions()
    return {'mode': 'READ_ONLY_PREFLIGHT', 'account_reads_verified': True,
        'real_order_execution_tested': False, 'live_enabled': config.enabled,
        'writes_enabled': False, 'max_total_cost_usd': str(TRADE_BUDGET),
        'balance_precision_for_cost_bound': config.balance_precision,
        'account': account, 'exchange': exchange,
        'existing_resting_order_count': len(resting), 'existing_unsettled_position_rows': len(positions),
        'checked_at_utc': datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight', action='store_true')
    modes.add_argument('--execute-live', action='store_true')
    modes.add_argument('--reconcile-only', action='store_true')
    modes.add_argument('--initialize-state', action='store_true')
    modes.add_argument('--inspect-state', action='store_true')
    parser.add_argument('--confirm-real-money', action='store_true')
    parser.add_argument('--persist-to-github', action='store_true')
    parser.add_argument('--config', default='live_config.json')
    parser.add_argument('--quote-policy', default='quote_policy.json')
    parser.add_argument('--state', default='live_runtime/state.json')
    parser.add_argument('--report-dir', default='live_reports')
    args = parser.parse_args(argv)
    config = settings(args.config)
    if args.execute_live and (not config.enabled or not args.confirm_real_money):
        parser.error('Real-money execution is disabled: enabled configuration and explicit confirmation are required')
    if args.confirm_real_money and not args.execute_live:
        parser.error('Real-money confirmation is valid only with --execute-live')
    if args.execute_live and not args.persist_to_github:
        parser.error('Production execution requires the shared GitHub live journal and serialized workflow')
    if args.persist_to_github and os.environ.get('GITHUB_ACTIONS') != 'true':
        parser.error('Remote live-state persistence requires the configured GitHub Actions environment')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    broker = LiveBroker(subaccount=config.subaccount, allow_trading=bool(args.execute_live))
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    client = None
    try:
        if not (args.execute_live or args.reconcile_only or args.initialize_state or args.inspect_state):
            report = preflight(broker, config)
            (report_dir / 'preflight.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, sort_keys=True))
            return 0
        store = LiveStore(args.state, push=args.persist_to_github)
        with store.locked():
            if args.initialize_state:
                account = broker.balance()
                state = new_state(account_scope=broker.account_scope)
                state['account_cash'] = account['available_balance_usd']
                receipt = store.initialize(state)
                print(json.dumps({'live_state_initialized': True, 'writes_enabled': False,
                                  'account': account, 'persistence': receipt}, sort_keys=True))
                return 0
            state = store.read()
            if args.inspect_state:
                if state['account_scope'] != broker.account_scope:
                    raise ValueError('Live journal belongs to a different account/key scope')
                can_run = state['mode'] not in {'STOPPED', 'HALTED'} or state.get('pending') is not None
                report = {'mode': state['mode'], 'can_run': can_run, 'writes_enabled': False,
                          'pending': state.get('pending') is not None}
                if os.environ.get('GITHUB_OUTPUT'):
                    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
                        output.write('can_run=' + str(can_run).lower() + '\n')
                print(json.dumps(report, sort_keys=True))
                return 0
            from live_client import LiveKalshiClient
            client = LiveKalshiClient(args.quote_policy)
            if client.readiness_reasons:
                LOG.warning('New entries are blocked by quote policy; existing orders can still be reconciled')
            from scripts.workflow_gate import source_metadata
            engine = LiveExecution(broker, state, persist_state=store.persist,
                official_market=client.official_market, armed=bool(args.execute_live),
                source=source_metadata()['source_sha'],
                require_remote_durability=args.persist_to_github)
            if args.reconcile_only:
                engine.reconcile()
            else:
                provider = LiveCandidateProvider(client, balance_precision=config.balance_precision)
                deadline = time.monotonic() + config.duration
                while time.monotonic() < deadline:
                    cycle = time.monotonic()
                    if engine.state.get('pending') is not None:
                        engine.tick()
                    elif engine.state['mode'] == 'SCANNING':
                        try:
                            for candidate in provider.candidates(engine.state['attempted_rounds']):
                                try:
                                    engine.submit(candidate)
                                except CandidateInvalid:
                                    LOG.info('Candidate expired, became stale, or no longer met the strategy rules')
                                    continue
                                if engine.state.get('pending') is not None or engine.state['mode'] != 'SCANNING':
                                    break
                        except (BrokerError, requests.RequestException, TimeoutError) as exc:
                            LOG.warning('Scan skipped after a temporary data-read failure: %s', type(exc).__name__)
                    if engine.source_update or engine.state['mode'] == 'STOPPED':
                        break
                    if engine.state['mode'] == 'HALTED' and engine.state.get('pending') is None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(remaining, max(0, config.interval - (time.monotonic() - cycle))))
            report = {'mode': engine.state['mode'], 'account_scope': engine.state['account_scope'],
                'realized_pnl': engine.state['realized_pnl'], 'completed': len(engine.state['history']),
                'pending': engine.state.get('pending') is not None,
                'halt_reason': engine.state.get('halt_reason'), 'source_update': engine.source_update}
            (report_dir / 'execution-summary.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, sort_keys=True))
            return 0
    finally:
        if client is not None:
            client.close()
        broker.close()


if __name__ == '__main__':
    raise SystemExit(main())
