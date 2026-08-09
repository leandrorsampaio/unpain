"""One recoverable unit of work, for the operations that write more than one file.

Every individual write in this application already publishes atomically — a unique
temporary name, fsync, rename — so no single document is ever left half written. That
was used as the argument that a mutation framework was unnecessary, and it is the wrong
argument, because the failures that actually happen here span several files:

  closing a month writes the lock, then the recorded figures, then a checkpoint;
  an import writes transactions, balance anchors, transfer marks and the upload log;
  closing a year does twelve of the first one, plus the annual settlement.

Each of those steps is atomic and the sequence is not. Interrupt it and the store is
internally inconsistent in a way no individual file can reveal: a month marked closed
with no baseline recorded is a month whose drift detection silently does nothing.

So this is deliberately not a general transaction system — the plan's full version was
declined, and its startup recovery, which has to *guess* which half-written state to roll
back, is a worse risk than the one it removes. This is the scoped version: name the paths
an operation will touch, and they are copied aside first. The body either finishes or the
paths go back exactly as they were. The journal is written before anything is touched and
outlives the process, so a crash is recovered on the next start rather than discovered
months later by the doctor.

What it deliberately does not do: nest, span processes, or roll back anything it was not
told about. A bundle that quietly protected more than it named would be a backup with a
misleading name.
"""
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .util import ROOT

JOURNAL_NAME = ".bundle-journal.json"
STAGING_NAME = ".bundle-staging"


class BundleRecoveryError(RuntimeError):
    """A previous mutation cannot be recovered safely, so no new one may start."""


def _snapshot_target(staging, index):
    return staging / ("%03d" % index)


def _write_journal(root, state):
    """Publish the journal the same way every other document is published."""
    path = Path(root) / JOURNAL_NAME
    tmp = path.with_suffix(".%d.tmp" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        # Never copy the bundle's own staging into the snapshot it is building.
        shutil.copytree(source, destination,
                        ignore=shutil.ignore_patterns(STAGING_NAME, JOURNAL_NAME, ".*"))
    else:
        shutil.copy2(source, destination)


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def restore(root, state):
    """Put every path in the journal back the way it was found. Idempotent."""
    root = Path(root)
    staging = root / STAGING_NAME
    for entry in state.get("entries") or []:
        target = Path(entry["path"])
        snapshot = staging / entry["snapshot"]
        _remove(target)
        if entry["existed"]:
            if snapshot.exists():
                _copy(snapshot, target)
        # A path that did not exist before the bundle must not exist after a rollback
        # either: "restore" means the state it was found in, not the nearest thing to it.
    _remove(staging)
    (root / JOURNAL_NAME).unlink(missing_ok=True)


def recover(root=None):
    """Roll back a bundle the process did not survive. Safe to call at any time."""
    root = Path(root or ROOT)
    path = root / JOURNAL_NAME
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # A journal torn by the same crash it was recording cannot be trusted to say
        # what to restore, and guessing is exactly what this design refuses to do.
        # Leave the snapshots in place for a human and say so.
        return {"unreadable": True, "staging": str(root / STAGING_NAME)}
    if not isinstance(state, dict) or state.get("stage") == "done":
        path.unlink(missing_ok=True)
        return None
    restore(root, state)
    return {"rolled_back": [entry["path"] for entry in state.get("entries") or []],
            "name": state.get("name"), "started_at": state.get("started_at")}


@contextmanager
def bundle(name, paths, *, root=None):
    """Run a multi-file operation so that it either lands whole or not at all.

    `paths` is everything the body may write. Anything it writes that is not named here
    is not protected — which is why the name of the operation is recorded too, so a
    rollback can be explained rather than merely performed.
    """
    root = Path(root or ROOT)
    # Recovery is a property of the mutation boundary, not of whichever entry point
    # happened to start the application.  CLI ingestion does not run FastAPI's startup
    # hook; without this check a second CLI run deleted the first run's snapshots below
    # and overwrote its journal, permanently adopting the half-written state.
    recovered = recover(root)
    if recovered and recovered.get("unreadable"):
        raise BundleRecoveryError(
            "an earlier mutation has an unreadable recovery journal; refusing to overwrite "
            "its snapshots at %s" % recovered.get("staging"))
    staging = root / STAGING_NAME
    _remove(staging)
    staging.mkdir(parents=True, exist_ok=True)

    entries = []
    for index, item in enumerate(paths):
        target = Path(item)
        if not target.is_absolute():
            target = root / target
        entry = {"path": str(target), "snapshot": "%03d" % index, "existed": target.exists()}
        if entry["existed"]:
            _copy(target, _snapshot_target(staging, index))
        entries.append(entry)

    state = {"stage": "running", "name": name, "entries": entries,
             "started_at": datetime.now(timezone.utc).isoformat()}
    # Journalled before the body runs, because the window that matters begins with the
    # body's first write and a journal published after it explains nothing.
    _write_journal(root, state)
    try:
        yield state
    except BaseException:
        restore(root, state)
        raise
    state["stage"] = "done"
    _write_journal(root, state)
    _remove(staging)
    (root / JOURNAL_NAME).unlink(missing_ok=True)
