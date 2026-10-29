"""Read-only live adapter injected into the same engine used by simulations."""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path

from kalshi_client import Kalshi, ask_and_depth, implied_ask
from quote_adapter import QuoteUnavailable, build_modeled_quote, build_quote
from reference_prices import ReferencePrices
from timing import observation_fresh, timestamp
from universe import UNIVERSE, check_compatibility


def quote_policy(path):
    """An unverified flag or a synthetic arithmetic fixture cannot unlock entries."""
    result = {'balance_precision': None, 'verified': False, 'reason': 'quote_evidence_unverified'}
    try:
        location = Path(path)
        config = json.loads(location.read_text())
        if not isinstance(config, dict):
            raise ValueError('quote policy must be an object')
        precision = config.get('balance_precision')
        if precision not in ('0.01', '0.0001'):
            result['reason'] = 'account_balance_precision_unverified'
            return result
        result['balance_precision'] = precision
        if not config.get('evidence_file'):
            return result
        evidence = json.loads((location.parent / config['evidence_file']).read_text())
        actual = build_quote(evidence)
        if not actual.ui_parity_verified:
            return result
        # This input record is an audited companion to the preserved real response.
        inputs = evidence['model_validation_inputs']
        if not isinstance(inputs, dict) or not isinstance(inputs.get('market'), dict):
            raise ValueError('model validation inputs must identify a market')
        if (not str(inputs['market'].get('ticker', '')).startswith('KXSOL15M-')
                or inputs.get('side') not in ('YES', 'NO')):
            result['reason'] = 'required_sol_market_and_side_unverified'
            return result
        response = json.loads(evidence['provenance']['raw_response'], parse_float=str)
        identity = {}
        for name in ('market_ticker', 'side'):
            value = response
            path = evidence['provenance']['field_paths'][name]
            if not isinstance(path, list) or not path:
                raise ValueError('missing captured quote identity path')
            for component in path:
                value = value[component]
            identity[name] = value
        if (identity['market_ticker'] != inputs['market']['ticker']
                or str(identity['side']).upper() != inputs['side']):
            result['reason'] = 'captured_quote_market_or_side_mismatch'
            return result
        modeled = build_modeled_quote(inputs['ask'], inputs['available_quantity'],
            inputs['series'], inputs['event'], inputs['market'], balance_precision=precision,
            now=timestamp(inputs['observed_at']))
        if (actual.quote != modeled.quote or actual.quantity != modeled.quantity
                or actual.fees != modeled.fees or actual.principal != modeled.principal):
            result['reason'] = 'captured_quote_fee_model_mismatch'
            return result
        if actual.quote.debit != Decimal('5.00') or actual.quote.payout != Decimal('5.69'):
            result['reason'] = 'required_sol_quote_parity_unverified'
            return result
        result.update(verified=True, reason=None, evidence=actual.evidence)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError) as exc:
        result['reason'] = 'invalid_quote_evidence:' + type(exc).__name__
    return result


class LiveKalshiClient:
    def __init__(self, policy_path='quote_policy.json', *, api=None, references=None, clock=time.time):
        self.api, self.clock = api or Kalshi(), clock
        self.references = references or ReferencePrices(self.api)
        self.policy = quote_policy(policy_path)
        self.discovery_errors = {}
        self._markets, self._discovered = [], float('-inf')
        self._series, self._events = {}, {}
        self._shortlist, self._statuses = set(), {}

    @property
    def readiness_reasons(self):
        return [] if self.policy['verified'] else [self.policy['reason']]

    def series_metadata(self, series, force=False):
        cached = self._series.get(series)
        if not cached or force or self.clock() - cached[0] >= 300:
            cached = self._series[series] = (self.clock(), self.api.series(series))
        return cached[1]

    def event_metadata(self, event, force=False):
        cached = self._events.get(event)
        if not cached or force or self.clock() - cached[0] >= 60:
            cached = self._events[event] = (self.clock(), self.api.event(event))
        return cached[1]

    def discover(self, now=None):
        now = self.clock() if now is None else now
        if now - self._discovered < 60:
            return list(self._markets)
        markets, errors = [], {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = {pool.submit(self.api.open_markets, series, now): series for series in UNIVERSE}
            for task in as_completed(tasks):
                series = tasks[task]
                try:
                    rows = task.result()
                    # Only the currently open nearest closing market for each mapped series.
                    rows = [m for m in rows if timestamp(m.get('open_time') or 0) <= now]
                    if rows:
                        markets.append(min(rows, key=lambda m: timestamp(m['close_time'])))
                except Exception as exc:
                    errors[series] = 'discovery_failed:' + type(exc).__name__
        self._markets = sorted(markets, key=lambda m: m['ticker'])
        self._discovered, self.discovery_errors = self.clock(), errors
        return list(self._markets)

    def _snapshot(self, market, *, book=None, include_quotes=True, force_metadata=False):
        result = {'market': market, 'current': None, 'observation_time': None,
                  'compatible': False, 'compatibility_reason': 'not_evaluated',
                  'quotes': {}, 'asks': {}, 'rejections': {},
                  'quote_ready': self.policy['verified'], 'quote_blocker': self.policy['reason']}
        series = market.get('series_ticker') or market['ticker'].split('-')[0]
        try:
            current, stamp = self.references.price(series)
            result.update(current=current, observation_time=stamp)
            metadata = self.series_metadata(series, force_metadata)
            compatible, reason = check_compatibility(market, current, metadata)
            result.update(compatible=compatible, compatibility_reason=reason)
        except Exception as exc:
            result['compatibility_reason'] = 'reference_or_metadata_unavailable:' + type(exc).__name__
        for side in ('YES', 'NO'):
            try:
                result['asks'][side] = implied_ask(market, side)
            except (ValueError, TypeError, ArithmeticError):
                result['rejections'][side] = 'ask_unavailable'
        if not include_quotes:
            return result
        if not result['compatible']:
            for side in ('YES', 'NO'):
                result['rejections'].setdefault(side, result['compatibility_reason'])
            return result
        if not observation_fresh(result['observation_time'], self.clock()):
            result['rejections'] = {s: 'stale' for s in ('YES', 'NO')}
            return result
        if self.policy['balance_precision'] is None:
            result['rejections'] = {s: self.policy['reason'] for s in ('YES', 'NO')}
            return result
        try:
            if book is None:
                book = self.api.orderbook(market['ticker'])
            event = self.event_metadata(market['event_ticker'], force_metadata)
        except Exception as exc:
            result['rejections'] = {s: 'quote_data_unavailable:' + type(exc).__name__ for s in ('YES', 'NO')}
            return result
        for side, ask in result['asks'].items():
            try:
                price, quantity = ask_and_depth(book, side, ask)
                result['quotes'][side] = build_modeled_quote(price, quantity, metadata, event, market,
                    balance_precision=self.policy['balance_precision'], now=self.clock())
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                result['rejections'][side] = ('quote_unavailable:' + str(exc))
        return result

    def batch_snapshots(self, markets, now=None, include_quotes=True, final_window=False):
        """Batch status/asks; refresh precomputed shortlist and changed-status markets."""
        tickers = [m['ticker'] for m in markets]
        updated = self.api.refresh_markets(tickers)
        refreshed = []
        for old in markets:
            m = updated.get(old['ticker'])
            if m:
                refreshed.append(dict(m, series_ticker=old.get('series_ticker', old['ticker'].split('-')[0])))
        changed = {m['ticker'] for m in refreshed if self._statuses.get(m['ticker']) != m.get('status')}
        # A previously stale reference can recover without a market-status change.
        # Include all active mapped markets in the batched refresh (at most 30),
        # so a shortlist never becomes a hidden distance or freshness gate.
        active = {m['ticker'] for m in refreshed if m.get('status') in ('open', 'active')}
        targets = (self._shortlist | changed | active) if final_window else set(tickers)
        # Initial window scan has no prior shortlist; all current markets are evaluated.
        if not self._statuses:
            targets = set(tickers)
        quotes_for = [m['ticker'] for m in refreshed if m['ticker'] in targets]
        books, book_error = {}, None
        if include_quotes and self.policy['balance_precision'] is not None:
            try:
                books = self.api.orderbooks(quotes_for)
            except Exception as exc:
                book_error = 'batch_orderbook_unavailable:' + type(exc).__name__
        results = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = {}
            for m in refreshed:
                should_quote = include_quotes and m['ticker'] in targets
                # Empty books intentionally prevent fallback to 30 sequential network reads.
                task = pool.submit(self._snapshot, m, book=books.get(m['ticker'], {'orderbook_fp': {}}),
                                   include_quotes=should_quote)
                tasks[task] = m
            for task in as_completed(tasks):
                result = task.result()
                if book_error:
                    result['rejections'] = {s: book_error for s in ('YES', 'NO')}
                elif self.policy['balance_precision'] is None:
                    result['rejections'] = {s: self.policy['reason'] for s in ('YES', 'NO')}
                if result['compatible'] and result['market'].get('status') in ('open', 'active'):
                    self._shortlist.add(result['market']['ticker'])
                results.append(result)
        for missing in set(tickers) - set(updated):
            old = next(m for m in markets if m['ticker'] == missing)
            results.append({'market': dict(old, status='unavailable'), 'current': None,
                'observation_time': None, 'compatible': False,
                'compatibility_reason': 'market_missing_from_refresh', 'quotes': {}, 'asks': {},
                'rejections': {s: 'market_missing_from_refresh' for s in ('YES', 'NO')},
                'quote_ready': False, 'quote_blocker': 'market_missing_from_refresh'})
        self._statuses = {m['ticker']: m.get('status') for m in refreshed}
        return sorted(results, key=lambda s: s['market']['ticker'])

    def snapshot(self, market, now=None, include_quotes=True):
        current = self.api.market(market['ticker'])
        current['series_ticker'] = market.get('series_ticker', market['ticker'].split('-')[0])
        return self._snapshot(current, include_quotes=include_quotes, force_metadata=True)

    def official_market(self, ticker):
        return self.api.market(ticker)

    def close(self):
        self.references.close()
