"""Assess captured scheduling evidence without contacting GitHub or inventing coverage."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

FINGERPRINT_PATTERN = r"git-tree-state-excluded-v1:sha256:[0-9a-f]{64}"


def timestamp(value) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing or invalid timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timestamps must contain a timezone")
        result = dt.timestamp()
    if not math.isfinite(result):
        raise ValueError("non-finite timestamp")
    return result


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def round_close_time(row):
    """Use the audit timestamp while preserving the state's quarter-hour bucket key."""
    if "close_time" in row:
        # An invalid explicit timestamp must not silently fall back to another field.
        return timestamp(row["close_time"])
    key = row.get("round_key")
    if isinstance(key, str) and re.fullmatch(r"[0-9]+", key):
        # Historical state and current runner keys are ceil(epoch_seconds / 900).
        return timestamp(int(key) * 900)
    # Older standalone evidence files used an ISO close timestamp as the key.
    return timestamp(key)


def evaluate_evidence(runs, coverage, *, minimum_hours=3.0, max_gap_seconds=60.0,
                      chain_break_at=None, cancelled_run_id=None, recovery_limit_seconds=600.0):
    if isinstance(runs, dict):
        runs = runs.get("workflow_runs", runs.get("runs", []))
    if not isinstance(runs, list):
        raise ValueError("run evidence must be a list or a workflow_runs object")
    runs = [r for r in runs if isinstance(r, dict)]
    scheduled = [r for r in runs if r.get("event") == "schedule"]
    intervals, rounds, invalid, sources, fingerprints = [], {}, [], set(), set()
    for index, record in enumerate(coverage):
        try:
            if not isinstance(record, dict):
                raise ValueError("coverage record is not an object")
            start, end = timestamp(record.get("scan_started_at")), timestamp(record.get("scan_ended_at"))
            if end <= start:
                raise ValueError("scan end must follow scan start")
            source = record.get("source_sha")
            if not isinstance(source, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source):
                raise ValueError("actual source SHA is missing")
            if record.get("provenance_conflict"):
                raise ValueError("coverage and workflow provenance disagree")
            source = source.strip()
            sources.add(source)
            fingerprint = record.get("code_fingerprint")
            if not isinstance(fingerprint, str) or not re.fullmatch(FINGERPRINT_PATTERN, fingerprint):
                raise ValueError("missing or invalid versioned code fingerprint")
            fingerprints.add(fingerprint)
            run_id = str(record.get("run_id", record.get("workflow_run_id")) or "")
            intervals.append((start, end, source, run_id))
            for row in record.get("rounds", []):
                if not isinstance(row, dict):
                    raise ValueError("round coverage is not an object")
                close = round_close_time(row)
                if row.get("final_window_scanned") is True:
                    if not (start < close and end >= close - 60):
                        raise ValueError("reported final-minute scan lies outside this run's observation interval")
                    rounds[close] = True
                else:
                    rounds.setdefault(close, False)
        except (TypeError, ValueError, OverflowError) as exc:
            invalid.append({"record": index, "error": str(exc)})
    intervals = sorted(set(intervals))
    gaps = [{"previous_last_scan": iso(a[1]), "next_first_scan": iso(b[0]),
             "gap_seconds": round(b[0] - a[1], 6)}
            for a, b in zip(intervals, intervals[1:])]
    first = min((i[0] for i in intervals), default=None)
    last = max((i[1] for i in intervals), default=None)
    span = last - first if first is not None else 0
    expected = []
    if first is not None:
        boundary = int(first // 900) * 900
        while boundary <= last:
            if boundary - 60 >= first:
                expected.append(float(boundary))
            boundary += 900
    missed = [iso(close) for close in expected if rounds.get(close) is not True]

    recovery = None
    cancellation = None
    if chain_break_at is not None and cancelled_run_id is not None:
        reported_break = timestamp(chain_break_at)
        cancelled = next((r for r in runs
            if str(r.get("databaseId", r.get("id"))) == str(cancelled_run_id)
            and r.get("conclusion") == "cancelled"
            and r.get("event") == "workflow_dispatch"), None)
        broken = None
        if cancelled is not None:
            try:
                broken = timestamp(cancelled.get("cancelled_at", cancelled.get("updatedAt", cancelled.get("updated_at"))))
                if abs(broken - reported_break) <= max_gap_seconds:
                    cancellation = cancelled
            except (TypeError, ValueError, OverflowError):
                pass
        if cancellation is not None and broken is not None:
            candidates = []
            for run in scheduled:
                if run.get("status") not in ("completed", "in_progress"):
                    continue
                if run.get("conclusion") not in (None, "success"):
                    continue
                try:
                    start = timestamp(run.get("startedAt", run.get("run_started_at")))
                except (TypeError, ValueError, OverflowError):
                    continue
                if broken <= start <= broken + recovery_limit_seconds:
                    # A schedule event alone is not evidence that scanning resumed.
                    run_id = str(run.get("databaseId", run.get("id")))
                    scans = [i for i in intervals if i[3] == run_id
                             and start <= i[0] <= broken + recovery_limit_seconds]
                    if scans:
                        candidates.append((start, run, min(scans)))
            if candidates:
                start, run, scan = min(candidates, key=lambda item: item[0])
                recovery = {"run_id": run.get("databaseId", run.get("id")),
                            "recorded_chain_cancellation_at": iso(broken),
                            "scheduled_run_started_at": iso(start),
                            "first_scan_at": iso(scan[0]),
                            "scanner_recovery_seconds": round(scan[0] - broken, 6)}

    checks = {
        "actual_scan_intervals_available": len(intervals) >= 2,
        "coverage_records_valid": not invalid and bool(intervals),
        "one_code_version_observed": bool(intervals) and len(fingerprints) == 1,
        "observation_span_sufficient": span >= minimum_hours * 3600,
        "successor_scan_gaps_within_limit": bool(gaps) and all(
            0 <= gap["gap_seconds"] <= max_gap_seconds for gap in gaps),
        "final_minute_coverage_recorded": bool(expected) and not missed,
        "scheduled_events_observed": bool(scheduled),
        "deliberate_chain_cancellation_recorded": cancellation is not None,
        "scheduled_scanning_recovery_observed": recovery is not None,
    }
    return {
        "schedule_status": "VERIFIED" if all(checks.values()) else "NOT_VERIFIED",
        "checks": checks,
        "remaining_evidence": [key for key, passed in checks.items() if not passed],
        "minimum_observation_hours": minimum_hours,
        "observed_span_hours": round(span / 3600, 6),
        "first_scan_at": iso(first), "last_scan_at": iso(last),
        "source_shas": sorted(sources),
        "code_fingerprints": sorted(fingerprints),
        "coverage_run_ids": sorted({i[3] for i in intervals if i[3]}),
        "coverage_intervals": len(intervals),
        "scheduled_events_observed": len(scheduled),
        "scanner_gaps": gaps,
        "expected_complete_final_windows": len(expected),
        "missed_or_unproven_final_windows": missed,
        "invalid_coverage_records": invalid,
        "chain_recovery": recovery,
        "notes": [
            "This assesses supplied schedule evidence, not payout parity or global trading readiness.",
            "Measure last scan to next first scan; serialized 600-second jobs cannot start less than a minute apart.",
            "Scheduled events can be delayed or dropped; a five-minute cron is not an execution guarantee.",
            "Each listed source SHA is preserved; different state-only commits are not silently represented as one SHA.",
            "Every scan interval must share one versioned tracked-tree fingerprint; only runtime state, ledger and archived state JSON are excluded.",
            "Recovery requires the cancelled run's updatedAt and coverage bound to the recovering schedule run ID.",
        ],
    }


def read_coverage(inputs):
    files = []
    for name in inputs:
        path = Path(name)
        files.extend(sorted(path.rglob("coverage.json")) if path.is_dir() else [path])
    records = []
    for path in dict.fromkeys(files):
        payload = json.loads(path.read_text(encoding="utf-8"))
        provenance_path = path.with_name("workflow_provenance.json")
        if isinstance(payload, dict) and "scan_started_at" in payload and provenance_path.is_file():
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            if provenance.get("source_sha") != payload.get("source_sha"):
                payload["provenance_conflict"] = True
            if provenance.get("code_fingerprint") != payload.get("code_fingerprint"):
                payload["provenance_conflict"] = True
            if payload.get("run_id") and str(payload["run_id"]) != str(provenance.get("run_id")):
                payload["provenance_conflict"] = True
            payload.setdefault("run_id", provenance.get("run_id"))
        if isinstance(payload, list):
            records.extend(payload)
        elif isinstance(payload, dict) and isinstance(payload.get("runs"), list):
            records.extend(payload["runs"])
        else:
            records.append(payload)
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, help="Captured gh run list or REST workflow-runs JSON")
    parser.add_argument("--coverage", nargs="*", default=[], help="Coverage JSON files or artifact directories")
    parser.add_argument("--minimum-hours", type=float, default=3.0)
    parser.add_argument("--max-gap-seconds", type=float, default=60.0)
    parser.add_argument("--chain-break-at")
    parser.add_argument("--cancelled-run-id")
    parser.add_argument("--recovery-limit-seconds", type=float, default=600.0)
    parser.add_argument("--output", default="schedule_evidence.json")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args(argv)
    for value in (args.minimum_hours, args.max_gap_seconds, args.recovery_limit_seconds):
        if not math.isfinite(value) or value <= 0:
            parser.error("evidence thresholds must be finite and positive")
    try:
        runs = json.loads(Path(args.runs).read_text(encoding="utf-8"))
        report = evaluate_evidence(
            runs, read_coverage(args.coverage),
            minimum_hours=args.minimum_hours, max_gap_seconds=args.max_gap_seconds,
            chain_break_at=args.chain_break_at, cancelled_run_id=args.cancelled_run_id,
            recovery_limit_seconds=args.recovery_limit_seconds)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["schedule_status"] == "VERIFIED" or args.allow_incomplete else 2
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"schedule_status": "NOT_VERIFIED", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

