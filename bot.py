"""Kalshi 15-minute PAPER bot. No authenticated trading/order placement.

This is intentionally fail-closed: an exact ticker/strike/rule mapping and
verified underlying-to-settlement-reference source are required for entries.
Public Coinbase spot is not automatically Kalshi's official settlement index.
"""
import argparse
import datetime as dt
import json
import logging
import math
import os
import sqlite3
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from statistics import pstdev
from typing import Optional
from urllib.parse import quote

import requests
import yaml

LOG = logging.getLogger("kalshi_paper")


def now():
    return time.time()


def parse_time(value):
    if isinstance(value, (float, int)):
        return float(value)
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def init_db(path, initial_balance):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, side TEXT NOT NULL,
        entry_time REAL NOT NULL, close_time REAL NOT NULL, stake REAL NOT NULL,
        contracts INTEGER NOT NULL, ask REAL NOT NULL, fees REAL NOT NULL,
        estimated_probability REAL NOT NULL, z_buffer REAL NOT NULL,
        status TEXT NOT NULL, result TEXT, pnl REAL, settled_time REAL);
      CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, ticker TEXT,
        side TEXT, reason TEXT NOT NULL, detail TEXT);
    """)
    if db.execute("SELECT 1 FROM state WHERE key='balance'").fetchone() is None:
        db.execute("INSERT INTO state VALUES ('balance',?)", (str(initial_balance),))
        db.execute("INSERT INTO state VALUES ('mode','SCANNING')")
    db.commit()
    return db


def state(db, key):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_state(db, key, value):
    db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def observe(db, ticker, side, reason, detail=""):
    db.execute("INSERT INTO observations(time,ticker,side,reason,detail) VALUES (?,?,?,?,?)",
               (now(), ticker, side, reason, str(detail)[:1000]))
    db.commit()


class Kalshi:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "paper-research-bot/1.0"})

    def get(self, path, params=None):
        for attempt in range(4):
            try:
                r = self.session.get(self.base + path, params=params, timeout=12)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"retryable HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(min(2 ** attempt, 8))

    def list_open(self, series, limit):
        result = []
        cursor = None
        while len(result) < limit:
            params = {"status": "open", "series_ticker": series, "limit": min(200, limit-len(result))}
            if cursor:
                params["cursor"] = cursor
            payload = self.get("/markets", params)
            result.extend(payload.get("markets", []))
            cursor = payload.get("cursor")
            if not cursor:
                break
        return result

    def market(self, ticker):
        return self.get("/markets/" + quote(ticker, safe="")).get("market", {})

    def orderbook(self, ticker):
        return self.get("/markets/" + quote(ticker, safe="") + "/orderbook")


class CoinbaseSpot:
    """Public spot quotes, NOT necessarily official settlement reference."""
    def __init__(self):
        self.session = requests.Session()

    def price(self, product_id):
        r = self.session.get("https://api.exchange.coinbase.com/products/" + quote(product_id, safe="") + "/ticker", timeout=10)
        r.raise_for_status()
        payload = r.json()
        ts = parse_time(payload.get("time"))
        if ts is None:
            raise ValueError("missing quote timestamp")
        return float(payload["price"]), ts


class Volatility:
    def __init__(self, lookback):
        self.lookback = lookback
        self.samples = {}

    def add(self, product, price, ts):
        series = self.samples.setdefault(product, deque())
        if not series or ts > series[-1][0]:
            series.append((ts, price))
        while series and series[0][0] < ts-self.lookback:
            series.popleft()

    def sigma_remaining(self, product, seconds, min_samples):
        series = list(self.samples.get(product, []))
        if len(series) < min_samples:
            return None
        # Empirical log-return volatility, scaled by observed sampling interval.
        increments = []
        for (t1,p1),(t2,p2) in zip(series,series[1:]):
            if p1 > 0 and p2 > 0 and t2 > t1:
                increments.append((math.log(p2/p1), t2-t1))
        if len(increments) < min_samples-1:
            return None
        mean_dt = sum(delta for _,delta in increments)/len(increments)
        if mean_dt <= 0:
            return None
        sd = pstdev(x for x,_ in increments)
        return sd*math.sqrt(seconds/mean_dt)


def implied_ask(market, side):
    # Kalshi's *_ask_dollars fields are preferred over legacy cent fields.
    key = side.lower()+"_ask_dollars"
    if market.get(key) is not None:
        return float(market[key])
    old = market.get(side.lower()+"_ask")
    return float(old)/100 if old is not None else None


def best_bid_depth(book, side, ask):
    """Conservative liquidity check using displayed opposing bids.

    Kalshi orderbooks contain bids for YES and NO; a YES ask is inferred from
    the opposing NO bid at 1-price. Supports fp dollars and legacy cents.
    """
    obj = book.get("orderbook_fp") or book.get("orderbook") or {}
    opposite = "no" if side == "YES" else "yes"
    levels = obj.get(opposite+"_dollars") or obj.get(opposite) or []
    depth = 0
    for level in levels:
        if isinstance(level, (list,tuple)) and len(level)>=2:
            raw_price, qty = level[0], level[1]
        else:
            continue
        price = float(raw_price)
        if price > 1:
            price /= 100
        implied = 1-price
        if implied <= ask+1e-6:
            depth += int(float(qty))
    return depth


def estimate_probability(price, target, yes_when, sigma_log):
    if sigma_log is None or sigma_log <= 0 or price <= 0 or target <= 0:
        return None
    z = math.log(price/target)/sigma_log
    # Normal approximation is a heuristic, NOT a calibrated probability.
    p_above = 0.5*(1+math.erf(z/math.sqrt(2)))
    return (p_above, z) if yes_when in ("above", "at_or_above") else None


def choose_candidate(candidates):
    """Select exactly ONE qualified market by signed threshold distance percent.

    Distance is used ONLY to rank eligible opportunities, never as an entry
    requirement. Larger positive distance_pct wins. Payout and ticker break
    exact ties deterministically. No basket/splitting logic exists.
    """
    if not candidates:
        return None
    return max(candidates, key=lambda c: (
        c["distance_pct"], c["payout"], c["ticker"], c["side"]
    ))


def affordable_single(candidate, budget):
    """Whole contracts only; never exceed the single-round budget or depth."""
    if not candidate or budget <= 0:
        return None
    unit = candidate["ask"] + candidate["fees_per_contract"]
    if unit <= 0:
        return None
    qty = min(math.floor((budget + 1e-9) / unit), int(candidate["depth"]))
    if qty < 1:
        return None
    result = dict(candidate)
    result.update(contracts=qty, stake=qty*unit,
                  fees=qty*candidate["fees_per_contract"],
                  payout=1/unit,
                  roi=candidate["probability"]/unit-1)
    return result


def evaluate(market, cfg, spot, vol, book, t):
    ticker = market.get("ticker", "")
    mapping = cfg.get("markets", {}).get(ticker)
    if not mapping:
        return None, "unmapped ticker"
    if mapping.get("settlement_source_verified") is not True:
        return None, "settlement reference not verified"
    if mapping.get("interval_seconds") != 900:
        return None, "15-minute interval not verified"
    close = parse_time(market.get("close_time"))
    if close is None:
        return None, "missing close time"
    remaining = close-t
    if not cfg.get("min_seconds_left", 5) <= remaining <= cfg.get("max_seconds_left", 60):
        return None, "outside entry window"
    if market.get("status") != "open":
        return None, "not open"
    price, ts = spot
    if t-ts > cfg.get("max_underlying_age_seconds",15) or ts-t > 3:
        return None, "stale underlying quote"
    sigma = vol.sigma_remaining(mapping["product_id"], remaining, cfg.get("min_price_samples",60))
    prob = estimate_probability(price, float(mapping["target_price"]), mapping.get("yes_when"), sigma)
    if prob is None:
        return None, "insufficient volatility data"
    p_yes, z = prob
    candidates = []
    for side, p, direction_z in (("YES",p_yes,z),("NO",1-p_yes,-z)):
        ask = implied_ask(market, side)
        if ask is None or not 0 < ask < 1:
            continue
        if not cfg["min_probability"] <= p <= cfg["max_probability"]:
            continue
        if not cfg.get("min_contract_price", 0.80) <= ask <= cfg.get("max_contract_price", 0.90):
            continue
        # Integer contracts; leave residual cash unspent, never exceed stake.
        fees_per_contract = float(mapping.get("fee_per_contract_usd",cfg.get("fee_per_contract_usd",0)))
        contracts = math.floor(cfg["stake_usd"]/(ask+fees_per_contract))
        if contracts < 1:
            continue
        actual_cost = contracts*(ask+fees_per_contract)
        payout = contracts/actual_cost
        if not cfg["min_payout"] <= payout <= cfg["max_payout"]:
            continue
        depth = best_bid_depth(book, side, ask)
        if depth < 1:
            continue
        # A single trade will be capped to available depth.
        expected_roi = (p*contracts-actual_cost)/actual_cost
        if expected_roi < cfg["min_expected_roi"]:
            continue
        target = float(mapping["target_price"])
        # Positive only when the current underlying is on the chosen winning side.
        signed_distance = (price-target) if side == "YES" else (target-price)
        candidates.append(dict(ticker=ticker,side=side,ask=ask,contracts=contracts,
                               stake=actual_cost,fees=fees_per_contract*contracts,
                               probability=p,z=direction_z,roi=expected_roi,payout=payout,
                               target_price=target,underlying_price=price,
                               signed_distance=signed_distance,
                               distance_pct=100*signed_distance/target,
                               close_time=close, depth=depth,
                               fees_per_contract=fees_per_contract))
    if not candidates:
        return None, "no side passes price/probability/liquidity/edge checks"
    return choose_candidate(candidates), "qualified"


def open_paper(db, candidate, budget=5.0):
    """Atomically open at most ONE paper position per round."""
    if state(db, "mode") != "SCANNING" or candidate is None:
        return False
    if db.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]:
        return False
    balance = float(state(db, "balance"))
    if candidate["stake"] > min(balance, budget) + 1e-8:
        return False
    with db:
        db.execute("""INSERT INTO trades(ticker,side,entry_time,close_time,stake,contracts,ask,fees,
                   estimated_probability,z_buffer,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (candidate["ticker"],candidate["side"],now(),candidate["close_time"],
                    candidate["stake"],candidate["contracts"],candidate["ask"],
                    candidate["fees"],candidate["probability"],candidate["z"],"OPEN"))
        set_state(db,"balance",balance-candidate["stake"])
        set_state(db,"mode","WAITING_FOR_SETTLEMENT")
    LOG.warning("PAPER SINGLE %s %s cost=%.2f",candidate["ticker"],candidate["side"],candidate["stake"])
    return True


def settle_pending(db, kalshi):
    """Wait for the single position result; stop if it loses."""
    rows = db.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id").fetchall()
    if len(rows) > 1:
        raise RuntimeError("legacy multi-position basket found; refusing new trades")
    if not rows:
        if state(db,"mode") == "WAITING_FOR_SETTLEMENT":
            raise RuntimeError("state mismatch: no open trade")
        return
    results = {}
    for row in rows:
        result = str(kalshi.market(row["ticker"]).get("result") or "").lower()
        if result not in ("yes", "no"):
            return
        results[row["id"]] = result
    lost = False
    credit = 0.0
    with db:
        for row in rows:
            result = results[row["id"]]
            won = result == row["side"].lower()
            lost |= not won
            payout = row["contracts"] if won else 0.0
            credit += payout
            db.execute("UPDATE trades SET status='SETTLED',result=?,pnl=?,settled_time=? WHERE id=?",
                       (result,payout-row["stake"],now(),row["id"]))
        set_state(db,"balance",float(state(db,"balance"))+credit)
        set_state(db,"mode","STOPPED" if lost else "SCANNING")
    LOG.warning("PAPER BASKET SETTLED count=%d loss=%s balance=%.2f mode=%s",
                len(rows),lost,float(state(db,"balance")),state(db,"mode"))


def validate(cfg):
    required = ("series_tickers","markets","stake_usd","min_probability","max_probability",
                "min_payout","max_payout","min_expected_roi")
    for key in required:
        if key not in cfg:
            raise ValueError("missing config: "+key)
    if not cfg["series_tickers"]:
        raise ValueError("Set explicit verified 15-minute series_tickers before running")
    if not 0 < cfg["min_probability"] <= cfg["max_probability"] < 1:
        raise ValueError("invalid probability bounds")
    if cfg["stake_usd"] <= 0 or cfg["stake_usd"] > 5.0:
        raise ValueError("paper stake must be between $0 and $5")
    if not 0 < cfg.get("min_contract_price", 0.80) <= cfg.get("max_contract_price", 0.90) < 1:
        raise ValueError("invalid contract price bounds")


def scan_once(cfg, kalshi, spot_client, vol, db, clock=now):
    """Always scan all configured open markets, including the first 14 minutes.

    Warm up volatility samples continuously; only place orders in the final
    60 seconds. All candidates are refreshed before any paper entry.
    """
    markets = {}
    for series in cfg["series_tickers"]:
        for m in kalshi.list_open(series,cfg.get("max_markets_per_series",200)):
            ticker = m.get("ticker")
            if ticker in cfg["markets"]:
                markets[ticker] = m
    # Fetch one fresh quote per product each scan, including the early 14 minutes.
    quotes = {}
    for ticker,m in markets.items():
        mapping = cfg["markets"][ticker]
        close = parse_time(m.get("close_time"))
        if close is None or not 0 < close-clock() <= 900:
            continue
        product = mapping["product_id"]
        if product not in quotes:
            try:
                quotes[product] = spot_client.price(product)
                vol.add(product,*quotes[product])
            except (requests.RequestException, ValueError) as exc:
                observe(db,ticker,None,"underlying unavailable",str(exc))
        if close-clock() > cfg.get("max_seconds_left",60):
            # First 14 minutes: continuously track prices and warming history.
            observe(db,ticker,None,"monitoring pre-entry window",
                    "seconds_left=%.1f"%(close-clock()))
    eligible = [(ticker,m) for ticker,m in markets.items()
                if parse_time(m.get("close_time")) is not None
                and cfg.get("min_seconds_left",5) <= parse_time(m["close_time"])-clock() <= cfg.get("max_seconds_left",60)]
    candidates = []
    if not eligible:
        return candidates
    # Concurrent orderbooks reduce latency when many markets enter final minute.
    with ThreadPoolExecutor(max_workers=min(8,len(eligible))) as pool:
        tasks = {pool.submit(kalshi.orderbook,ticker):(ticker,m) for ticker,m in eligible}
        for future in as_completed(tasks):
            ticker,m = tasks[future]
            try:
                book = future.result()
                mapping = cfg["markets"][ticker]
                spot = quotes.get(mapping["product_id"])
                if spot is None:
                    continue
                c,reason = evaluate(m,cfg,spot,vol,book,clock())
                observe(db,ticker,c["side"] if c else None,reason)
                if c:
                    candidates.append(c)
            except (requests.RequestException,ValueError,KeyError) as exc:
                observe(db,ticker,None,"data error",str(exc))
    return candidates


def run(cfg, db):
    validate(cfg)
    kalshi = Kalshi(cfg["kalshi_base_url"])
    spot_client = CoinbaseSpot()
    vol = Volatility(cfg.get("volatility_lookback_seconds",3600))
    LOG.info("PAPER ONLY. Continuously monitoring entire 15-minute window.")
    while True:
        try:
            mode = state(db,"mode")
            if mode == "STOPPED":
                LOG.error("STOPPED: at least one simulated loss")
                return
            if mode == "WAITING_FOR_SETTLEMENT":
                settle_pending(db,kalshi)
            else:
                candidates = scan_once(cfg,kalshi,spot_client,vol,db)
                if candidates:
                    budget = min(float(state(db,"balance")),float(cfg["stake_usd"]))
                    selected = affordable_single(choose_candidate(candidates), budget)
                    if selected:
                        LOG.info("SINGLE %s distance_pct=%.6f payout=%.3f",selected["ticker"],selected["distance_pct"],selected["payout"])
                        open_paper(db,selected,budget)
            db.commit()
        except (requests.RequestException,ValueError,KeyError) as exc:
            LOG.warning("scan failed closed: %s",exc)
        time.sleep(cfg.get("poll_seconds",2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",default="config.yaml")
    parser.add_argument("--db",default="paper.sqlite3")
    parser.add_argument("--status",action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if args.status:
        if not os.path.exists(args.db):
            print("No paper account database yet. Run the bot after configuring verified markets.")
            return
        db=sqlite3.connect(args.db)
        print("Balance:",db.execute("SELECT value FROM state WHERE key='balance'").fetchone())
        print("Mode:",db.execute("SELECT value FROM state WHERE key='mode'").fetchone())
        for row in db.execute("SELECT id,ticker,side,stake,status,result,pnl FROM trades ORDER BY id DESC LIMIT 20"):
            print(row)
        return
    with open(args.config,encoding="utf-8") as f:
        cfg=yaml.safe_load(f)
    db=init_db(args.db,cfg.get("initial_balance",1000))
    run(cfg,db)

if __name__ == "__main__":
    main()
