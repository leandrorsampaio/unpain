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
import shutil
import zipfile
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


def swap_in(root, staging, tops):
    """Move the finished areas into place, keeping the displaced ones until the end.

    Not a single atomic step — no filesystem offers that across several paths — but the
    live area is only ever moved aside, never deleted, until every area has landed. A
    failure part-way through can therefore be undone, which the previous delete-then-copy
    could not offer at any point after the first rmtree.
    """
    displaced = []
    try:
        for top in sorted(tops):
            target = root / top
            incoming = staging / top
            if not incoming.exists():
                continue
            if target.exists():
                aside = staging / ("__displaced__" + top)
                target.rename(aside)
                displaced.append((target, aside))
            incoming.rename(target)
        return [top for top in sorted(tops) if (root / top).exists()]
    except BaseException:
        for target, aside in reversed(displaced):
            if not target.exists() and aside.exists():
                aside.rename(target)
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
