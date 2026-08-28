"""The offline run reports one digest per class, not one across the folder."""

import sys
from pathlib import Path

import pytest

from co_lectr import cli

SAMPLES = Path(__file__).parent.parent / "samples"


class FakeStore:
    """Firestore without Firestore: a profile that already knows this student."""

    def __init__(self, recurring):
        self.recurring = recurring
        self.recorded = []

    def get_review(self, rid):
        return None  # nothing cached, so every submission is reviewed for real

    def profile(self, student):
        return {"recurring": dict(self.recurring)} if student == "student_01" else {}

    def record(self, **kwargs):
        self.recorded.append(kwargs)

    def digest(self, class_id, milestone):
        return []


def test_digests_are_per_class(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["co-lectr", str(SAMPLES), "--no-store"])
    cli.main()
    out = capsys.readouterr().out

    assert "Class 12a - 6 submission(s)" in out
    assert "Class 12b - 4 submission(s)" in out
    assert "4/6  ruff:E722" in out
    assert "3/4  ruff:E722" in out
    # 7/10 is what pooling the two classes together used to report.
    assert "7/10" not in out


def test_the_students_profile_reaches_the_reviewer(monkeypatch, capsys):
    """What the student has hit before is read from the store and passed into the review."""
    store = FakeStore({"ruff:E722": 3, "ruff:F401": 1})
    monkeypatch.setattr(cli, "open_store", lambda: store)
    seen = {}

    def fake_review(submission, findings, chapters, recurring=None, attempts=4):
        seen[submission.name] = recurring
        return []

    monkeypatch.setattr(cli, "review_with_backoff", fake_review)
    monkeypatch.setattr(sys, "argv", ["co-lectr", str(SAMPLES), "--review", "--pace", "0"])
    cli.main()

    assert seen["student_01"] == {"ruff:E722": 3, "ruff:F401": 1}
    assert seen["student_02"] == {}  # a student the profile has never seen
    # student_01 hits both remembered rules again, and the run says so.
    assert "2 rule(s) seen before" in capsys.readouterr().out


def test_an_unchanged_submission_costs_no_profile_read(monkeypatch):
    """The cached branch returns before the profile is touched."""
    store = FakeStore({"ruff:E722": 3})
    monkeypatch.setattr(store, "get_review", lambda rid: {"questions": []})
    monkeypatch.setattr(store, "profile", lambda student: pytest.fail("profile read on a cached review"))
    monkeypatch.setattr(cli, "open_store", lambda: store)
    monkeypatch.setattr(sys, "argv", ["co-lectr", str(SAMPLES), "--review", "--pace", "0"])
    cli.main()
