"""Workflow gates and evidence checks for the deployed paper-only runner."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts.schedule_evidence import evaluate_evidence, main as evidence_main, read_coverage
from scripts.workflow_gate import (FINGERPRINT_PREFIX, code_fingerprint, inspect_state,
                                   main as gate_main)

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = FINGERPRINT_PREFIX + "c" * 64


def workflow(name):
    # YAML 1.1 treats the GitHub key "on" as a boolean; BaseLoader preserves it.
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def position(steps, command):
    return next(index for index, step in enumerate(steps) if command in step.get("run", ""))


def test_requested_schedule_duration_and_single_writer_are_preserved():
    doc = workflow("main.yml")
    assert doc["on"]["schedule"] == [{"cron": "*/5 * * * *"}]
    assert doc["on"]["push"]["paths"] == [".github/workflows/main.yml"]
    assert "workflow_dispatch" in doc["on"]
    assert doc["concurrency"] == {"group": "kalshi-paper-state", "cancel-in-progress": "false"}
    assert doc["permissions"] == {"contents": "write", "actions": "write"}
    steps = doc["jobs"]["monitor"]["steps"]
    run = steps[position(steps, "python -u paper_runner.py")]
    assert "--duration 600 --interval 2" in run["run"]
    assert "--paper --persist-to-github --state paper_state.json" in run["run"]
    assert "--report-dir runtime_reports" in run["run"]


def test_live_deployment_is_disabled_and_account_verification_cannot_submit_orders():
    config = json.loads((ROOT / 'live_config.json').read_text())
    assert config['enabled'] is False
    assert config['max_total_cost_usd'] == '1.00'
    doc = workflow('live-readiness.yml')
    assert doc['permissions'] == {'contents': 'read'}
    commands = '\n'.join(step.get('run', '') for step in doc['jobs']['verify']['steps'])
    assert 'live_runner.py --preflight' in commands
    assert '--execute-live' not in commands and '--initialize-state' not in commands


def test_live_schedule_has_separate_serialization_and_stop_gate_before_successor():
    doc = workflow('live-trading.yml')
    assert doc['on']['schedule'] == [{'cron': '*/5 * * * *'}]
    assert 'push' not in doc['on']
    assert doc['on']['workflow_dispatch']['inputs']['mode']['default'] == 'preflight'
    assert doc['concurrency'] == {'group': 'kalshi-live-state', 'cancel-in-progress': 'false'}
    job = doc['jobs']['live']
    assert "vars.KALSHI_LIVE_SCHEDULE_ENABLED == 'true'" in job['if']
    steps = job['steps']
    state_gate = position(steps, '--inspect-state --persist-to-github')
    dispatch = position(steps, 'gh workflow run live-trading.yml')
    execute = position(steps, '--execute-live --confirm-real-money --persist-to-github')
    assert state_gate < dispatch < execute
    for index in (dispatch, execute):
        assert "steps.activation.outputs.allowed == 'true'" in steps[index]['if']
        assert "steps.state.outputs.can_run == 'true'" in steps[index]['if']
    assert "vars.KALSHI_LIVE_SCHEDULE_ENABLED == 'true'" in steps[dispatch]['if']
    assert 'paper_state.json' not in steps[execute]['run']


@pytest.mark.parametrize('enabled,event,mode,confirmation,schedule_enabled,allowed', [
    (False, 'workflow_dispatch', 'run', 'I_ACCEPT_REAL_MONEY_TRADES', 'true', False),
    (True, 'workflow_dispatch', 'run', '', 'true', False),
    (True, 'workflow_dispatch', 'run', 'I_ACCEPT_REAL_MONEY_TRADES', 'false', True),
    (False, 'schedule', '', '', 'true', False),
    (True, 'schedule', '', '', 'false', False),
    (True, 'schedule', '', '', 'true', True),
    (False, 'workflow_dispatch', 'preflight', '', 'false', True),
    (False, 'workflow_dispatch', 'reconcile', '', 'false', True),
])
def test_actual_live_workflow_gate_rejects_unarmed_execution(tmp_path, enabled, event, mode,
                                                         confirmation, schedule_enabled, allowed):
    doc = workflow('live-trading.yml')
    block = next(s['run'] for s in doc['jobs']['live']['steps'] if s.get('id') == 'activation')
    script = block.split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    config = json.loads((ROOT / 'live_config.json').read_text())
    config['enabled'] = enabled
    (tmp_path / 'live_config.json').write_text(json.dumps(config))
    output = tmp_path / 'output'
    env = dict(os.environ, EVENT_NAME=event, REQUESTED_MODE=mode, OWNER_CONFIRMATION=confirmation,
               SCHEDULE_ENABLED=schedule_enabled, GITHUB_OUTPUT=str(output))
    completed = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                               text=True, capture_output=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert 'allowed=' + str(allowed).lower() in output.read_text()


def test_chain_dispatch_requires_tests_simulations_and_universe_to_pass():
    steps = workflow("main.yml")["jobs"]["monitor"]["steps"]
    dispatch = position(steps, "gh workflow run main.yml")
    assert position(steps, "python -m pytest -q") < dispatch
    assert position(steps, "simulate.py --scenario all") < dispatch
    assert position(steps, "validate_universe.py") < dispatch
    assert dispatch < position(steps, "python -u paper_runner.py")
    assert "success()" in steps[dispatch]["if"]
    assert "stopped != 'true'" in steps[dispatch]["if"]
    assert "continue-on-error" not in steps[dispatch]
    assert all("set -euo pipefail" in step["run"] for step in steps
               if "tee runtime_reports/" in step.get("run", ""))


def test_stopped_run_exits_before_install_and_never_disables_or_resets():
    doc = workflow("main.yml")
    steps = doc["jobs"]["monitor"]["steps"]
    gate = position(steps, "scripts/workflow_gate.py")
    setup = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/setup-python@"))
    assert gate < setup
    for command in ("pip install", "python -m pytest", "simulate.py", "validate_universe.py",
                    "gh workflow run", "python -u paper_runner.py"):
        step = steps[position(steps, command)]
        assert "stopped != 'true'" in step["if"]
    raw = (ROOT / ".github/workflows/main.yml").read_text()
    assert "gh workflow disable" not in raw
    assert "--confirm-reset" not in raw and "--new-session" not in raw
    artifact = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert artifact["if"] == "always()"


def test_validation_workflow_always_tests_and_live_checks_are_read_only_on_main():
    doc = workflow("coverage-validation.yml")
    assert doc["permissions"] == {"contents": "read"}
    assert "pull_request" in doc["on"] and "push" in doc["on"]
    steps = doc["jobs"]["verify"]["steps"]
    tests = steps[position(steps, "python -m pytest -q")]
    assert "if" not in tests
    assert "--ignore" not in tests["run"]
    assert "python -m compileall -q ." in tests["run"]
    assert "if" not in steps[position(steps, "simulate.py --scenario all")]
    live = steps[position(steps, "python -u paper_runner.py")]
    assert "--duration 90 --interval 2" in live["run"]
    assert "--paper" not in live["run"] and "--persist-to-github" not in live["run"]
    assert "github.event_name != 'pull_request'" in live["if"]
    assert "github.ref == 'refs/heads/main'" in live["if"]


def test_both_workflows_record_actual_checkout_and_preserve_evidence():
    for name, job, step_id in (("main.yml", "monitor", "gate"),
                                ("coverage-validation.yml", "verify", "provenance")):
        steps = workflow(name)["jobs"][job]["steps"]
        provenance = next(s for s in steps if s.get("id") == step_id)
        assert "--metadata runtime_reports/workflow_provenance.json" in provenance["run"]
        assert "--github-output" in provenance["run"]
        live = steps[position(steps, "python -u paper_runner.py")]
        assert "outputs.source_sha" in live["env"]["PAPER_SOURCE_SHA"]
        assert "outputs.code_fingerprint" in live["env"]["PAPER_CODE_FINGERPRINT"]
        universe = steps[position(steps, "python validate_universe.py")]
        assert "outputs.code_fingerprint" in universe["env"]["PAPER_CODE_FINGERPRINT"]
        upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
        for expected in ("runtime_reports/", "universe_report.csv", "universe_report.json"):
            assert expected in upload["with"]["path"]


def saved_state(tmp_path, **updates):
    data = {"mode": "SCANNING", "balance": "995.71", "history": [], "pending": None}
    data.update(updates)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data))
    return path


def test_gate_preserves_a_stopped_loss_and_never_writes_state(tmp_path):
    path = saved_state(tmp_path, mode="STOPPED", balance="995.20", history=[{"won": False}])
    original = path.read_bytes()
    assert inspect_state(path) == {
        "mode": "STOPPED", "stopped": True, "balance": "995.20", "history_count": 1}
    assert path.read_bytes() == original


@pytest.mark.parametrize("updates", [
    {"mode": "SCANNING", "history": [{"won": False}]},
    {"mode": "WAITING_FOR_SETTLEMENT"},
    {"mode": "SCANNING", "pending": {"ticker": "X"}},
    {"balance": "NaN"}, {"balance": "Infinity"}, {"balance": "-0.01"},
    {"balance": True}, {"history": {}}, {"history": [None]}, {"mode": "unknown"},
])
def test_gate_rejects_corrupt_or_reset_loss_state(tmp_path, updates):
    with pytest.raises(ValueError):
        inspect_state(saved_state(tmp_path, **updates))


def test_gate_accepts_current_or_legacy_pending_and_rejects_conflicts(tmp_path):
    path = saved_state(tmp_path, mode="WAITING_FOR_SETTLEMENT", trade={"ticker": "OLD"})
    assert inspect_state(path)["mode"] == "WAITING_FOR_SETTLEMENT"
    path = saved_state(tmp_path, mode="WAITING_FOR_SETTLEMENT", pending={"ticker": "NEW"})
    assert inspect_state(path)["mode"] == "WAITING_FOR_SETTLEMENT"
    path = saved_state(tmp_path, mode="WAITING_FOR_SETTLEMENT",
                       pending={"ticker": "NEW"}, trade={"ticker": "OLD"})
    with pytest.raises(ValueError, match="Conflicting"):
        inspect_state(path)


def test_gate_missing_state_never_creates_a_session(tmp_path):
    path = tmp_path / "missing.json"
    assert gate_main(["--state", str(path)]) == 1
    assert not path.exists()


def test_gate_outputs_actual_sha_and_stop_without_loading_network_dependencies(tmp_path, monkeypatch):
    path = saved_state(tmp_path, mode="STOPPED", history=[{"won": False}])
    output, metadata = tmp_path / "output", tmp_path / "metadata.json"
    monkeypatch.setattr("scripts.workflow_gate.source_metadata",
                        lambda: {"source_sha": "a" * 40, "event_sha": "b" * 40,
                                 "code_fingerprint": FINGERPRINT})
    assert gate_main(["--state", str(path), "--github-output", str(output),
                      "--metadata", str(metadata)]) == 0
    assert "stopped=true" in output.read_text()
    assert "source_sha=" + "a" * 40 in output.read_text()
    assert "code_fingerprint=" + FINGERPRINT in output.read_text()
    assert json.loads(metadata.read_text())["source_sha"] != "b" * 40


def test_code_fingerprint_survives_state_commits_but_detects_source_and_policy_changes(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    def commit():
        git("add", ".")
        git("-c", "user.name=Paper test", "-c", "user.email=paper-test@example.invalid",
            "commit", "-qm", "Fingerprint fixture")
        return git("rev-parse", "HEAD")
    git("init", "-q")
    (tmp_path / "runner.py").write_text("print('version one')\n")
    (tmp_path / "paper_state.json").write_text('{"balance":"1000.00"}')
    first_sha = commit()
    first = code_fingerprint(cwd=tmp_path)
    (tmp_path / "paper_state.json").write_text('{"balance":"995.00"}')
    (tmp_path / "paper_ledger.csv").write_text("debit\n5.00\n")
    for archive in ("ledger_archive", "paper_sessions"):
        (tmp_path / archive).mkdir()
        (tmp_path / archive / "session.json").write_text('{"mode":"STOPPED"}')
    assert commit() != first_sha
    assert code_fingerprint(cwd=tmp_path) == first
    for name in ("runner.py", "quote_policy.json", "requirements.txt", ".github/workflows/main.yml",
                 "tests/fixtures/quote.json", "ledger_archive/executable.py"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        before = code_fingerprint(cwd=tmp_path)
        target.write_text("changed source or configuration\n")
        commit()
        assert code_fingerprint(cwd=tmp_path) != before


def evidence_fixture():
    base = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
    stamp = lambda seconds: (base + timedelta(seconds=seconds)).isoformat()
    records = [
        {"source_sha": "a" * 40, "run_id": "10", "scan_started_at": stamp(0), "scan_ended_at": stamp(600), "rounds": []},
        {"source_sha": "a" * 40, "run_id": "2", "scan_started_at": stamp(630), "scan_ended_at": stamp(1230),
         "rounds": [{"round_key": stamp(900), "final_window_scanned": True, "reason": "no_candidate"}]},
    ]
    for record in records:
        record["code_fingerprint"] = FINGERPRINT
    runs = [
        {"databaseId": 1, "event": "workflow_dispatch", "conclusion": "cancelled", "status": "completed", "updatedAt": stamp(600)},
        {"databaseId": 2, "event": "schedule", "status": "completed",
         "conclusion": "success", "startedAt": stamp(610)},
    ]
    kwargs = {"minimum_hours": .2, "chain_break_at": stamp(600), "cancelled_run_id": "1"}
    return runs, records, kwargs


def test_evidence_measures_scan_gaps_not_job_start_gaps():
    runs, coverage, kwargs = evidence_fixture()
    report = evaluate_evidence(runs, coverage, **kwargs)
    assert report["schedule_status"] == "VERIFIED"
    assert report["scanner_gaps"][0]["gap_seconds"] == 30
    assert report["chain_recovery"]["scanner_recovery_seconds"] == 30


def test_state_only_checkout_changes_can_share_one_verified_code_version():
    runs, coverage, kwargs = evidence_fixture()
    coverage[1]["source_sha"] = "b" * 40
    report = evaluate_evidence(runs, coverage, **kwargs)
    assert report["schedule_status"] == "VERIFIED"
    assert report["checks"]["one_code_version_observed"]
    assert report["code_fingerprints"] == [FINGERPRINT]
    assert report["source_shas"] == ["a" * 40, "b" * 40]


def test_mixed_code_versions_cannot_verify_even_when_all_schedule_checks_pass():
    runs, coverage, kwargs = evidence_fixture()
    coverage[1]["source_sha"] = "b" * 40
    coverage[1]["code_fingerprint"] = FINGERPRINT_PREFIX + "d" * 64
    report = evaluate_evidence(runs, coverage, **kwargs)
    assert report["schedule_status"] == "NOT_VERIFIED"
    assert not report["checks"]["one_code_version_observed"]
    assert report["remaining_evidence"] == ["one_code_version_observed"]


@pytest.mark.parametrize("fingerprint", [None, "", "c" * 64,
                                        "unknown-v2:sha256:" + "c" * 64,
                                        FINGERPRINT_PREFIX + "not-a-digest"])
def test_missing_or_invalid_fingerprint_cannot_verify(fingerprint):
    runs, coverage, kwargs = evidence_fixture()
    coverage[1]["code_fingerprint"] = fingerprint
    report = evaluate_evidence(runs, coverage, **kwargs)
    assert report["schedule_status"] == "NOT_VERIFIED"
    assert not report["checks"]["coverage_records_valid"]
    assert "code fingerprint" in report["invalid_coverage_records"][0]["error"]


@pytest.mark.parametrize("explicit_close", [True, False])
def test_evidence_accepts_runner_bucket_keys_and_explicit_close_time(explicit_close):
    runs, coverage, kwargs = evidence_fixture()
    row = coverage[1]["rounds"][0]
    close = row["round_key"]
    row["round_key"] = str(int(datetime.fromisoformat(close).timestamp()) // 900)
    if explicit_close:
        row["close_time"] = close
    report = evaluate_evidence(runs, coverage, **kwargs)
    assert report["schedule_status"] == "VERIFIED"
    assert report["invalid_coverage_records"] == []
    assert report["expected_complete_final_windows"] == 1


def test_evidence_rejects_invalid_explicit_close_time_without_fallback():
    runs, coverage, kwargs = evidence_fixture()
    coverage[1]["rounds"][0]["close_time"] = "not-a-timestamp"
    report = evaluate_evidence(runs, coverage, **kwargs)
    assert report["schedule_status"] == "NOT_VERIFIED"
    assert not report["checks"]["coverage_records_valid"]


def test_green_schedule_events_without_actual_coverage_do_not_verify():
    runs, _, kwargs = evidence_fixture()
    report = evaluate_evidence(runs, [], **kwargs)
    assert report["schedule_status"] == "NOT_VERIFIED"
    assert report["checks"]["scheduled_events_observed"]
    assert not report["checks"]["actual_scan_intervals_available"]


def test_unrelated_cancelled_run_or_nearby_manual_scan_cannot_prove_recovery():
    runs, coverage, kwargs = evidence_fixture()
    old_runs = deepcopy(runs)
    old_runs[0]["updatedAt"] = "2026-10-28T18:10:00+00:00"
    assert not evaluate_evidence(old_runs, coverage, **kwargs)["checks"]["deliberate_chain_cancellation_recorded"]
    coverage[1]["run_id"] = "unrelated-manual-run"
    assert not evaluate_evidence(runs, coverage, **kwargs)["checks"]["scheduled_scanning_recovery_observed"]


def test_final_window_outside_run_interval_does_not_count():
    runs, coverage, kwargs = evidence_fixture()
    coverage[0]["rounds"] = coverage[1].pop("rounds")
    report = evaluate_evidence(runs, coverage, **kwargs)
    assert not report["checks"]["coverage_records_valid"]
    assert not report["checks"]["final_minute_coverage_recorded"]


def test_coverage_artifact_binds_to_its_workflow_provenance(tmp_path):
    _, coverage, _ = evidence_fixture()
    record = coverage[1]
    record.pop("run_id")
    (tmp_path / "coverage.json").write_text(json.dumps(record))
    provenance = {"run_id": "2", "source_sha": "a" * 40, "code_fingerprint": FINGERPRINT}
    (tmp_path / "workflow_provenance.json").write_text(json.dumps(provenance))
    assert read_coverage([str(tmp_path)])[0]["run_id"] == "2"
    provenance["source_sha"] = "b" * 40
    (tmp_path / "workflow_provenance.json").write_text(json.dumps(provenance))
    assert read_coverage([str(tmp_path)])[0]["provenance_conflict"]


def test_coverage_and_workflow_fingerprint_disagreement_cannot_verify(tmp_path):
    runs, coverage, kwargs = evidence_fixture()
    (tmp_path / "coverage.json").write_text(json.dumps(coverage[1]))
    (tmp_path / "workflow_provenance.json").write_text(json.dumps({
        "run_id": "2", "source_sha": "a" * 40,
        "code_fingerprint": FINGERPRINT_PREFIX + "d" * 64}))
    records = [coverage[0], *read_coverage([str(tmp_path)])]
    report = evaluate_evidence(runs, records, **kwargs)
    assert report["schedule_status"] == "NOT_VERIFIED"
    assert not report["checks"]["coverage_records_valid"]


def test_short_observation_missing_break_and_missed_window_stay_unverified():
    runs, coverage, _ = evidence_fixture()
    coverage[1]["rounds"][0]["final_window_scanned"] = False
    report = evaluate_evidence(runs, coverage)
    assert report["schedule_status"] == "NOT_VERIFIED"
    assert not report["checks"]["observation_span_sufficient"]
    assert not report["checks"]["deliberate_chain_cancellation_recorded"]
    assert not report["checks"]["final_minute_coverage_recorded"]


def test_overlapping_writers_or_long_gap_cannot_pass_coverage():
    runs, coverage, kwargs = evidence_fixture()
    overlap = deepcopy(coverage)
    overlap[1]["scan_started_at"] = coverage[0]["scan_started_at"]
    assert not evaluate_evidence(runs, overlap, **kwargs)["checks"]["successor_scan_gaps_within_limit"]
    gap = deepcopy(coverage)
    gap[0]["scan_ended_at"] = "2026-10-29T18:09:00+00:00"
    assert not evaluate_evidence(runs, gap, **kwargs)["checks"]["successor_scan_gaps_within_limit"]


def test_evidence_cli_returns_nonzero_when_required_observations_are_missing(tmp_path):
    runs, output = tmp_path / "runs.json", tmp_path / "result.json"
    runs.write_text("[]")
    assert evidence_main(["--runs", str(runs), "--output", str(output)]) == 2
    assert json.loads(output.read_text())["schedule_status"] == "NOT_VERIFIED"

