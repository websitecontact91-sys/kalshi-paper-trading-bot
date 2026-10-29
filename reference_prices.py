"""Decimal reference observations from Kalshi-hosted feeds only."""
import math
import time
from kalshi_client import Kalshi, decimal_value
from kalshi_pyth import KalshiPythPrices
from timing import observation_fresh
from universe import CF_INDEX_IDS, PYTH_INDEX_IDS


class KalshiCFPrices:
    def __init__(self, client=None):
        self.client, self.cache = client or Kalshi(), {}

    def price(self, series):
        index_id = CF_INDEX_IDS[series]
        cached = self.cache.get(index_id)
        if cached and observation_fresh(cached[1], time.time(), max_age=8):
            return cached
        if not (self.client._key and self.client._key_id):
            raise ValueError('authenticated Kalshi reference-feed credentials missing')
        data = self.client.get('/cfbenchmarks/values', {'id': index_id})
        payload = data.get('data', {}).get('payload')
        if not isinstance(payload, list):
            raise ValueError('invalid CF response for ' + index_id)
        valid = []
        for point in payload:
            try:
                value = decimal_value(point['value'])
                stamp = float(point['time'])
                stamp = stamp / 1000 if stamp > 1e11 else stamp
                if value > 0 and math.isfinite(stamp):
                    valid.append((stamp, value))
            except (KeyError, ValueError, TypeError, ArithmeticError):
                continue
        if not valid:
            raise ValueError('no CF observations for ' + index_id)
        stamp, value = max(valid)
        if not observation_fresh(stamp, time.time()):
            raise ValueError('stale CF observation for ' + index_id)
        self.cache[index_id] = (value, stamp)
        return value, stamp


class ReferencePrices:
    def __init__(self, client=None):
        self.cf = KalshiCFPrices(client)
        self.pyth = KalshiPythPrices()

    def price(self, series):
        if series in CF_INDEX_IDS:
            return self.cf.price(series)
        if series in PYTH_INDEX_IDS:
            return self.pyth.price(series)
        raise ValueError('unmapped series')

    def close(self):
        self.pyth.close()
