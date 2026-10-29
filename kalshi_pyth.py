"""Kalshi-hosted Pyth streaming reference values. Read-only, fail closed."""
import base64
import json
import logging
import math
import os
import threading
import time

import websocket
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

LOG = logging.getLogger(__name__)
PYTH_INDEX_IDS = {
    "KXGOLD15M": "Metal.Index.1OZGOLD/USD",
    "KXSILVER15M": "Metal.Index.SILVER/USD",
    "KXBRENT15M": "Commodities.Index.BRENT/USD",
    "KXCOPPER15M": "Commodities.Index.CU/USD",
    "KXNATGAS15M": "Commodities.Index.NATGAS/USD",
    "KXPALLADIUM15M": "Metal.XPD/USD",
    "KXPLATINUM15M": "Metal.XPT/USD",
    "KXWTI15M": "Commodities.Index.WTI1M/USD",
    "KXEURUSD15M": "FX.EUR/USD",
    "KXGBPUSD15M": "FX.GBP/USD",
    "KXAUDUSD15M": "FX.AUD/USD",
    "KXUSDCAD15M": "FX.USD/CAD",
    "KXUSDJPY15M": "FX.USD/JPY",
    "KXUSDMXN15M": "FX.USD/MXN",
    "KX2YRRATE15M": "InterestRate.US2Y",
    "KX5YRRATE15M": "InterestRate.US5Y",
    "KX10YRRATE15M": "InterestRate.US10Y",
    "KX30YRRATE15M": "InterestRate.US30Y",
    "KXDJIA15M": "Equity.Index.US30/USD",
}
# Mapping is only a candidate; contract denomination and settlement must also match.
# No direct Pyth credentials or network connection is used.

class KalshiPythPrices:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @staticmethod
    def _headers():
        key_id = os.environ["KALSHI_API_KEY_ID"]
        key = serialization.load_pem_private_key(os.environ["KALSHI_PRIVATE_KEY"].encode(), password=None)
        stamp = str(int(time.time() * 1000))
        message = (stamp + "GET/trade-api/ws/v2").encode()
        if isinstance(key, ed25519.Ed25519PrivateKey):
            sig = key.sign(message)
        else:
            sig = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        return [f"KALSHI-ACCESS-KEY: {key_id}",
                f"KALSHI-ACCESS-TIMESTAMP: {stamp}",
                f"KALSHI-ACCESS-SIGNATURE: {base64.b64encode(sig).decode()}"]

    def _run(self):
        while not self.stop_event.is_set():
            ws = None
            try:
                ws = websocket.create_connection("wss://external-api-ws.kalshi.com/trade-api/ws/v2",
                                                 header=self._headers(), timeout=8)
                ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                    "channels": ["pyth_value"], "underlying_tickers": sorted(set(PYTH_INDEX_IDS.values()))}}))
                while not self.stop_event.is_set():
                    try:
                        event = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                    if event.get("type") == "error":
                        raise RuntimeError(str(event.get("msg")))
                    if event.get("type") != "pyth_value":
                        continue
                    msg = event.get("msg") or {}
                    key = msg.get("underlying_ticker")
                    if key not in PYTH_INDEX_IDS.values():
                        continue
                    try:
                        value = float(msg["value_usd"])
                        timestamp = float(msg["source_ts_ms"]) / 1000
                    except (KeyError, ValueError, TypeError):
                        continue
                    if math.isfinite(value) and value > 0 and math.isfinite(timestamp):
                        with self.lock:
                            if timestamp > self.values.get(key, (0, 0))[1]:
                                self.values[key] = (value, timestamp)
            except Exception as exc:
                LOG.warning("Kalshi Pyth stream disconnected: %s", exc)
                self.stop_event.wait(3)
            finally:
                if ws:
                    ws.close()

    def price(self, series):
        key = PYTH_INDEX_IDS[series]
        with self.lock:
            point = self.values.get(key)
        if point is None:
            raise ValueError("No Kalshi Pyth observation for " + key)
        value, ts = point
        if not -5 <= time.time() - ts <= 15:
            raise ValueError("Stale Kalshi Pyth observation for " + key)
        return value, ts

    def close(self):
        self.stop_event.set()
