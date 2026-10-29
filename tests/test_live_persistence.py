"""Use real temporary Git repositories to test live write-ahead durability."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from live_persistence import LiveStore, LivePersistenceError
from live_state import new_state, validate


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


@pytest.fixture
def repositories(tmp_path):
    remote = tmp_path / 'remote.git'
    work = tmp_path / 'checkout'
    remote.mkdir()
    work.mkdir()
    git(remote, 'init', '--bare', '-q')
    git(work, 'init', '-q', '-b', 'main')
    git(work, 'remote', 'add', 'origin', str(remote))
    (work / 'paper_state.json').write_text('{"preserve":"paper history"}\n')
    git(work, 'add', '.')
    git(work, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'Existing source and paper ledger')
    git(work, 'push', '-q', 'origin', 'main')
    return remote, work


def state():
    return new_state(session_id='test-session', account_scope='production:0:abc123')


def test_local_write_requires_lock_and_explicit_initialization(tmp_path):
    path = tmp_path / 'live.json'
    store = LiveStore(path)
    with pytest.raises(LivePersistenceError, match='lock'):
        store.initialize(state())
    with store.locked():
        with pytest.raises(LivePersistenceError, match='Read or explicitly initialize'):
            store.persist(state())
        receipt = store.initialize(state())
        assert receipt['durable'] and not receipt['remote']
        assert store.read() == state()
        with pytest.raises(LivePersistenceError, match='already exists'):
            store.initialize(state())
    assert json.loads(path.read_text()) == state()


def test_second_writer_cannot_hold_same_local_state_lock(tmp_path):
    a, b = LiveStore(tmp_path / 'live.json'), LiveStore(tmp_path / 'live.json')
    with a.locked():
        with pytest.raises(LivePersistenceError, match='Another live-state writer'):
            with b.locked():
                pass


def test_remote_live_branch_is_separate_and_restart_reads_authoritative_state(repositories):
    remote, work = repositories
    before = git(remote, 'rev-parse', 'refs/heads/main')
    store = LiveStore(work / 'runtime' / 'state.json', push=True, cwd=work)
    with store.locked():
        first = store.initialize(state())
        assert first['durable'] and first['remote']
        updated = deepcopy(state())
        updated['account_cash'] = '19.8499'
        second = store.persist(updated)
    assert second['state_commit'] != first['state_commit']
    assert git(remote, 'rev-parse', 'refs/heads/main') == before
    assert git(remote, 'ls-tree', '-r', '--name-only', 'live-state') == 'live_state.json'
    restarted = LiveStore(work / 'new-runtime' / 'state.json', push=True, cwd=work)
    with restarted.locked():
        assert restarted.read() == updated
        with pytest.raises(LivePersistenceError, match='already exists'):
            restarted.initialize(state())


def test_conflicting_remote_writer_is_rejected_without_overwrite(repositories):
    remote, work = repositories
    a = LiveStore(work / 'a.json', push=True, cwd=work)
    b = LiveStore(work / 'b.json', push=True, cwd=work)
    with a.locked(), b.locked():
        a.initialize(state())
        b.read()
        changed = {**state(), 'account_cash': '10.00'}
        a.persist(changed)
        with pytest.raises(LivePersistenceError, match='Another writer'):
            b.persist({**state(), 'account_cash': '11.00'})
    assert json.loads(git(remote, 'show', 'live-state:live_state.json')) == changed


def test_lost_push_ack_is_confirmed_by_exact_remote_state(repositories):
    remote, work = repositories
    def lost_ack(args, **kwargs):
        result = subprocess.run(args, **kwargs)
        if len(args) > 1 and args[1] == 'push' and result.returncode == 0:
            return subprocess.CompletedProcess(args, 1, '', 'connection interrupted after server accepted')
        return result
    store = LiveStore(work / 'live.json', push=True, cwd=work, run=lost_ack)
    with store.locked():
        receipt = store.initialize(state())
    assert receipt['remote'] and receipt['acknowledgement_recovered']
    assert json.loads(git(remote, 'show', 'live-state:live_state.json')) == state()


def test_unconfirmed_push_never_returns_durable_remote_receipt(repositories):
    remote, work = repositories
    def refuse_push(args, **kwargs):
        if len(args) > 1 and args[1] == 'push':
            return subprocess.CompletedProcess(args, 1, '', 'rejected')
        return subprocess.run(args, **kwargs)
    store = LiveStore(work / 'live.json', push=True, cwd=work, run=refuse_push)
    with store.locked():
        with pytest.raises(LivePersistenceError, match='not confirmed'):
            store.initialize(state())
    assert not git(remote, 'branch', '--list', 'live-state')
    validate(json.loads((work / 'live.json').read_text()))


def test_missing_remote_is_not_silently_initialized(repositories):
    _, work = repositories
    store = LiveStore(work / 'live.json', push=True, cwd=work)
    with store.locked():
        with pytest.raises(FileNotFoundError):
            store.read()
    assert not (work / 'live.json').exists()


def test_paper_path_and_source_branch_are_rejected(tmp_path):
    with pytest.raises(LivePersistenceError):
        LiveStore(tmp_path / 'paper_state.json')
    with pytest.raises(LivePersistenceError):
        LiveStore(tmp_path / 'live.json', branch='main')
