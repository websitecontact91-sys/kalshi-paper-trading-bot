import json
from pathlib import Path
import subprocess

import pytest

from persistence import persist, PersistenceError, RemoteStateChangedError, SourceChangedError
from state_store import load, new_state, record_entry, save


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    root = tmp_path / "working"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Paper test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "runner.py").write_text("# fixed tested source\n")
    save(root / "paper_state.json", new_state(session_id="test-session"))
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial fixture")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    return root, remote, git(root, "rev-parse", "HEAD")


def pending(root):
    state = load(root / "paper_state.json")
    record_entry(state, "1992550", {"ticker": "KXSOL15M-TEST", "side": "YES",
                                    "budget": "5.00", "debit": "5.00", "payout": "5.69"})
    return state


def remote_state(remote):
    return json.loads(git(remote, "show", "main:paper_state.json"))


def other_checkout(root, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", str(root.parent / "remote.git"), str(other))
    git(other, "config", "user.name", "Other test")
    git(other, "config", "user.email", "other@example.invalid")
    return other


def test_real_local_remote_persists_state_and_derived_ledger(repository):
    root, remote, source = repository
    receipt = persist(pending(root), root / "paper_state.json", push=True,
                      source_sha=source, sleep=lambda _: None)
    assert receipt["remote"] is True
    saved = remote_state(remote)
    assert saved["balance"] == "995.00"
    assert saved["rounds_traded"] == ["1992550"]
    assert "KXSOL15M-TEST" in git(remote, "show", "main:paper_ledger.csv")
    assert git(root, "diff", "--name-only", source, "HEAD") == "paper_ledger.csv\npaper_state.json"


def test_four_failed_pushes_raise_and_remote_remains_unchanged(repository):
    root, remote, source = repository
    pushes, waits = [], []

    def fail_push(args, **kwargs):
        if args[:2] == ["git", "push"]:
            pushes.append(args)
            return subprocess.CompletedProcess(args, 1, "", "simulated transport failure")
        return subprocess.run(args, **kwargs)

    with pytest.raises(PersistenceError, match="after 4 attempts"):
        persist(pending(root), root / "paper_state.json", push=True,
                source_sha=source, run=fail_push, sleep=waits.append)
    assert len(pushes) == 4
    assert waits == [1, 2, 4]
    assert remote_state(remote)["pending"] is None
    assert load(root / "paper_state.json")["pending"] is not None


def test_successful_push_with_lost_ack_is_not_double_applied(repository):
    root, remote, source = repository
    pushes = []

    def lose_ack(args, **kwargs):
        result = subprocess.run(args, **kwargs)
        if args[:2] == ["git", "push"]:
            pushes.append(args)
            return subprocess.CompletedProcess(args, 1, "", "acknowledgement lost")
        return result

    result = persist(pending(root), root / "paper_state.json", push=True,
                     source_sha=source, run=lose_ack, sleep=lambda _: None)
    assert result["remote"] is True
    assert len(pushes) == 1
    assert remote_state(remote)["balance"] == "995.00"
    assert len(remote_state(remote)["rounds_traded"]) == 1


def test_remote_code_change_blocks_state_push_before_rebase(repository, tmp_path):
    root, remote, source = repository
    state = pending(root)
    other = other_checkout(root, tmp_path)
    (other / "runner.py").write_text("# newer untested source\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "Concurrent code change")
    git(other, "push")
    with pytest.raises(SourceChangedError, match="after tests"):
        persist(state, root / "paper_state.json", push=True, source_sha=source)
    assert (root / "runner.py").read_text() == "# fixed tested source\n"
    assert remote_state(remote)["pending"] is None


def test_competing_state_writer_cannot_be_silently_overwritten(repository, tmp_path):
    root, remote, source = repository
    our_state = pending(root)
    other = other_checkout(root, tmp_path)
    other_state = load(other / "paper_state.json")
    record_entry(other_state, "1992551", {"ticker": "KXBTC15M-OTHER", "side": "NO",
                                         "debit": "5.00", "payout": "5.60"})
    save(other / "paper_state.json", other_state)
    git(other, "add", ".")
    git(other, "commit", "-m", "Concurrent state write")
    git(other, "push")
    with pytest.raises(RemoteStateChangedError, match="Another writer"):
        persist(our_state, root / "paper_state.json", push=True, source_sha=source)
    assert remote_state(remote)["pending"]["ticker"] == "KXBTC15M-OTHER"


def test_failed_rebase_is_aborted_and_never_pushed(repository):
    root, remote, source = repository
    commands = []

    def fail_pull(args, **kwargs):
        commands.append(args)
        if args[:2] == ["git", "pull"]:
            return subprocess.CompletedProcess(args, 1, "", "simulated conflict")
        return subprocess.run(args, **kwargs)

    with pytest.raises(PersistenceError, match="after 4 attempts"):
        persist(pending(root), root / "paper_state.json", push=True,
                source_sha=source, run=fail_pull, sleep=lambda _: None)
    assert commands.count(["git", "rebase", "--abort"]) == 4
    assert not any(command[:2] == ["git", "push"] for command in commands)
    assert remote_state(remote)["pending"] is None


def test_unrelated_staged_source_cannot_be_committed_with_state(repository):
    root, remote, source = repository
    (root / "runner.py").write_text("# unreviewed local change\n")
    git(root, "add", "runner.py")
    with pytest.raises(PersistenceError, match="Unrelated staged"):
        persist(pending(root), root / "paper_state.json", push=True, source_sha=source)
    assert remote_state(remote)["pending"] is None
