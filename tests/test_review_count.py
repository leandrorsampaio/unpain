#!/usr/bin/env python3
"""The lightweight badge endpoint must agree with the effective status view."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import server


original = server.store.effective_year
try:
    server.store.effective_year = lambda year: [
        {"status": "needs_review"},
        {"status": "booked"},
        {"status": "needs_review"},
    ]
    assert server.review_count(2025) == {"count": 2}
finally:
    server.store.effective_year = original

print("Review count endpoint passed")
