"""Cross-process serialization for operations that mutate financial state."""
import fcntl
import hashlib
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import anyio

from .util import ROOT


# Keep coordination outside the personal-data tree and the git worktree. Every process
# using the same FA_ROOT derives the same file; separate test sandboxes never block one
# another. The file contains no data and may safely outlive a process.
LOCK_PATH = Path(tempfile.gettempdir()) / (
    "unpain-write-%s.lock" % hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:16])


def _handle():
    return open(LOCK_PATH, "a+b")  # noqa: SIM115 - caller owns the lock lifetime


@contextmanager
def mutation_lock():
    """Serialize a synchronous mutation against web and CLI writers."""
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
    handle = _handle()
    try:
        await anyio.to_thread.run_sync(fcntl.flock, handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
