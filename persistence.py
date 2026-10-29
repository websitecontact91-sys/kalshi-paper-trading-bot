"""Atomic local paper state and explicit, conflict-aware Git durability."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
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

    def git(*args, cwd=path.parent, env=None):
        options = {"env": env} if env is not None else {}
        return run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False, **options)

    def required(*args, env=None):
        response = git(*args, env=env)
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
        return all(name in relative or (name.startswith(("ledger_archive/", "paper_sessions/"))
                                       and name.endswith(".json"))
                   for name in changes if name)

    def receipt(commit, *, restart_required=False, remote_source_sha=None):
        result = {"state_path": str(path), "ledger_path": str(ledger), "remote": True,
                  "source_sha": tested_source, "state_commit": commit,
                  "restart_required": restart_required}
        if remote_source_sha is not None:
            result["remote_source_sha"] = remote_source_sha
        return result

    def is_ancestor(older, newer):
        result = git("merge-base", "--is-ancestor", older, newer)
        if result.returncode not in (0, 1):
            raise PersistenceError("Cannot establish remote state ancestry")
        return result.returncode == 0

    def handoff_commit(remote_head, local_head):
        """Overlay only saved runtime files on new source without checking it out.

        The currently executing Python process keeps its tested source tree.
        A separate Git index preserves every other remote file byte for byte.
        The caller must stop after this transition has been durably published.
        """
        with tempfile.TemporaryDirectory(prefix="paper-state-index-") as directory:
            environment = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
            required("read-tree", remote_head, env=environment)
            for name in relative:
                tracked = required("ls-tree", local_head, "--", name)
                metadata, tracked_name = tracked.split("\t", 1)
                mode, kind, blob = metadata.split()
                if tracked_name != name or mode != "100644" or kind != "blob":
                    raise PersistenceError("Runtime persistence requires regular data files")
                required("update-index", "--add", "--cacheinfo", f"{mode},{blob},{name}",
                         env=environment)
            tree = required("write-tree", env=environment)
        candidate = required("-c", "user.name=Kalshi Paper Bot", "-c",
            "user.email=paper-bot@users.noreply.github.com", "commit-tree", tree,
            "-p", remote_head, "-m", "Persist simulated paper state before source handoff")
        changes = required("diff", "--name-only", remote_head, candidate).splitlines()
        if set(changes) - set(relative):
            raise PersistenceError("Source handoff attempted to change unrelated files")
        return candidate

    last_error = None
    attempted_commit = None
    attempted_handoff = False
    attempted_remote_source = None
    for attempt in range(attempts):
        try:
            required("fetch", "origin", "main")
            remote_head = required("rev-parse", "refs/remotes/origin/main")
            local_head = required("rev-parse", "HEAD")
            changed_source = required("diff", "--name-only", tested_source, remote_head).splitlines()
            source_changed = not runtime_only(changed_source)

            # A push can reach GitHub even if its acknowledgement is lost. A
            # subsequent source-only deployment must not double-apply the state.
            if attempted_commit and is_ancestor(attempted_commit, remote_head):
                later_state = required("diff", "--name-only", attempted_commit, remote_head,
                                       "--", *relative).splitlines()
                if later_state:
                    raise RemoteStateChangedError("Another writer changed paper state after our push")
                return receipt(attempted_commit,
                               restart_required=attempted_handoff or source_changed,
                               remote_source_sha=(remote_head if source_changed
                                                  else attempted_remote_source))
            if remote_head == local_head:
                return receipt(local_head)

            if not is_ancestor(remote_head, base_head):
                state_changes = required("diff", "--name-only", base_head, remote_head,
                                         "--", *relative).splitlines()
                if state_changes:
                    raise RemoteStateChangedError("Another writer changed paper state; refusing a silent fork")

            if source_changed:
                # Save the already computed entry/settlement/audit on top of the
                # newer code, then let the next serialized job test that code.
                # This is a state-only Git operation, never a code hot reload.
                candidate = handoff_commit(remote_head, local_head)
            else:
                # Use the exact fetched revision. `pull` would fetch a second
                # time and could replace running code after the source check.
                rebased = git("rebase", remote_head)
                if rebased.returncode:
                    git("rebase", "--abort")
                    raise PersistenceError("State rebase failed and was aborted")
                changed_after_rebase = required("diff", "--name-only", tested_source, "HEAD").splitlines()
                if not runtime_only(changed_after_rebase):
                    raise SourceChangedError("Source changed locally during persistence")
                candidate = required("rev-parse", "HEAD")
            if path.read_bytes() != expected_state:
                raise RemoteStateChangedError("Persisted state changed during rebase")
            attempted_commit = candidate
            attempted_handoff = source_changed
            attempted_remote_source = remote_head if source_changed else None
            pushed = git("push", "origin", candidate + ":refs/heads/main")
            if pushed.returncode:
                raise PersistenceError("State push failed; local write is not confirmed remote")
            return receipt(candidate, restart_required=source_changed,
                           remote_source_sha=attempted_remote_source)
        except (SourceChangedError, RemoteStateChangedError):
            raise
        except (PersistenceError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleep(min(2 ** attempt, 8))
    raise PersistenceError(f"Remote paper state was not saved after {attempts} attempts") from last_error
