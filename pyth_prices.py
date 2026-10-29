"""Authenticated Pyth Core reference-price adapter (paper simulation only).

Feed IDs must be explicitly verified against Kalshi's settlement source.
Never substitute an unrelated exchange spot quote.
"""
import math
import os
import time
import requests

HERMES = "https://pyth.dourolabs.app/hermes/v2/updates/price/latest"


class PythPrices:
    def __init__(self, feed_ids=None, api_key=None, session=None):
        self.feed_ids = feed_ids or {}
        self.api_key = api_key if api_key is not None else os.getenv("PYTH_API_KEY", "")
        self.session = session or requests.Session()

    def price(self, series):
        if not self.api_key:
            raise RuntimeError("PYTH_API_KEY not configured")
        feed_id = self.feed_ids.get(series)
        if not isinstance(feed_id, str) or len(feed_id.removeprefix("0x")) != 64:
            raise ValueError("No verified Pyth feed ID for " + series)
        if any(c not in "0123456789abcdefABCDEF" for c in feed_id.removeprefix("0x")):
            raise ValueError("Invalid Pyth feed ID for " + series)
        response = self.session.get(
            HERMES, params={"ids[]": feed_id},
            headers={"Authorization": "Bearer " + self.api_key},
            timeout=12)
        response.raise_for_status()
        parsed = response.json().get("parsed") or []
        wanted = feed_id.removeprefix("0x").lower()
        matches = [p for p in parsed if str(p.get("id", "")).removeprefix("0x").lower() == wanted]
        if len(matches) != 1:
            raise ValueError("Pyth response missing requested feed")
        value = matches[0]["price"]
        px = int(value["price"]) * 10 ** int(value["expo"])
        ts = float(value["publish_time"])
        if not math.isfinite(px) or px <= 0 or not math.isfinite(ts):
            raise ValueError("Invalid Pyth price")
        if not -5 <= time.time() - ts <= 15:
            raise ValueError("Stale Pyth observation")
        return px, ts
