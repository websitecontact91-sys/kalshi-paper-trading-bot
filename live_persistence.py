"""Durable live state, isolated from paper records and the source branch.

GitHub executions use the dedicated ``live-state`` branch. No checkout, pull,
rebase, force push, paper-ledger edit, or source-file update is performed here.
The caller must hold ``locked()`` for its complete process lifetime.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import subprocess

from live_state import load, save, validate


class LivePersistenceError(RuntimeError):
    pass


class LiveStore:
    def __init__(self, path='live_runtime/state.json', *, push=False, branch='live-state',
                 cwd=None, run=subprocess.run):
        self.path = Path(path).resolve()
        if self.path.name in {'paper_state.json', 'paper_ledger.csv'}:
            raise LivePersistenceError('Live records must use their separate state path')
        if branch != 'live-state':
            raise LivePersistenceError('Live remote durability uses only the live-state branch')
        self.push, self.branch = bool(push), branch
        self.cwd, self.run = Path(cwd or Path.cwd()).resolve(), run
        self.expected_remote, self.loaded = None, False
        self._lock_held = False

    @contextmanager
    def locked(self):
        if self._lock_held:
            raise LivePersistenceError('Live-state lock is already held by this store')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + '.lock').open('a') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LivePersistenceError('Another live-state writer is already running') from None
            try:
                self._lock_held = True
                yield self
            finally:
                self._lock_held = False
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _require_lock(self):
        if not self._lock_held:
            raise LivePersistenceError('Hold the live-state lock for the complete runner lifetime')

    def _git(self, *args, input=None, check=True):
        try:
            result = self.run(['git', *args], cwd=self.cwd, input=input, text=True,
                              capture_output=True, check=False, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise LivePersistenceError('Live-state Git operation did not complete') from None
        if check and result.returncode:
            raise LivePersistenceError('Live-state Git operation failed')
        return result

    def _remote(self):
        ref = 'refs/heads/' + self.branch
        found = self._git('ls-remote', '--exit-code', 'origin', ref, check=False)
        if found.returncode == 2:
            return None
        if found.returncode:
            raise LivePersistenceError('Cannot verify the remote live-state branch')
        rows = found.stdout.strip().splitlines()
        if len(rows) != 1:
            raise LivePersistenceError('Ambiguous remote live-state reference')
        sha, observed_ref = rows[0].split()
        if not re.fullmatch('[0-9a-f]{40}', sha) or observed_ref != ref:
            raise LivePersistenceError('Invalid remote live-state reference')
        self._git('fetch', '--no-tags', 'origin', sha)
        return sha

    def _read_remote(self, sha):
        paths = self._git('ls-tree', '-r', '--name-only', sha).stdout.splitlines()
        if paths != ['live_state.json']:
            raise LivePersistenceError('Remote live-state branch must contain only its state file')
        raw = self._git('show', sha + ':live_state.json').stdout
        try:
            state = json.loads(raw)
            validate(state)
        except (ValueError, TypeError, KeyError):
            raise LivePersistenceError('Remote live state is invalid; refusing to reset it') from None
        return state

    def read(self):
        self._require_lock()
        if self.push:
            self.expected_remote = self._remote()
            self.loaded = True
            if self.expected_remote is None:
                raise FileNotFoundError('The live-state branch has not been explicitly initialized')
            state = self._read_remote(self.expected_remote)
            save(self.path, state)
            return state
        self.loaded = True
        return load(self.path)

    def initialize(self, state):
        self._require_lock()
        validate(state)
        if self.push:
            self.expected_remote = self._remote()
            if self.expected_remote is not None:
                raise LivePersistenceError('Live state already exists; initialization cannot reset it')
        elif self.path.exists():
            raise LivePersistenceError('Live state already exists; initialization cannot reset it')
        self.loaded = True
        return self.persist(state)

    def persist(self, state):
        self._require_lock()
        if not self.loaded:
            raise LivePersistenceError('Read or explicitly initialize the live store before saving')
        validate(state)
        receipt = save(self.path, state)
        if not self.push:
            return {**(receipt or {}), 'durable': True, 'remote': False}
        remote = self._remote()
        if remote != self.expected_remote:
            raise LivePersistenceError('Another writer changed live state; refusing an unconfirmed fork')
        payload = json.dumps(state, sort_keys=True, indent=2) + '\n'
        blob = self._git('hash-object', '-w', '--stdin', input=payload).stdout.strip()
        tree = self._git('mktree', input='100644 blob ' + blob + '\tlive_state.json\n').stdout.strip()
        parent = ['-p', remote] if remote else []
        candidate = self._git('-c', 'user.name=Kalshi Live State', '-c',
            'user.email=live-state@users.noreply.github.com', 'commit-tree', tree,
            *parent, '-m', 'Persist live order intent and reconciliation state').stdout.strip()
        # A non-force push whose parent is the observed remote head is a lease:
        # another writer's advance rejects this commit instead of overwriting it.
        try:
            pushed = self._git('push', 'origin', candidate + ':refs/heads/' + self.branch, check=False)
        except LivePersistenceError:
            pushed = None
        observed = self._remote()
        if observed != candidate:
            raise LivePersistenceError('Live state was not confirmed on GitHub; order submission must stop')
        if self._read_remote(observed) != state:
            raise LivePersistenceError('Confirmed live state does not match the intended transition')
        self.expected_remote = candidate
        return {'durable': True, 'remote': True, 'state_commit': candidate,
                'state_path': str(self.path), 'branch': self.branch,
                'acknowledgement_recovered': pushed is None or pushed.returncode != 0}
