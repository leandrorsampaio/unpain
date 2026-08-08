"""Cross-process serialization for operations that mutate financial state.

The web app and `pipeline.cli` can write the same store at the same moment, and the
in-process lock in app/server.py cannot see the other process. A file lock can.

**Windows keeps the in-process lock only.** `fcntl` is POSIX-only, and importing it
unconditionally meant `app.server` did not import at all on the platform README.md
documents — the app failed to start rather than failing to lock, which is a far worse
answer to a race nobody had reported. So the file lock degrades to a no-op where it
cannot exist, leaving Windows exactly the protection it had before this module: two
browser tabs are still serialized, a simultaneous CLI run is not. Anyone who needs
that guarantee on Windows should run the CLI while the server is stopped, or the
fallback here can grow an `msvcrt.locking` branch.
"""
import hashlib
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import anyio

from .util import ROOT

try:
    import fcntl
except ImportError:          # Windows, and any other platform without POSIX locks
    fcntl = None


# Keep coordination outside the personal-data tree and the git worktree. Every process
# using the same FA_ROOT derives the same file; separate test sandboxes never block one
# another. The file contains no data and may safely outlive a process.
LOCK_PATH = Path(tempfile.gettempdir()) / (
    "unpain-write-%s.lock" % hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:16])

AVAILABLE = fcntl is not None


def _handle():
    return open(LOCK_PATH, "a+b")  # noqa: SIM115 - caller owns the lock lifetime


@contextmanager
def mutation_lock():
    """Serialize a synchronous mutation against web and CLI writers."""
    if fcntl is None:
        yield
        return
    handle = _handle()
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@asynccontextmanager
async def async_mutation_lock():
    """Async form that waits off the event loop, so reads remain responsive."""
    if fcntl is None:
        yield
        return
    handle = _handle()
    try:
        await anyio.to_thread.run_sync(fcntl.flock, handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
