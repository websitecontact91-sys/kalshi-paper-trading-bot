"""Validate the persisted session before dependencies or network clients load."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess
import sys

MODES = {"SCANNING", "WAITING_FOR_SETTLEMENT", "STOPPED"}


def inspect_state(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ValueError("Missing persistent state; refusing to invent a new session")
    try:
        state = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Unreadable persistent state; refusing to reset") from exc
    if not isinstance(state, dict) or state.get("mode") not in MODES:
        raise ValueError("Invalid persistent state mode")
    history = state.get("history")
    if not isinstance(history, list) or any(not isinstance(t, dict) for t in history):
        raise ValueError("Invalid persisted trade history")
    if isinstance(state.get("balance"), bool):
        raise ValueError("Invalid persisted balance")
    try:
        balance = Decimal(str(state["balance"]))
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise ValueError("Invalid persisted balance") from exc
    if not balance.is_finite() or balance < 0:
        raise ValueError("Invalid persisted balance")
    if state["mode"] != "STOPPED" and any(t.get("won") is False for t in history):
        raise ValueError("Saved loss requires STOPPED; refusing an automatic restart")
    pending = state.get("pending")
    legacy_pending = state.get("trade")
    if pending is not None and legacy_pending is not None and pending != legacy_pending:
        raise ValueError("Conflicting current and legacy pending positions")
    pending = pending if pending is not None else legacy_pending
    if pending is not None and not isinstance(pending, dict):
        raise ValueError("Invalid pending position")
    if state["mode"] == "WAITING_FOR_SETTLEMENT" and not pending:
        raise ValueError("Waiting state has no pending position")
    if state["mode"] != "WAITING_FOR_SETTLEMENT" and pending is not None:
        raise ValueError("Pending position conflicts with saved mode")
    return {"mode": state["mode"], "stopped": state["mode"] == "STOPPED",
            "balance": str(balance), "history_count": len(history)}


def source_metadata() -> dict:
    actual_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()
    return {"source_sha": actual_sha,
            "event_sha": os.getenv("GITHUB_SHA"),
            "run_id": os.getenv("GITHUB_RUN_ID"),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
            "event_name": os.getenv("GITHUB_EVENT_NAME"),
            "recorded_at": datetime.now(timezone.utc).isoformat()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="paper_state.json")
    parser.add_argument("--github-output")
    parser.add_argument("--metadata")
    parser.add_argument("--provenance-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        state = {} if args.provenance_only else inspect_state(args.state)
        metadata = source_metadata() if args.metadata or args.github_output else {}
        if args.metadata:
            destination = Path(args.metadata)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps({**metadata, "gate": state}, indent=2) + "\n",
                                   encoding="utf-8")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as out:
                out.write("source_sha=" + metadata["source_sha"] + "\n")
                if not args.provenance_only:
                    out.write("stopped=" + str(state["stopped"]).lower() + "\n")
                    out.write("mode=" + state["mode"] + "\n")
        print(json.dumps({**metadata, **state}, sort_keys=True))
        if state.get("stopped"):
            print("STOPPED: retaining the saved ledger; no automatic reset or successor is permitted.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Workflow gate refused startup: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
