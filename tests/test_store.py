"""Firestore persistence.

The unit tests run anywhere. The integration tests need real credentials and are
skipped without them; they write under a `_test_` prefix and clean up after
themselves.
"""

import os
import uuid

import pytest
from co_lectr.layer1 import Finding
from co_lectr.store import (
    UNASSIGNED,
    Store,
    class_id_from,
    review_id,
    spec_id,
    theme_slug,
)

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


def test_spec_id_changes_when_the_taught_chapters_change():
    # A new chapter must produce a new review id, or a redelivered PR is skipped
    # as "already reviewed" and never lands in the new milestone's digest.
    assert spec_id("m1", ["run_agent"], ["ch1"]) != spec_id("m1", ["run_agent"], ["ch1", "ch2"])


def test_spec_id_is_stable_regardless_of_input_order():
    assert spec_id("m1", ["a", "b"], ["ch1", "ch2"]) == spec_id("m1", ["b", "a"], ["ch2", "ch1"])


def write_class_file(tmp_path, text):
    (tmp_path / ".colectr").mkdir()
    (tmp_path / ".colectr" / "class.yml").write_text(text, encoding="utf-8")
    return tmp_path


def test_class_id_is_read_from_the_file(tmp_path):
    assert class_id_from(write_class_file(tmp_path, "class: 12a")) == "12a"


def test_a_missing_file_is_unassigned(tmp_path):
    assert class_id_from(tmp_path) == UNASSIGNED


@pytest.mark.parametrize("text", [
    "class:",             # key present, no value - used to become the class "None"
    'class: ""',
    "class: 12/a",        # a slash is a document path to Firestore, not an id
    "class: ../reviews",
    "- not a mapping",
    "class: [12a]",
    "",
])
def test_an_id_firestore_would_reject_is_unassigned(tmp_path, text):
    assert class_id_from(write_class_file(tmp_path, text)) == UNASSIGNED


def test_class_config_short_circuits_an_unassigned_submission():
    # Unassigned needs no Firestore round-trip - and no credentials to test.
    assert Store(db=None).class_config(UNASSIGNED) == {}


@needs_firestore
def test_reserve_theme_is_won_once_and_lost_the_second_time(store_and_ids):
    # Two students committing the identical theme race to create one slug doc;
    # exactly one wins the reservation.
    store, tag, created = store_and_ids
    klass, slug = f"{tag}_c", theme_slug("A chess engine")
    created["classes"].append(klass)

    assert store.reserve_theme(klass, slug, student="ada", theme="A chess engine", spec="x") is True
    assert store.reserve_theme(klass, slug, student="bob", theme="A chess engine", spec="y") is False
    assert store.get_theme(klass, slug)["student"] == "ada"  # the first writer stands


@needs_firestore
def test_list_themes_filters_by_status(store_and_ids):
    store, tag, created = store_and_ids
    klass, s_a, s_b = f"{tag}_c", f"{tag}_a", f"{tag}_b"
    created["classes"].append(klass)
    created["students"].extend([s_a, s_b])
    store.reserve_theme(klass, "a", student=s_a, theme="Alpha", spec="")
    store.reserve_theme(klass, "b", student=s_b, theme="Beta", spec="")
    store.approve_theme(klass, "a")

    assert {t["slug"] for t in store.list_themes(klass, status="pending")} == {"b"}
    assert {t["slug"] for t in store.list_themes(klass)} == {"a", "b"}


@needs_firestore
def test_approve_theme_records_it_to_the_student(store_and_ids):
    store, tag, created = store_and_ids
    klass, student = f"{tag}_c", f"{tag}_s"
    created["classes"].append(klass); created["students"].append(student)
    store.reserve_theme(klass, "chess", student=student, theme="A chess engine", spec="minimax")

    assert store.has_approved_theme(student) is False
    approved = store.approve_theme(klass, "chess")

    assert approved["status"] == "approved"
    assert store.has_approved_theme(student) is True
    assert store.profile(student)["theme"] == "A chess engine"
    assert store.get_theme(klass, "chess")["status"] == "approved"


@needs_firestore
def test_reject_theme_frees_the_slug_for_a_classmate_but_bars_the_student(store_and_ids):
    store, tag, created = store_and_ids
    klass = f"{tag}_c"
    rejected_s, other_s = f"{tag}_rej", f"{tag}_oth"
    created["classes"].append(klass)
    created["students"].extend([rejected_s, other_s])
    store.reserve_theme(klass, "chess", student=rejected_s, theme="A chess engine", spec="")

    assert store.reject_theme(klass, "chess")["theme"] == "A chess engine"
    assert store.get_theme(klass, "chess") is None
    # The bar is recorded on the rejected student's own profile...
    assert "chess" in store.profile(rejected_s).get("rejected_themes", [])
    # ...but the slug is free, so a classmate can still claim the same words.
    assert store.reserve_theme(klass, "chess", student=other_s, theme="A chess engine", spec="") is True


@needs_firestore
def test_release_theme_frees_a_students_own_pending_slug(store_and_ids):
    # The revision path: a student editing to a new theme frees the old slug.
    store, tag, created = store_and_ids
    klass = f"{tag}_c"
    created["classes"].append(klass)
    store.reserve_theme(klass, "chess", student="ada", theme="A chess engine", spec="")

    store.release_theme(klass, "chess")
    assert store.get_theme(klass, "chess") is None


@needs_firestore
def test_class_config_reads_the_class_document(store_and_ids):
    store, tag, created = store_and_ids
    klass = f"{tag}_c"
    created["classes"].append(klass)
    store.db.collection("classes").document(klass).set({
        "chapters": ["ch1", "ch2"], "required": ["run_agent"], "milestone": "ch2",
    })
    cfg = store.class_config(klass)
    assert cfg["chapters"] == ["ch1", "ch2"]
    assert cfg["required"] == ["run_agent"]
    assert cfg["milestone"] == "ch2"


@pytest.fixture
def store_and_ids():
    from pathlib import Path

    from dotenv import load_dotenv

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
        for t in ref.collection("themes").stream():
            t.reference.delete()
        ref.delete()


@needs_firestore
def test_reserve_is_won_once_and_lost_the_second_time(store_and_ids):
    # The lock against a duplicate review/comment: two deliveries with the same
    # head sha both call reserve, exactly one wins.
    store, tag, created = store_and_ids
    rid, student = f"{tag}#1#aaa", f"{tag}_s0"
    created["reviews"].append(rid)

    assert store.reserve(rid, student=student, milestone="ch1") is True
    assert store.reserve(rid, student=student, milestone="ch1") is False
    # release hands the claim back so a redelivery can try again
    store.release(rid)
    assert store.reserve(rid, student=student, milestone="ch1") is True


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
def test_an_injection_attempt_is_flagged_and_readable(store_and_ids):
    # Detection has to reach the lecturer: the review is flagged, the student's
    # count goes up, and flagged_reviews reads it back.
    store, tag, created = store_and_ids
    student, rid, klass = f"{tag}_inj", f"{tag}#1#inj", f"{tag}_c"
    created["students"].append(student); created["reviews"].append(rid); created["classes"].append(klass)

    store.record(
        rid=rid, student=student, class_id=klass, milestone="ch1",
        chapters_taught=[], findings=[finding("ruff:E722")],
        questions=[{"path": "agent.py", "line": 1,
                    "rule": "prompt-injection-attempt", "question": "a directive was here"}],
        model="test", repo="org/repo", pr=3,
    )

    assert store.profile(student).get("injection_attempts") == 1
    flagged = store.flagged_reviews(klass, "ch1")
    assert any(r["student"] == student and r["pr"] == 3 for r in flagged)


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
