"""Read-only live report; configuration, freshness and eligibility are distinct.

Run: python validate_universe.py --output universe_report.csv \
         --json-output universe_report.json --wait 10

The report evaluates quotes outside the entry window as well.  It never enters
a trade or updates paper state, and a successful diagnostic exit is not a
readiness assertion.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import sys
import time

from payout_quote import as_decimal
from quote_adapter import QuoteDetails, build_quote
from selection import Candidate, rejection_reason
from timing import MAX_OBS_AGE_S, in_entry_window, observation_fresh, timestamp
from universe import EXCLUDED_FAMILIES, UNIVERSE, check_compatibility, series_of, validate_configuration

FLAGS = ("configured", "available", "fresh", "compatible", "evaluated", "eligible")
CSV_FIELDS = (
    "ticker", "instrument", "source_type", "source_id", "units", "target_rule",
    "direction_rule", "market_ticker", "configured", "available", "fresh",
    "compatible", "evaluated", "eligible", "entry_window_open", "current",
    "target", "last_observation_time", "observation_age_seconds", "evaluated_at", "close_time",
    "compatibility_reason", "reason", "yes_ask", "no_ask", "eligible_sides",
    "quote_ready", "quote_blocker", "ui_parity_verified", "official_settlement_sources", "contract_terms_url",
    "quote_details", "rejections",
)


def _safe_error(exc, prefix):
    # Do not echo unexpected exception messages: network/auth libraries may put
    # URLs, headers or credential material in them.
    message = str(exc).casefold()
    if any(marker in message for marker in (
        "kalshi_api_key", "kalshi_private_key", "missing credential", "no api key",
        "missing api key", "private key is required", "credentials unavailable",
    )):
        return "kalshi_reference_credentials_unavailable"
    return prefix + ":" + type(exc).__name__


def _decimal_text(value):
    try:
        return str(as_decimal(value))
    except (ValueError, TypeError):
        return ""


def _iso(value):
    try:
        return dt.datetime.fromtimestamp(timestamp(value), dt.timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def _current_market(markets, now):
    active = []
    for market in markets:
        if market.get("status") not in {"open", "active"}:
            continue
        try:
            close = timestamp(market.get("close_time"))
            if close <= now:
                continue
            if market.get("open_time") is not None and timestamp(market["open_time"]) > now:
                continue
        except (ValueError, TypeError):
            continue
        active.append((close, str(market.get("ticker") or ""), market))
    return min(active, key=lambda item: item[:2])[2] if active else None


def quote_parity_status(fixture_path=None):
    """A screenshot arithmetic example does not satisfy this evidence gate."""
    path = Path(fixture_path) if fixture_path else Path(__file__).parent / "tests/fixtures/sol_5usd_quote.json"
    if not path.is_file():
        return {"verified": False, "reason": "captured_SOL_raw_quote_fixture_missing"}
    try:
        detail = build_quote(json.loads(path.read_text(encoding="utf-8")), budget="5.00")
        matches = (
            detail.source == "kalshi_response" and detail.ui_parity_verified
            and detail.quote.debit == as_decimal("5.00")
            and detail.quote.payout == as_decimal("5.69")
            and detail.quote.displayed_multiplier == as_decimal("1.14")
        )
    except (ValueError, TypeError, OSError, KeyError):
        return {"verified": False, "reason": "captured_SOL_quote_fixture_invalid_or_unverified"}
    return {"verified": bool(matches), "reason": "verified" if matches else "SOL_quote_UI_parity_unverified"}


def _new_row(spec):
    return {
        **spec, **{flag: False for flag in FLAGS}, "configured": True,
        "market_ticker": "", "entry_window_open": False, "current": "", "target": "",
        "last_observation_time": "", "observation_age_seconds": None, "evaluated_at": "", "close_time": "",
        "compatibility_reason": "not_observed", "reason": "no_current_market",
        "yes_ask": "", "no_ask": "", "eligible_sides": [],
        "quote_ready": False, "quote_blocker": "quote_policy_unavailable",
        "ui_parity_verified": False, "official_settlement_sources": [],
        "contract_terms_url": "", "quote_details": {}, "rejections": {},
    }


def _policy_status(client):
    """Use the same evidence gate as the live runner; no independent bypass."""
    policy = getattr(client, "policy", {})
    if not isinstance(policy, dict):
        policy = {}
    reasons = getattr(client, "readiness_reasons", [])
    if not isinstance(reasons, (list, tuple)):
        reasons = ["invalid_client_readiness_report"]
    reasons = [str(reason) for reason in reasons if reason]
    verified = policy.get("verified") is True and not reasons
    reason = None if verified else str(policy.get("reason") or (reasons[0] if reasons else "quote_policy_unavailable"))
    return {
        "verified": verified, "reason": reason,
        "balance_precision": policy.get("balance_precision"),
        "readiness_reasons": reasons, "verification_source": "client.quote_policy",
    }


def build_report(client, *, now=None, wait=0, clock=time.time, sleeper=time.sleep,
                 source_sha=None, parity_fixture=None):
    """Build all 30 rows through an injected read-only client (no network in tests)."""
    validate_configuration()
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 60:
        raise ValueError("wait must be between zero and 60 seconds")
    policy = _policy_status(client)
    observed_now = timestamp(clock() if now is None else now)
    errors = []
    discovery_failed = False
    try:
        markets = client.discover(now=observed_now)
        if not isinstance(markets, list) or any(not isinstance(item, dict) for item in markets):
            raise ValueError("Invalid discovery response")
    except Exception as exc:
        markets = []
        discovery_failed = True
        errors.append(_safe_error(exc, "public_discovery_failed"))
    discovery_errors = getattr(client, "discovery_errors", {})
    if not isinstance(discovery_errors, dict):
        discovery_errors = {"discovery": "invalid_discovery_error_report"}
    # Wait only for read-only reference-feed observations, never for an entry.
    if wait and not discovery_failed:
        sleeper(wait)
    report_now = timestamp(clock() if now is None else now)
    by_series = {series: [] for series in UNIVERSE}
    for market in markets:
        series = series_of(market)
        if series in by_series:
            by_series[series].append(market)
    rows = []
    for series, spec in UNIVERSE.items():
        row = _new_row(spec)
        row["quote_ready"] = policy["verified"]
        row["quote_blocker"] = policy["reason"]
        metadata = {}
        try:
            metadata = client.series_metadata(series)
            if isinstance(metadata, dict) and isinstance(metadata.get("series"), dict):
                metadata = metadata["series"]
            if not isinstance(metadata, dict):
                metadata = {}
            row["official_settlement_sources"] = metadata.get("settlement_sources") or []
            row["contract_terms_url"] = metadata.get("contract_terms_url") or ""
        except Exception as exc:
            row["compatibility_reason"] = _safe_error(exc, "series_metadata_unavailable")
        row_now = timestamp(clock() if now is None else now)
        row["evaluated_at"] = _iso(row_now)
        market = _current_market(by_series[series], row_now)
        if market is None:
            if discovery_failed or series in discovery_errors:
                row["reason"] = "public_market_discovery_unavailable"
            rows.append(row)
            continue
        row.update(available=True, market_ticker=str(market.get("ticker") or ""),
                   target=_decimal_text(market.get("floor_strike")), close_time=_iso(market.get("close_time")))
        row["entry_window_open"] = in_entry_window(market["close_time"], row_now)
        try:
            snapshot = client.snapshot(market, now=row_now, include_quotes=True)
            if not isinstance(snapshot, dict):
                raise ValueError("Invalid snapshot response")
        except Exception as exc:
            row["reason"] = _safe_error(exc, "snapshot_unavailable")
            rows.append(row)
            continue
        refreshed = snapshot.get("market", market)
        if (not isinstance(refreshed, dict) or series_of(refreshed) != series
                or refreshed.get("ticker") != market.get("ticker")):
            row["reason"] = "snapshot_market_identity_mismatch"
            rows.append(row)
            continue
        market = refreshed
        row["target"] = _decimal_text(market.get("floor_strike"))
        row["close_time"] = _iso(market.get("close_time"))
        row["quote_ready"] = policy["verified"] and snapshot.get("quote_ready", True) is True
        if not row["quote_ready"]:
            row["quote_blocker"] = str(snapshot.get("quote_blocker") or policy["reason"] or "quote_evidence_unverified")
        current = snapshot.get("current")
        observed = snapshot.get("observation_time")
        # Serial public requests can take longer than the freshness window.
        # Recheck at each completed snapshot, not at the start of the inventory.
        row_now = timestamp(clock() if now is None else now)
        row["evaluated_at"] = _iso(row_now)
        try:
            row["entry_window_open"] = in_entry_window(market["close_time"], row_now)
            still_open = timestamp(market["close_time"]) > row_now and market.get("status") in {"open", "active"}
        except (KeyError, ValueError, TypeError):
            row["entry_window_open"], still_open = False, False
        row["current"] = _decimal_text(current)
        row["last_observation_time"] = _iso(observed)
        row["fresh"] = bool(row["current"] and as_decimal(row["current"]) > 0 and observation_fresh(observed, row_now))
        if row["last_observation_time"]:
            row["observation_age_seconds"] = row_now - timestamp(observed)
        row["compatible"], row["compatibility_reason"] = check_compatibility(market, current, metadata)
        # A stricter client-level check is retained, never overridden by the
        # inventory check.  This also exposes a client's explicit feed error.
        if snapshot.get("compatible") is False:
            row["compatible"] = False
            row["compatibility_reason"] = str(snapshot.get("compatibility_reason") or row["compatibility_reason"])
        asks = snapshot.get("asks") or {}
        quotes = snapshot.get("quotes") or {}
        rejections = snapshot.get("rejections") or {}
        if not all(isinstance(item, dict) for item in (asks, quotes, rejections)):
            row["reason"] = "invalid_snapshot_quote_structure"
            rows.append(row)
            continue
        row["evaluated"] = all(side in quotes or side in rejections for side in ("YES", "NO"))
        for side in ("YES", "NO"):
            row[side.lower() + "_ask"] = _decimal_text(asks.get(side))
            detail = quotes.get(side)
            if not isinstance(detail, QuoteDetails):
                row["rejections"][side] = str(rejections.get(side) or "quote_unavailable")
                continue
            row["quote_details"][side] = detail.ledger_fields()
            try:
                candidate = Candidate(
                    ticker=row["market_ticker"], side=side, ask=asks.get(side), current=current,
                    target=market.get("floor_strike"), quote=detail.quote,
                    fresh=row["fresh"] and row["compatible"] and still_open and row["quote_ready"],
                )
                reason = rejection_reason(candidate)
            except (ValueError, TypeError):
                reason = "invalid_candidate_inputs"
            if not row["compatible"]:
                reason = "incompatible: " + row["compatibility_reason"]
            elif not row["fresh"]:
                reason = "stale_or_missing_reference"
            elif not still_open:
                reason = "closed_during_evaluation"
            elif not row["quote_ready"]:
                reason = row["quote_blocker"]
            if reason is None:
                row["eligible_sides"].append(side)
            else:
                row["rejections"][side] = reason
        row["eligible"] = bool(row["eligible_sides"])
        row["ui_parity_verified"] = bool(row["quote_details"]) and all(
            detail["ui_parity_verified"] for detail in row["quote_details"].values()
        )
        if not still_open:
            row["available"] = False
            row["reason"] = "closed_during_evaluation"
        elif not row["fresh"]:
            row["reason"] = "stale_or_missing_reference"
        elif not row["compatible"]:
            row["reason"] = row["compatibility_reason"]
        elif not row["quote_ready"]:
            row["reason"] = row["quote_blocker"]
        elif not row["evaluated"]:
            row["reason"] = "incomplete_quote_evaluation"
        elif row["eligible"]:
            row["reason"] = "eligible_now" if row["entry_window_open"] else "eligible_quote_outside_entry_window"
        else:
            row["reason"] = "no_eligible_side"
        rows.append(row)
    if len(rows) != 30 or {row["ticker"] for row in rows} != set(UNIVERSE):
        raise ValueError("Report must retain exactly the 30 configured series")
    sha = source_sha if source_sha is not None else os.environ.get("PAPER_SOURCE_SHA", "")
    # The real adapter validates its configured raw capture, the UI evidence and
    # the documented model together.  A modeled quote's own ui_parity flag stays
    # false, even after that policy is verified; do not confuse those concepts.
    parity = dict(policy)
    if parity_fixture is not None:
        parity["independent_fixture_check"] = quote_parity_status(parity_fixture)
    blockers = []
    for flag in ("configured", "available", "fresh", "compatible", "evaluated"):
        failing = [row["ticker"] for row in rows if not row[flag]]
        if failing:
            blockers.append({"gate": flag, "series": failing})
    if not parity["verified"]:
        blockers.append({"gate": "historical_SOL_quote_UI_parity", "reason": parity["reason"]})
    quote_blocked = [row["ticker"] for row in rows if row["available"] and not row["quote_ready"]]
    if quote_blocked:
        blockers.append({"gate": "quote_policy", "series": quote_blocked, "reasons": policy["readiness_reasons"]})
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(sha)):
        blockers.append({"gate": "source_sha", "reason": "exact_commit_SHA_missing"})
    # This inventory cannot certify the workflow's same-SHA tests, official
    # settlement scenarios, or several hours of schedule/chain coverage.
    blockers.append({"gate": "deployment_and_schedule_evidence", "reason": "requires_same_commit_workflow_and_multihour_schedule_evidence"})
    all_public_failed = discovery_failed or (
        not markets and all(series in discovery_errors for series in UNIVERSE)
    )
    dow = next(row for row in rows if row["ticker"] == "KXDJIA15M")
    return {
        "schema": "kalshi-paper.universe-report.v1", "read_only": True,
        "generated_at": _iso(report_now), "source_sha": sha,
        "code_fingerprint": os.environ.get("PAPER_CODE_FINGERPRINT"), "configured_count": 30,
        "max_observation_age_seconds": MAX_OBS_AGE_S,
        "flag_definitions": {
            "configured": "present in the exact requested mapping",
            "available": "a currently open market was retrieved",
            "fresh": "a finite reference observation is within the shared freshness bounds",
            "compatible": "binary, notional, target, direction and official source checks passed",
            "evaluated": "both sides received a quote or an explicit rejection",
            "eligible": "at least one side passes price/payout/freshness/compatibility and the live quote policy; entry window is separate",
        },
        "exclusions": [{"family": family, "aliases": list(aliases)} for family, aliases in EXCLUDED_FAMILIES.items()],
        "rows": rows, "counts": {flag: sum(bool(row[flag]) for row in rows) for flag in FLAGS},
        "dow_check": {key: dow[key] for key in ("ticker", "available", "current", "target", "units", "compatible", "compatibility_reason")},
        "quote_policy": policy, "quote_parity": parity, "ready": not blockers, "readiness_blockers": blockers,
        "errors": errors, "discovery_failed_series": sorted(str(key) for key in discovery_errors),
        "all_public_discovery_failed": all_public_failed, "diagnostic_exit_code": 1 if all_public_failed else 0,
    }


def _csv_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def csv_text(report):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in report["rows"]:
        writer.writerow({key: _csv_value(row.get(key, "")) for key in CSV_FIELDS})
    return output.getvalue()


def write_report(report, output, json_output):
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(json_output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(csv_text(report), encoding="utf-8")
    Path(json_output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None, *, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="universe_report.csv")
    parser.add_argument("--json-output", default="universe_report.json")
    parser.add_argument("--wait", type=float, default=10)
    parser.add_argument("--quote-policy", default="quote_policy.json")
    args = parser.parse_args(argv)
    client = None
    try:
        if client_factory is None:
            from live_client import LiveKalshiClient
            client_factory = LiveKalshiClient
        client = client_factory(policy_path=args.quote_policy)
        report = build_report(client, wait=args.wait)
        write_report(report, args.output, args.json_output)
        print(csv_text(report), end="")
        print(json.dumps({key: report[key] for key in (
            "source_sha", "counts", "exclusions", "dow_check", "ready", "readiness_blockers", "errors"
        )}, sort_keys=True))
        return report["diagnostic_exit_code"]
    except Exception as exc:
        print(_safe_error(exc, "universe_report_failed"), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
