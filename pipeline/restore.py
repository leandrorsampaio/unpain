"""Restoring a backup, without destroying the thing you are restoring over.

The old order of operations was: write a safety backup, delete the live folders, then
copy the archive in file by file. Everything after that delete is unprotected. A
subtly corrupt archive replaced good data with bad; a crash halfway left neither. The
safety backup was the only thing between the household and that, and recovering from it
was a manual operation nobody had rehearsed.

The order here is the opposite one. Nothing live is touched until a *complete* candidate
tree exists on disk and has passed every schema and cross-reference check the app has.
Then, and only then, the finished directories are swapped in.

"Replace" means the candidate does not carry the old contents of a selected area unless
the archive supplies them. It does not mean "delete the live area first" — that reading
is what made the operation destructive before it had decided whether to proceed.

An uploaded zip is also the one file in this application that arrives from outside it, so
it is treated accordingly: traversal, absolute paths, symlinks, case-collisions, CRC
failures and decompression bombs are all refused before a single entry is extracted.
"""
import io
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import schemas
from .util import read_json

# An archive is untrusted input. These are deliberately generous for a household's real
# backup and still small enough that a malicious or corrupt one cannot exhaust the disk.
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3          # 2 GiB compressed
MAX_TOTAL_UNCOMPRESSED = 8 * 1024 ** 3     # 8 GiB expanded
MAX_ENTRIES = 200_000
MAX_ENTRY_BYTES = 2 * 1024 ** 3
MAX_COMPRESSION_RATIO = 200                # a 200x entry is a bomb, not a statement

# Unix mode bits live in the top 16 of external_attr. Anything that is not a regular
# file or a directory — a symlink, device node or FIFO — has no business in a data
# backup and would extract as something the rest of the app cannot reason about.
S_IFMT, S_IFREG, S_IFDIR, S_IFLNK = 0o170000, 0o100000, 0o040000, 0o120000

# The swap's crash-recovery state. Both live under the root rather than under the
# staging directory, which the caller deletes on the way out: the only copy of the
# displaced data must not sit inside the directory that is about to be removed.
JOURNAL_NAME = ".restore-journal.json"
DISPLACED_DIR = ".restore-displaced"


class RestoreRejected(ValueError):
    """The archive or the state it would produce is not acceptable. Nothing was touched."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _reject(code, message):
    raise RestoreRejected(code, message)


def inspect_archive(raw, allowed_tops):
    """Everything that must be true before a single byte is extracted.

    Each check is here because the alternative is discovering the problem with the live
    tree already deleted.
    """
    if len(raw) > MAX_ARCHIVE_BYTES:
        _reject("archive-too-large", "The archive is larger than %d GiB."
                % (MAX_ARCHIVE_BYTES // 1024 ** 3))
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        _reject("not-a-zip", "The uploaded file is not a valid zip archive.")

    # CRC every member, so a truncated or bit-rotted upload is caught here rather than
    # halfway through extraction. testzip() *returns* the first bad name for a checksum
    # mismatch but *raises* when the compressed stream itself is unreadable, and both are
    # the same answer to the only question being asked: this archive cannot be trusted.
    try:
        broken = archive.testzip()
    except Exception as exc:            # noqa: BLE001 - zlib.error, BadZipFile, EOFError…
        _reject("corrupt-entry", "The archive is damaged and cannot be read: %s" % exc)
    if broken is not None:
        _reject("corrupt-entry", "The archive is damaged: %s failed its checksum." % broken)

    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        _reject("too-many-entries", "The archive contains more than %d entries." % MAX_ENTRIES)

    selected, total_uncompressed, seen = [], 0, {}
    for info in infos:
        name = info.filename
        mode = (info.external_attr >> 16) & S_IFMT
        if mode not in (0, S_IFREG, S_IFDIR):
            kind = "symlink" if mode == S_IFLNK else "special file"
            _reject("unsafe-entry", "The archive contains a %s (%s), which a data backup "
                                    "never needs." % (kind, name))
        if info.is_dir() or name.endswith("/"):
            continue

        # Normalize separators before judging the path: a Windows-authored archive can
        # carry backslashes, and "..\\..\\x" is the same escape as "../../x".
        normalized = name.replace("\\", "/")
        parts = PurePosixPathParts(normalized)
        if not parts or any(part in ("..", "") for part in parts) or normalized.startswith("/") \
                or (len(normalized) > 1 and normalized[1] == ":"):
            _reject("unsafe-path", "Unsafe path in archive: %s" % name)

        # Two entries that differ only in case collide on a case-insensitive filesystem,
        # so which one wins would depend on the machine doing the restore.
        folded = normalized.casefold()
        if folded in seen and seen[folded] != normalized:
            _reject("duplicate-path", "The archive holds two entries whose paths collide: "
                                      "%s and %s." % (seen[folded], name))
        if folded in seen:
            _reject("duplicate-path", "The archive holds %s twice." % name)
        seen[folded] = normalized

        if info.file_size > MAX_ENTRY_BYTES:
            _reject("entry-too-large", "%s is larger than this restore allows." % name)
        if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
            _reject("compression-bomb", "%s expands more than %dx and is refused."
                    % (name, MAX_COMPRESSION_RATIO))
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
            _reject("archive-too-large", "The archive expands to more than %d GiB."
                    % (MAX_TOTAL_UNCOMPRESSED // 1024 ** 3))

        if parts[0] in allowed_tops:
            selected.append(normalized)

    return archive, selected


def PurePosixPathParts(normalized):
    """Path components, with the quirks a zip can carry stripped out."""
    return [part for part in normalized.split("/") if part not in (".",)]


def build_candidate(root, staging, archive, members, *, mode, selected_tops):
    """Assemble the complete tree the restore would produce, beside the live one.

    Unselected areas are copied from the current state, so the candidate is the whole
    application and can be validated as one. Inside a selected area, `replace` starts
    empty and `merge` starts from what is there now — which is the entire difference
    between the two modes, expressed as what the candidate *contains* rather than as
    something done to live data.
    """
    staging.mkdir(parents=True, exist_ok=True)
    for entry in sorted(root.iterdir()):
        if entry.name in ("backups", ".git") or entry.name.startswith("."):
            continue
        if entry.name in selected_tops and mode == "replace":
            continue                       # the archive supplies this area, or it is absent
        destination = staging / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True, symlinks=False)
        elif entry.is_file():
            shutil.copy2(entry, destination)

    for name in members:
        destination = staging / name
        # Re-check containment against the staging root itself. The path was validated
        # above, but the check that matters is the one made against the directory being
        # written to, not the one the name was inspected in.
        resolved = destination.resolve()
        if staging.resolve() != resolved and staging.resolve() not in resolved.parents:
            _reject("unsafe-path", "Archive entry escapes the restore folder: %s" % name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, open(destination, "wb") as out:
            shutil.copyfileobj(source, out)
    return staging


def validate_candidate(staging):
    """The candidate must be a working application, not merely well-formed files."""
    config = staging / "config.json"
    if config.exists():
        try:
            document = read_json(config)
        except ValueError:
            _reject("bad-config", "config.json in the archive is not valid JSON.")
        people = document.get("people")
        if not (isinstance(people, list) and len(people) == 2):
            _reject("bad-config", "config.json in the archive has no valid 'people' list; "
                                  "restoring it would leave the app unusable.")

    report = schemas.validate_graph(staging)
    if not report["ok"]:
        # Report the path inside the archive, not the temporary directory it was staged
        # in. "/var/folders/T/xy9/.restore-candidate/data/…" tells the reader nothing they
        # can act on and exposes where the machine happens to keep its temp files.
        prefix = str(staging.resolve()) + "/"
        first = report["findings"][0]["message"].replace(prefix, "").replace(str(staging) + "/", "")
        _reject("invalid-state", "The archive would produce an invalid store — %d problem(s), "
                                 "starting with: %s" % (len(report["findings"]), first))
    return report


def _remove(path):
    """Delete a file or a directory, whichever it turns out to be."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def _write_journal(root, state):
    """Record the swap's progress somewhere that outlives both staging and the process.

    Written with fsync before the rename it describes, because a journal that reaches the
    disk after the operation it is meant to explain is not a journal.
    """
    path = root / JOURNAL_NAME
    tmp = path.with_suffix(".%d.tmp" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def roll_back(root, state):
    """Put the live tree back the way the journal says it was found.

    Order matters and is the reverse of installation: an installed area is removed
    *before* its displaced original is moved back, or the rename would land on an
    occupied name. Idempotent by construction — every step checks the state it is about
    to create — so running it twice, or after a crash mid-rollback, converges.
    """
    root = Path(root)
    displaced = state.get("displaced") or {}
    for top in reversed(state.get("installed") or []):
        # Everything in `installed` was put there by this swap, so all of it comes back
        # out — including an area that did not exist before, which must not survive a
        # rollback just because there was nothing to displace.
        _remove(root / top)
    for top, aside in sorted(displaced.items()):
        target, saved = root / top, Path(aside)
        if saved.exists():
            _remove(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            saved.rename(target)
    holding = root / DISPLACED_DIR
    if holding.exists() and not any(holding.iterdir()):
        holding.rmdir()
    (root / JOURNAL_NAME).unlink(missing_ok=True)


def recover(root):
    """Finish or undo a swap that a crash interrupted. Safe to call at any time.

    A restore that dies between two renames leaves the tree half old and half new, and
    nothing on disk to say so — which is the failure the previous version could not even
    detect. The journal makes it detectable, and this makes it recoverable: the tree ends
    up entirely as it was before the swap began.
    """
    root = Path(root)
    path = root / JOURNAL_NAME
    if not path.exists():                # `read_json(default=None)` raises rather than
        return None                      # returning the default, and no journal is normal
    try:
        state = read_json(path, default={})
    except ValueError:                   # a journal torn by the same crash it records
        state = {}
    if not isinstance(state, dict) or state.get("stage") == "done":
        (root / JOURNAL_NAME).unlink(missing_ok=True)
        return None
    roll_back(root, state)
    return {"rolled_back": sorted(state.get("displaced") or {}),
            "started_at": state.get("started_at")}


def swap_in(root, staging, tops):
    """Move the finished areas into place, keeping the displaced ones until the end.

    Not a single atomic step — no filesystem offers that across several paths — but the
    live area is only ever moved aside, never deleted, until every area has landed, and a
    journal on disk records each rename before it happens. A failure part-way through is
    therefore undone completely, whether the failure was an exception (rolled back here)
    or the process dying (rolled back by `recover` on the next run).

    The displaced originals are held under the *root*, not under staging: staging is
    deleted by the caller's `finally`, and putting the only copy of the old data inside
    the directory that is about to be deleted is how a rollback destroys what it was
    protecting.
    """
    root, staging = Path(root), Path(staging)
    holding = root / DISPLACED_DIR
    _remove(holding)
    holding.mkdir(parents=True, exist_ok=True)
    state = {"stage": "swapping", "started_at": datetime.now(timezone.utc).isoformat(),
             "displaced": {}, "installed": []}
    _write_journal(root, state)
    try:
        for top in sorted(tops):
            target, incoming = root / top, staging / top
            if not incoming.exists():
                continue
            if target.exists():
                aside = holding / top
                # Journal the intent before the rename, not after: the window that
                # matters is the one where the old copy is no longer where it was.
                state["displaced"][top] = str(aside)
                _write_journal(root, state)
                target.rename(aside)
            state["installed"].append(top)
            _write_journal(root, state)
            incoming.rename(target)
        state["stage"] = "done"
        _write_journal(root, state)
        applied = [top for top in sorted(tops) if (root / top).exists()]
        _remove(holding)
        (root / JOURNAL_NAME).unlink(missing_ok=True)
        return applied
    except BaseException:
        roll_back(root, state)
        raise


def restore_archive(raw, *, root, mode, selected_parts, backup_parts, staging_parent=None,
                    safety_backup=None):
    """Validate an archive completely, then swap it in. Returns the plan that was applied.

    `safety_backup` is a callable so the caller keeps ownership of how a backup is made;
    it is invoked only once the candidate has passed, because a safety copy taken before
    validation is a safety copy of a restore that was never going to happen.
    """
    if mode not in ("replace", "merge"):
        _reject("bad-mode", "mode must be 'replace' or 'merge'")
    root = Path(root).resolve()
    # A previous attempt that died mid-swap left the tree half old and half new. Undo it
    # before judging anything, or the candidate is validated against a state nobody chose.
    recover(root)
    allowed_tops = {entry.split("/")[0] for part in selected_parts
                    for entry in backup_parts[part]}

    archive, members = inspect_archive(raw, allowed_tops)
    if not members:
        _reject("nothing-to-restore", "The archive has nothing to restore for the selected parts.")

    present_tops = {member.split("/")[0] for member in members}
    staging = Path(staging_parent or root) / (".restore-candidate")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        build_candidate(root, staging, archive, members, mode=mode, selected_tops=present_tops)
        report = validate_candidate(staging)
        safety = safety_backup() if safety_backup else None
        applied = swap_in(root, staging, present_tops)
        return {"ok": True, "restored": len(members), "mode": mode,
                "parts": sorted(present_tops), "applied": applied,
                "validated": len(report["findings"]) == 0,
                "safety_backup": safety.name if safety else None}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
