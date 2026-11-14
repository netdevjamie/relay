"""Shared test set-up.

No test may keep the real machine awake or read when it last slept: both
would make the suite depend on the laptop it runs on. Tests of awake.py
itself replace these again with what they need.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import awake


@pytest.fixture(autouse=True)
def no_real_keep_awake(monkeypatch):
    held = []
    monkeypatch.setattr(awake, "hold", lambda task_dir, seconds: held.append((task_dir, seconds)) or False)
    monkeypatch.setattr(awake, "sleeps_between", lambda started, finished: [])
    return held
