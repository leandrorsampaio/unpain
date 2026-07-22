"""Shared isolated FA_ROOT setup for integration and browser tests."""
import shutil
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = PROJECT / "examples"


def build_sandbox(root):
    """Populate ``root`` with generic config, rules, accounts, and inbox fixtures."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLES / "config.json", root / "config.json")
    (root / "rules").mkdir()
    for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
        shutil.copy(EXAMPLES / name, root / "rules" / name)
    (root / "data").mkdir()
    shutil.copy(EXAMPLES / "accounts.json", root / "data" / "accounts.json")
    (root / "inbox" / "processed").mkdir(parents=True)
    for fixture in FIXTURES.iterdir():
        if fixture.is_file():
            shutil.copy(fixture, root / "inbox" / fixture.name)
    return root
