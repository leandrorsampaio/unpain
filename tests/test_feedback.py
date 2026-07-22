"""Feedback entries and uploads stay outside all accountability storage."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile


tmp = Path(tempfile.mkdtemp(prefix="fa-feedback-test-"))
os.environ["FA_ROOT"] = str(tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import server  # noqa: E402


upload = UploadFile(BytesIO(b"screen contents"), filename="screen shot.txt")
created = asyncio.run(server.feedback_add(
    title="Improve the dashboard",
    description="Please add a clearer monthly comparison.",
    file=upload,
))["entry"]

entries_path = tmp / "feedback" / "entries.json"
assert entries_path.is_file()
stored = json.loads(entries_path.read_text())["entries"][0]
attachment = tmp / "feedback" / "files" / stored["attachment"]["stored"]
assert attachment.read_bytes() == b"screen contents"
assert created["attachment"]["name"] == "screen shot.txt"
assert server.feedback_list()["items"][0]["title"] == "Improve the dashboard"
assert Path(server.feedback_file(created["id"]).path) == attachment.resolve()

for accountability_dir in ("data", "inbox", "receipts"):
    directory = tmp / accountability_dir
    assert not directory.exists() or not any(path.is_file() for path in directory.rglob("*"))

# Default backup now includes EVERYTHING, feedback included.
full = server.backup()
with zipfile.ZipFile(full.path) as archive:
    assert any(name.startswith("feedback/") for name in archive.namelist()), "default backup must include feedback"

# A selective backup only zips the requested parts (feedback left out unless asked for).
partial = server.backup(parts="data,rules")
with zipfile.ZipFile(partial.path) as archive:
    assert not any(name.startswith("feedback/") for name in archive.namelist()), "unselected feedback must be excluded"

server.feedback_delete(server.FeedbackDelete(id=created["id"]))
assert server.feedback_list()["items"] == []
assert not attachment.exists()

shutil.rmtree(tmp)
print("Feedback storage passed: entry, attachment, download, deletion and backup isolation")
