"""Firestore persistence.

The unit tests run anywhere. The integration tests need real credentials and are
skipped without them; they write under a `_test_` prefix and clean up after
themselves.
"""

import os
import uuid

import pytest

from co_lectr.layer1 import Finding
from co_lectr.store import UNASSIGNED, Store, review_id

needs_firestore = pytest.mark.skipif(
    not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
    reason="no Firestore credentials in the environment",
)


def finding(rule, line=1):
    return Finding(rule=rule, message=f"msg {rule}", path="agent.py", line=line, tool="ruff")


def test_review_id_survives_a_slash_in_the_repo_name():
    assert "/" not in review_id("kbatya/co-lectr", 7, "a3f19c2")


def test_review_id_is_unique_per_commit():
    assert review_id("r", 1, "aaa") != review_id("r", 1, "bbb")


@pytest.fixture
def store_and_ids():
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).parent.parent / ".env")
    store = Store.open()
    tag = f"_test_{uuid.uuid4().hex[:8]}"
    created = {"reviews": [], "students": [], "classes": []}
    yield store, tag, created
    for rid in created["reviews"]:
        store.db.collection("reviews").document(rid).delete()
    for sid in created["students"]:
        store.db.collection("students").document(sid).delete()
    for cid in created["classes"]:
        ref = store.db.collection("classes").document(cid)
        for m in ref.collection("milestones").stream():
            m.reference.delete()
        ref.delete()


@needs_firestore
def test_has_review_is_false_before_and_true_after(store_and_ids):
    store, tag, created = store_and_ids
    rid, student, klass = f"{tag}#1#aaa", f"{tag}_s1", f"{tag}_c"
    created["reviews"].append(rid); created["students"].append(student); created["classes"].append(klass)

    assert store.has_review(rid) is False
    store.record(rid=rid, student=student, class_id=klass, milestone="ch1",
                 chapters_taught=["ch1"], findings=[finding("ruff:E722")],
                 questions=[], model="test")
    assert store.has_review(rid) is True


@needs_firestore
def test_profile_accumulates_across_reviews(store_and_ids):
    store, tag, created = store_and_ids
    student, klass = f"{tag}_s2", f"{tag}_c"
    created["students"].append(student); created["classes"].append(klass)

    assert store.profile(student) == {}
    for n, sha in enumerate(("aaa", "bbb")):
        rid = f"{tag}#{n}#{sha}"
        created["reviews"].append(rid)
        store.record(rid=rid, student=student, class_id=klass, milestone="ch1",
                     chapters_taught=[], findings=[finding("ruff:E722")],
                     questions=[], model="test")

    profile = store.profile(student)
    assert profile["prs_reviewed"] == 2
    assert profile["recurring"]["ruff:E722"] == 2


@needs_firestore
def test_digest_counts_students_once_and_orders_by_reach(store_and_ids):
    store, tag, created = store_and_ids
    klass = f"{tag}_c"
    created["classes"].append(klass)
    plan = [("s1", ["ruff:E722", "ruff:F401"]), ("s1", ["ruff:E722"]), ("s2", ["ruff:E722"])]
    for n, (who, rules) in enumerate(plan):
        student, rid = f"{tag}_{who}", f"{tag}#{n}#sha{n}"
        created["students"].append(student); created["reviews"].append(rid)
        store.record(rid=rid, student=student, class_id=klass, milestone="ch1",
                     chapters_taught=[], findings=[finding(r) for r in rules],
                     questions=[], model="test")

    rows = store.digest(klass, "ch1")
    assert rows[0]["rule"] == "ruff:E722"
    assert len(rows[0]["students"]) == 2      # s1 twice, counted once
    assert rows[0]["occurrences"] == 3


@needs_firestore
def test_unassigned_students_stay_out_of_every_class_digest(store_and_ids):
    store, tag, created = store_and_ids
    student, rid = f"{tag}_s3", f"{tag}#9#ccc"
    created["students"].append(student); created["reviews"].append(rid)

    store.record(rid=rid, student=student, class_id=UNASSIGNED, milestone="ch1",
                 chapters_taught=[], findings=[finding("ruff:E722")],
                 questions=[], model="test")

    assert store.profile(student)["prs_reviewed"] == 1     # still reviewed
    assert store.digest(UNASSIGNED, "ch1") == []           # but not counted


@needs_firestore
def test_a_clean_submission_does_not_wipe_the_class_digest(store_and_ids):
    """Regression: `counts: {}` is an explicit value to Firestore, not a no-op.

    A student with zero findings used to overwrite the whole class's running
    totals with an empty map.
    """
    store, tag, created = store_and_ids
    klass = f"{tag}_c"
    created["classes"].append(klass)

    messy, clean = f"{tag}_messy", f"{tag}_clean"
    for student, findings in ((messy, [finding("ruff:E722")]), (clean, [])):
        rid = f"{tag}#{student}#sha"
        created["students"].append(student); created["reviews"].append(rid)
        store.record(rid=rid, student=student, class_id=klass, milestone="ch1",
                     chapters_taught=[], findings=findings, questions=[], model="test")

    rows = store.digest(klass, "ch1")
    assert [r["rule"] for r in rows] == ["ruff:E722"]
    assert rows[0]["students"] == [messy]
