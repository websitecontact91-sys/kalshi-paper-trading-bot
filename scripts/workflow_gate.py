"""Validate the persisted session before dependencies or network clients load."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

# This script runs before dependency installation; state_store and trade_config
# use only the standard library. Use the same validated policy as the runner.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from state_store import SCHEMA, migrate

FINGERPRINT_PREFIX = "git-tree-state-excluded-v1:sha256:"


def code_fingerprint(revision="HEAD", *, cwd=None) -> str:
    """Hash the tracked source tree while allowing only mutable paper records to vary.

    Tree records include each path, mode, object type and Git object ID. Keep all
    code, workflows, dependency declarations, policy files, fixtures and tests.
    Only the exact runtime ledger/state paths and archived JSON state are omitted.
    The versioned prefix defines this exclusion policy for evidence consumers.
    """
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--full-tree", "-z", revision, "--"], cwd=cwd)
    digest = hashlib.sha256(FINGERPRINT_PREFIX.encode("ascii") + b"\0")
    retained = 0
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, _ = metadata.split(b" ", 2)
        runtime_path = path in (b"paper_state.json", b"paper_ledger.csv")
        archive_json = path.startswith((b"ledger_archive/", b"paper_sessions/")) and path.endswith(b".json")
        # Executable files and symlinks are never classified as mutable state.
        if mode == b"100644" and kind == b"blob" and (runtime_path or archive_json):
            continue
        digest.update(record + b"\0")
        retained += 1
    if not retained:
        raise ValueError("Cannot fingerprint an empty source tree")
    return FINGERPRINT_PREFIX + digest.hexdigest()


def inspect_state(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ValueError("Missing persistent state; refusing to invent a new session")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Unreadable persistent state; refusing to reset") from exc
    if not isinstance(raw, dict):
        raise ValueError("Invalid persistent state")
    pending = raw.get("pending")
    legacy_pending = raw.get("trade")
    current_schema = raw.get("schema") == SCHEMA
    if ((current_schema and legacy_pending is not None and legacy_pending != pending)
            or (not current_schema and pending is not None and pending != legacy_pending)):
        raise ValueError("Conflicting current and legacy pending positions")
    # Migration is read-only here. The runner saves an authorized policy
    # migration durably before it scans or records another paper entry.
    state = migrate(raw)
    return {"mode": state["mode"], "stopped": state["mode"] == "STOPPED",
            "balance": state["balance"], "history_count": len(state["history"]),
            "saved_mode": raw.get("mode"), "stop_reason": state.get("stop_reason"),
            "policy_migrated": raw.get("stop_policy") != state.get("stop_policy")
                or raw.get("mode") != state["mode"]}


def source_metadata() -> dict:
    actual_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip()
    return {"source_sha": actual_sha,
            "code_fingerprint": code_fingerprint(actual_sha),
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
                out.write("code_fingerprint=" + metadata["code_fingerprint"] + "\n")
                if not args.provenance_only:
                    out.write("stopped=" + str(state["stopped"]).lower() + "\n")
                    out.write("mode=" + state["mode"] + "\n")
        print(json.dumps({**metadata, **state}, sort_keys=True))
        if state.get("stopped"):
            print("STOPPED: available paper cash is below $1.00; retaining the saved ledger and skipping scanning/successor dispatch.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Workflow gate refused startup: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
