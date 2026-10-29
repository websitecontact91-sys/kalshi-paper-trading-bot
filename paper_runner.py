"""Persistent paper simulation. Live I/O is read-only Kalshi market data."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import json
import logging
import os
from pathlib import Path
import subprocess
import time
from typing import Protocol

from payout_quote import as_decimal
from persistence import persist
from selection import Candidate, distance_pct, rank, rejection_reason
from state_store import (archive_and_reset, can_enter, load, record_entry,
                         record_settlement, record_void, validate)
from timing import in_entry_window, observation_fresh, round_key, seconds_to_close
from universe import UNIVERSE, series_of

LOG = logging.getLogger('paper_runner')


class KalshiClient(Protocol):
    def discover(self, now=None): ...
    def batch_snapshots(self, markets, now=None, include_quotes=True, final_window=False): ...
    def snapshot(self, market, now=None, include_quotes=True): ...
    def official_market(self, ticker): ...
    def close(self): ...


def iso(stamp):
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()


def source_sha():
    if os.environ.get('PAPER_SOURCE_SHA'):
        return os.environ['PAPER_SOURCE_SHA']
    return subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, text=True,
                          capture_output=True).stdout.strip()


class PaperEngine:
    def __init__(self, client: KalshiClient, state, *, persist_state, paper=False,
                 clock=time.time, source='unknown', report_dir=None):
        validate(state)
        self.client, self.state, self.persist_state = client, copy.deepcopy(state), persist_state
        self.paper, self.clock, self.source = paper, clock, source
        self.report_dir = Path(report_dir) if report_dir else None
        if self.report_dir:
            self.report_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = self.clock()
        self.first_scan, self.last_scan = None, None
        self.rounds, self.events = {}, []
        self._last_messages = {}

    def _emit(self, kind, **fields):
        event = {'kind': kind, 'timestamp': iso(self.clock()), 'source_sha': self.source, **fields}
        self.events.append(event)
        LOG.info('%s %s', kind, json.dumps(fields, sort_keys=True, default=str))
        if self.report_dir:
            with (self.report_dir / 'events.jsonl').open('a') as f:
                f.write(json.dumps(event, sort_keys=True, default=str) + '\n')

    def _commit(self, updated, kind, **fields):
        receipt = self.persist_state(updated)
        self.state = updated
        self._emit(kind, **fields, balance=self.state['balance'], mode=self.state['mode'],
                   persistence=receipt)

    def _round(self, key):
        return self.rounds.setdefault(key, {'round_key': key, 'final_window_scanned': False,
            'close_time': iso(int(key) * 900), 'scan_count': 0, 'reason': 'monitor_only',
            'rejections': {}, 'entry': None})

    def _reject(self, key, ticker, side, reason):
        self._round(key)['rejections'][ticker + ':' + side] = reason

    def _candidates(self, snapshot, now):
        market = snapshot['market']
        try:
            key = round_key(market['close_time'])
        except (ValueError, KeyError, TypeError):
            self._emit('INVALID_MARKET_TIME', ticker=market.get('ticker'))
            return []
        coverage = self._round(key)
        coverage['scan_count'] += 1
        coverage['last_scan_at'] = iso(now)
        coverage.setdefault('first_scan_at', iso(now))
        window = in_entry_window(market['close_time'], now)
        coverage['final_window_scanned'] |= window
        result = []
        for side in ('YES', 'NO'):
            reason = None
            if market.get('status') not in ('open', 'active') or seconds_to_close(market['close_time'], now) <= 0:
                reason = 'closed_or_unavailable'
            elif not window:
                reason = 'monitor_only_before_final_minute'
            elif self.paper and not can_enter(self.state, key):
                reason = 'round_already_traded_or_state_blocked'
            elif not observation_fresh(snapshot.get('observation_time'), now):
                reason = 'stale_or_missing_reference'
            elif not snapshot.get('compatible'):
                reason = 'incompatible:' + str(snapshot.get('compatibility_reason'))
            else:
                quote = snapshot.get('quotes', {}).get(side)
                ask = snapshot.get('asks', {}).get(side)
                if ask is None or quote is None:
                    reason = snapshot.get('rejections', {}).get(side, 'quote_unavailable')
                else:
                    try:
                        candidate = Candidate(market['ticker'], side, ask, snapshot['current'],
                                              as_decimal(market['floor_strike']), quote.quote, True)
                        reason = rejection_reason(candidate)
                        if reason is None and not snapshot.get('quote_ready', False):
                            reason = snapshot.get('quote_blocker') or 'quote_evidence_unverified'
                        if reason is None:
                            result.append((candidate, snapshot, quote))
                    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                        reason = 'invalid_candidate:' + type(exc).__name__
            self._reject(key, market['ticker'], side, reason or 'eligible')
        coverage['reason'] = 'candidates_eligible' if result else 'no_eligible_candidate'
        if not window and seconds_to_close(market['close_time'], now) > 0:
            coverage['reason'] = 'monitor_only'
        return result

    def _settle(self):
        pending = self.state['pending']
        try:
            market = self.client.official_market(pending['ticker'])
        except Exception as exc:
            self._emit('SETTLEMENT_PENDING', ticker=pending['ticker'], reason=type(exc).__name__)
            return
        if market.get('ticker') != pending['ticker']:
            self._emit('SETTLEMENT_PENDING', ticker=pending['ticker'], reason='market_identity_mismatch')
            return
        status, result = market.get('status'), str(market.get('result') or '').lower()
        updated = copy.deepcopy(self.state)
        if result in ('yes', 'no') and status in ('settled', 'finalized'):
            won = record_settlement(updated, result, settled_at=iso(self.clock()))
            self._commit(updated, 'PAPER_SETTLEMENT', ticker=pending['ticker'], result=result,
                         won=won, pnl=updated['history'][-1]['pnl'])
        elif status in ('cancelled', 'canceled', 'voided') and result in ('', 'void', 'cancelled', 'canceled'):
            record_void(updated, verified=True, settled_at=iso(self.clock()))
            self._commit(updated, 'PAPER_VOID_REFUND', ticker=pending['ticker'], result='VOID', pnl='0.00')
        else:
            self._emit('SETTLEMENT_PENDING', ticker=pending['ticker'], status=status, result=result)

    def tick(self):
        if self.paper and self.state['mode'] == 'STOPPED':
            return 'STOPPED'
        now = self.clock()
        self.first_scan = now if self.first_scan is None else self.first_scan
        self.last_scan = now
        if self.paper and self.state['pending'] is not None:
            waiting = self._round(str(int(now // 900) + 1))
            waiting['reason'] = ('entry_recorded_waiting_for_settlement' if waiting['entry']
                                 else 'waiting_for_settlement')
            self._settle()
            return self.state['mode']
        try:
            markets = self.client.discover(now)
            in_window = any(in_entry_window(m['close_time'], now) for m in markets)
            snapshots = self.client.batch_snapshots(markets, now, include_quotes=True, final_window=in_window)
        except Exception as exc:
            key = str(int(now // 900) + 1)
            self._round(key)['reason'] = 'scan_failed:' + type(exc).__name__
            self._emit('SCAN_FAILED', reason=type(exc).__name__)
            return self.state['mode']
        completed = self.clock()
        self.last_scan = completed
        key_now = str(int(completed // 900) + 1)
        seen_series = {series_of(m) for m in markets}
        for series in UNIVERSE.keys() - seen_series:
            reason = getattr(self.client, 'discovery_errors', {}).get(series, 'no_markets')
            self._reject(key_now, series, '*', reason)
        if not markets:
            row = self._round(key_now)
            row['reason'] = 'no_markets'
            row['final_window_scanned'] |= in_entry_window(int(key_now) * 900, completed)
        choices = []
        for snapshot in snapshots:
            choices.extend(self._candidates(snapshot, completed))
        lookup = {(c.ticker, c.side): (c, snap, quote) for c, snap, quote in choices}
        ordered = rank([c for c, _, _ in choices])
        if ordered and not self.paper:
            self._emit('READ_ONLY_CANDIDATE', ticker=ordered[0].ticker, side=ordered[0].side)
        if self.paper:
            for candidate in ordered:
                _, snapshot, _ = lookup[(candidate.ticker, candidate.side)]
                market = snapshot['market']
                key = round_key(market['close_time'])
                if not in_entry_window(market['close_time'], self.clock()):
                    self._reject(key, candidate.ticker, candidate.side, 'expired_during_processing')
                    continue
                try:
                    fresh = self.client.snapshot(market, self.clock(), include_quotes=True)
                    if fresh['market']['ticker'] != candidate.ticker:
                        self._reject(key, candidate.ticker, candidate.side, 'market_identity_mismatch')
                        continue
                    refreshed = self._candidates(fresh, self.clock())
                    found = next((item for item in refreshed if item[0].side == candidate.side), None)
                    if found is None:
                        continue
                    chosen, snapshot, detail = found
                    entry_now = self.clock()
                    key = round_key(snapshot['market']['close_time'])
                    if (not in_entry_window(snapshot['market']['close_time'], entry_now)
                            or not observation_fresh(snapshot['observation_time'], entry_now)
                            or not can_enter(self.state, key)):
                        self._reject(key, chosen.ticker, chosen.side, 'expired_or_stale_at_entry')
                        continue
                    if detail.quote.debit > as_decimal(self.state['balance']):
                        self._reject(key, chosen.ticker, chosen.side, 'insufficient_paper_cash')
                        continue
                    trade = {**detail.ledger_fields(), 'ticker': chosen.ticker, 'side': chosen.side,
                        'series_ticker': series_of(snapshot['market']), 'round_key': key,
                        'close_time': snapshot['market']['close_time'], 'index_time': snapshot['observation_time'],
                        'entered_at': iso(entry_now), 'seconds_to_close': seconds_to_close(snapshot['market']['close_time'], entry_now),
                        'ask': str(chosen.ask), 'probability': str(chosen.ask),
                        'probability_field': (chosen.side.lower() + '_ask_dollars'
                            if chosen.side.lower() + '_ask_dollars' in snapshot['market']
                            else chosen.side.lower() + '_ask'),
                        'current': str(chosen.current), 'target': str(chosen.target),
                        'distance_pct': str(distance_pct(chosen)), 'source_sha': self.source,
                        'quote_policy_verified': bool(snapshot['quote_ready'])}
                except Exception as exc:
                    self._reject(key, candidate.ticker, candidate.side, 'revalidation_failed:' + type(exc).__name__)
                    continue
                updated = copy.deepcopy(self.state)
                record_entry(updated, key, trade)
                self._commit(updated, 'PAPER_ENTRY', ticker=chosen.ticker, side=chosen.side,
                    round_key=key, debit=trade['debit'], payout=trade['payout'], seconds_to_close=trade['seconds_to_close'])
                row = self._round(key)
                row.update(reason='entry_recorded', entry={'ticker': chosen.ticker, 'side': chosen.side})
                break
        self._summaries()
        return self.state['mode']

    def _summaries(self):
        for key, row in sorted(self.rounds.items()):
            fingerprint = (json.dumps(row['rejections'], sort_keys=True) + row['reason']
                           + str(row['final_window_scanned']))
            if self._last_messages.get(key) != fingerprint:
                self._emit('ROUND_SUMMARY', **row)
                self._last_messages[key] = fingerprint

    def finish(self, *, persist_audit=True, failure=None):
        self._summaries()
        coverage = {'source_sha': self.source, 'run_id': os.environ.get('GITHUB_RUN_ID'),
            'code_fingerprint': os.environ.get('PAPER_CODE_FINGERPRINT'),
            'scan_started_at': iso(self.first_scan) if self.first_scan is not None else None,
            'scan_ended_at': iso(self.last_scan) if self.last_scan is not None else None,
            'rounds': list(self.rounds.values()),
            'readiness_blockers': getattr(self.client, 'readiness_reasons', []),
            'paper': self.paper, 'mode': self.state['mode'], 'failure': failure}
        if self.report_dir:
            (self.report_dir / 'coverage.json').write_text(json.dumps(coverage, indent=2, sort_keys=True) + '\n')
        if self.paper and self.rounds and persist_audit:
            updated = copy.deepcopy(self.state)
            updated.setdefault('round_reports', []).append(coverage)
            self._commit(updated, 'ROUND_AUDIT_SAVED', round_count=len(self.rounds))
        return coverage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=600)
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--paper', action='store_true')
    parser.add_argument('--persist-to-github', action='store_true')
    parser.add_argument('--state', default='paper_state.json')
    parser.add_argument('--report-dir', default='runtime_reports')
    parser.add_argument('--quote-policy', default='quote_policy.json')
    parser.add_argument('--new-session', action='store_true')
    parser.add_argument('--confirm-reset', action='store_true')
    args = parser.parse_args(argv)
    if not 0 < args.duration <= 600 or not 0 < args.interval <= 60:
        parser.error('duration must be (0,600] seconds and interval (0,60] seconds')
    if args.persist_to_github and (not args.paper or os.environ.get('GITHUB_ACTIONS') != 'true'):
        parser.error('GitHub persistence requires --paper and the existing Actions environment')
    if args.confirm_reset != args.new_session or (args.new_session and not args.paper):
        parser.error('a reset requires --paper --new-session --confirm-reset together')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    sha = source_sha()
    state_path = Path(args.state)
    archives = []
    if args.new_session:
        _, archived = archive_and_reset(state_path, confirm=True)
        archives.append(archived)
    state = load(state_path)
    if args.paper and state['mode'] == 'STOPPED':
        LOG.info('STOPPED: persisted loss stop; explicit archive/reset required for a new session')
        return 0
    def save(updated):
        return persist(updated, state_path, push=args.persist_to_github, source_sha=sha, extra_paths=archives)
    if args.new_session:
        save(state)
    from live_client import LiveKalshiClient
    client = LiveKalshiClient(args.quote_policy)
    engine = PaperEngine(client, state, persist_state=save, paper=args.paper, source=sha, report_dir=args.report_dir)
    engine._emit('SCAN_START', duration=args.duration, interval=args.interval, paper=args.paper,
                 readiness_blockers=client.readiness_reasons, initial_mode=state['mode'], balance=state['balance'])
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            cycle = time.monotonic()
            if engine.tick() == 'STOPPED' and args.paper:
                break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, max(0.0, args.interval - (time.monotonic() - cycle))))
        engine.finish()
    except Exception as exc:
        try:
            engine.finish(persist_audit=False, failure=type(exc).__name__)
        except Exception:
            LOG.error('Could not write failure coverage report')
        raise
    finally:
        client.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
