"""Atomic local paper state and explicit, conflict-aware Git durability."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

from state_store import save, validate, write_ledger


class PersistenceError(RuntimeError):
    pass


class SourceChangedError(PersistenceError):
    pass


class RemoteStateChangedError(PersistenceError):
    pass


def persist(state, path="paper_state.json", *, ledger_path=None, push=False,
            source_sha=None, extra_paths=(), run=subprocess.run, sleep=time.sleep,
            attempts=4):
    """Persist before reporting a paper entry; any failed remote save raises.

    JSON is authoritative; CSV can always be reconstructed after a local crash.
    The runner must stop after an exception and must not announce the new state
    until this function returns. Tests inject a subprocess runner and sleep.
    """
    validate(state)
    path = Path(path).resolve()
    ledger = Path(ledger_path).resolve() if ledger_path else path.with_name("paper_ledger.csv")
    save(path, state)
    write_ledger(ledger, state)
    if not push:
        return {"state_path": str(path), "ledger_path": str(ledger), "remote": False,
                "source_sha": source_sha}
    if not 1 <= attempts <= 4:
        raise ValueError("Persistence allows one to four attempts")

    def git(*args, cwd=path.parent):
        return run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)

    def required(*args):
        response = git(*args)
        if response.returncode:
            raise PersistenceError(f"Git {' '.join(args[:2])} failed ({response.returncode})")
        return response.stdout.strip()

    root = Path(required("rev-parse", "--show-toplevel")).resolve()
    targets = [path, ledger] + [Path(item).resolve() for item in extra_paths]
    try:
        relative = [item.relative_to(root).as_posix() for item in targets]
    except ValueError as exc:
        raise PersistenceError("Persistence paths must be inside the repository") from exc
    expected_state = path.read_bytes()
    base_head = required("rev-parse", "HEAD")
    tested_source = source_sha or base_head
    staged = set(required("diff", "--cached", "--name-only").splitlines())
    if staged - set(relative):
        raise PersistenceError("Unrelated staged changes; refusing mixed source/state commit")
    required("add", "--", *relative)
    dirty = git("diff", "--cached", "--quiet")
    if dirty.returncode == 1:
        required("-c", "user.name=Kalshi Paper Bot", "-c", "user.email=paper-bot@users.noreply.github.com",
                 "commit", "-m", "Persist simulated paper state and ledger")
    elif dirty.returncode != 0:
        raise PersistenceError("Cannot inspect staged paper state")

    def runtime_only(changes):
        return all(name in relative or name.startswith(("ledger_archive/", "paper_sessions/"))
                   for name in changes if name)

    last_error = None
    for attempt in range(attempts):
        try:
            required("fetch", "origin", "main")
            remote_head = required("rev-parse", "refs/remotes/origin/main")
            local_head = required("rev-parse", "HEAD")
            changed_source = required("diff", "--name-only", tested_source, remote_head).splitlines()
            if not runtime_only(changed_source):
                raise SourceChangedError("Remote source changed after tests; restart and retest before scanning")
            if remote_head == local_head:
                # A prior push may have reached GitHub before its acknowledgement
                # was lost. The identical commit is already durable.
                return {"state_path": str(path), "ledger_path": str(ledger), "remote": True,
                        "source_sha": tested_source, "state_commit": local_head}
            ancestor = git("merge-base", "--is-ancestor", remote_head, base_head)
            if ancestor.returncode not in (0, 1):
                raise PersistenceError("Cannot establish remote state ancestry")
            if ancestor.returncode == 1:
                state_changes = required("diff", "--name-only", base_head, remote_head, "--", *relative).splitlines()
                if state_changes:
                    raise RemoteStateChangedError("Another writer changed paper state; refusing a silent fork")
            pulled = git("pull", "--rebase", "origin", "main")
            if pulled.returncode:
                git("rebase", "--abort")
                raise PersistenceError("State rebase failed and was aborted")
            changed_after_pull = required("diff", "--name-only", tested_source, "HEAD").splitlines()
            if not runtime_only(changed_after_pull):
                raise SourceChangedError("Source changed during persistence; aborting this run")
            if path.read_bytes() != expected_state:
                raise RemoteStateChangedError("Persisted state changed during rebase")
            pushed = git("push", "origin", "HEAD:main")
            if pushed.returncode:
                raise PersistenceError("State push failed; local write is not confirmed remote")
            return {"state_path": str(path), "ledger_path": str(ledger), "remote": True,
                    "source_sha": tested_source, "state_commit": required("rev-parse", "HEAD")}
        except (SourceChangedError, RemoteStateChangedError):
            raise
        except (PersistenceError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleep(min(2 ** attempt, 8))
    raise PersistenceError(f"Remote paper state was not saved after {attempts} attempts") from last_error
