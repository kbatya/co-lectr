"""Firestore persistence for Co-Lectr.

Collections, as documented in Design.md:

    reviews/{repo}#{pr}#{sha}                 one review: findings and questions
    students/{login}                          the misconception profile, accumulating
    classes/{class}/milestones/{milestone}    what that class got wrong, counted
    classes/{class}/themes/{slug}             a claimed project theme, one per student

Two deliberate limits on what is stored:

* **No whole files.** A review keeps rule ids, paths, lines, the finding messages
  and the questions - not the source. A message or a question can quote one
  identifier or the line it asks about, but the code itself already lives in the
  student's own repo, and these are minors' submissions: the less of their work
  sits in a cloud database, the fewer consent problems there are.
* **Nothing derived by the model, in the counts.** The class picture is built
  from layer-1 rule ids only, because those count exactly.

Counters use Firestore's atomic Increment and ArrayUnion rather than
read-modify-write: two students in the same class can be reviewed at the same
moment by two GitHub Actions runners.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from co_lectr.layer1 import Finding

UNASSIGNED = "unassigned"  # class.yml missing or unreadable - see Design.md

CLASS_FILE = Path(".colectr") / "class.yml"

CLASS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # what Firestore takes as a document id


def class_id_from(root: Path) -> str:
    """Which class this submission belongs to.

    One line the student never edits, placed from the template at provisioning.
    A missing or broken file must not stop the review: the student still gets
    their questions, the findings just stay out of every class digest.
    """
    path = Path(root) / CLASS_FILE
    if not path.is_file():
        return UNASSIGNED
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        value = str(loaded["class"] or "").strip()
    except (yaml.YAMLError, KeyError, TypeError, AttributeError):
        return UNASSIGNED
    # An id Firestore will not take is worse than no id at all: `12/a` reads as a
    # document path and raises mid-write, after the review has been paid for. And
    # the file sits in the student's own repo, so what it says is accepted, not
    # trusted - a name outside this shape means unassigned, not a new class.
    return value if CLASS_ID.fullmatch(value) else UNASSIGNED


GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}")  # GitHub's own shape


def student_id(login: str) -> str:
    """A GitHub login, validated into a Firestore-safe document id.

    The login comes from the pull-request payload, so it is GitHub's, not the
    student's to type — but it still becomes a document id, and an id Firestore
    would refuse (or one shaped unlike a login) must not reach a write. Anything
    off-shape lands in `unassigned` rather than crashing the review.
    """
    login = (login or "").strip()
    return login if GITHUB_LOGIN.fullmatch(login) else UNASSIGNED


def review_id(repo: str, pr: int, sha: str) -> str:
    """Stable document id. A repo is `owner/name`, and `/` is illegal in an id."""
    return f"{repo}#{pr}#{sha}".replace("/", "_")


def spec_id(milestone: str, require: list[str], chapters: list[str]) -> str:
    """Hash of the assignment inputs that decide what a review is allowed to say.

    Folded into the review id alongside the commit sha. Unchanged code still has
    to be reviewed again once the milestone moves or another chapter is taught:
    otherwise the run replays questions scoped to last week's syllabus, and
    writes nothing into the new milestone's class digest.
    """
    payload = json.dumps([milestone, sorted(require), sorted(chapters)])
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def theme_slug(theme: str) -> str:
    """A project theme, hashed into a stable Firestore document id.

    The slug is only the atomic lock against two students claiming the *same
    words*. It folds case and whitespace — `A Chess Engine` and `a  chess engine`
    are one theme — then hashes, so identical text lands on one id and `create()`
    lets exactly one of them through. It deliberately does NOT fold punctuation:
    `C++ parser` and `C parser` are different themes and must get different ids,
    not collide into one and have the second wrongly refused as an exact
    duplicate. Themes that are the same *idea* in different words are a separate
    question, answered by the model in themes.py — this is just the cheap, exact
    half. A theme with no letters or digits is not real content; it returns ""
    and the caller treats it as unreadable.
    """
    normalized = " ".join((theme or "").lower().split())
    if not re.search(r"[a-z0-9]", normalized):
        return ""
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


@dataclass
class Store:
    db: firestore.Client

    @classmethod
    def open(cls) -> Store:
        """Credentials come from GOOGLE_APPLICATION_CREDENTIALS."""
        return cls(firestore.Client())

    # --- reads ---------------------------------------------------------------

    def has_review(self, rid: str) -> bool:
        """True if this exact commit was already reviewed.

        This is what stops a student's second push from spending Gemini requests
        on work that has not changed.
        """
        return self.db.collection("reviews").document(rid).get().exists

    # --- claim ---------------------------------------------------------------

    def reserve(self, rid: str, *, student: str, milestone: str) -> bool:
        """Claim this review id atomically. True if we won it, False if taken.

        `create()` fails when the document already exists, so the reservation
        itself is the lock: two deliveries carrying the same head sha race to
        create the placeholder, exactly one wins, and only the winner reviews
        and posts. It replaces a check-then-act `has_review`, which two threads
        could both pass before either wrote. `record` overwrites the placeholder
        with the finished review.
        """
        try:
            self.db.collection("reviews").document(rid).create({
                "student": student,
                "milestone": milestone,
                "status": "reviewing",
                "created_at": firestore.SERVER_TIMESTAMP,
            })
            return True
        except AlreadyExists:
            return False

    def release(self, rid: str) -> None:
        """Drop a reservation whose review never posted, so a redelivery of the
        same commit can try again instead of being locked out by the placeholder.
        Only called when the work failed before the comment went out."""
        self.db.collection("reviews").document(rid).delete()

    def reserve_theme(
        self, class_id: str, slug: str, *,
        student: str, theme: str, spec: str, repo: str = "", pr: int = 0,
    ) -> bool:
        """Claim a project theme's slug atomically. True if won, False if taken.

        The same `create()` lock the review reservation uses: two students who
        submit the identical theme at the same moment both try to create the one
        `themes/{slug}` document, and exactly one wins. The reworded-but-same case
        is not this method's job — the conflict check runs first and only a theme
        that survived it reaches here. Held `pending` until the lecturer approves.
        """
        try:
            self._theme_ref(class_id, slug).create({
                "slug": slug,
                "theme": theme,
                "spec": spec,
                "student": student,
                "class_id": class_id,
                "status": "pending",
                "repo": repo,
                "pr": pr,
                "created_at": firestore.SERVER_TIMESTAMP,
            })
            return True
        except AlreadyExists:
            return False

    def get_review(self, rid: str) -> dict | None:
        """The stored review, or None. Lets a repeat run reuse questions for free."""
        snap = self.db.collection("reviews").document(rid).get()
        return snap.to_dict() if snap.exists else None

    def profile(self, student: str) -> dict:
        """The student's accumulated profile, or {} if they are new.

        `recurring` maps rule id -> how many times that student has hit it. The
        reviewer reads this before writing questions, so "we have been here
        before" is available to it.
        """
        snap = self.db.collection("students").document(student).get()
        return snap.to_dict() if snap.exists else {}

    def flagged_reviews(self, class_id: str = "", milestone: str = "") -> list[dict]:
        """Reviews where a prompt-injection attempt was detected in student code.

        The channel that turns detection into something the lecturer can act on.
        A single-field `injection_flagged` filter needs no composite index; the
        class and milestone are narrowed in Python, cheap at pilot scale.
        """
        query = self.db.collection("reviews").where(
            filter=FieldFilter("injection_flagged", "==", True)
        )
        rows = []
        for snap in query.stream():
            d = snap.to_dict() or {}
            if class_id and d.get("class_id") != class_id:
                continue
            if milestone and d.get("milestone") != milestone:
                continue
            rows.append({
                "student": d.get("student"),
                "repo": d.get("repo"),
                "pr": d.get("pr"),
                "class_id": d.get("class_id"),
                "milestone": d.get("milestone"),
            })
        return rows

    def class_config(self, class_id: str) -> dict:
        """Per-class review scope from the class doc: chapters, required symbols,
        milestone. Any key the document does not set comes back as None, and the
        caller falls back to the service-wide env default. This is what lets two
        classes moving at different speeds be reviewed correctly without a
        redeploy - the design's answer, wired up (Design.md).
        """
        if class_id == UNASSIGNED:
            return {}
        snap = self.db.collection("classes").document(class_id).get()
        data = snap.to_dict() if snap.exists else {}
        return {
            "chapters": data.get("chapters"),
            "required": data.get("required"),
            "milestone": data.get("milestone"),
        }

    def has_approved_theme(self, student: str) -> bool:
        """True once this student's project theme is approved.

        The gate the delivery path reads: while it is False a PR carrying a theme
        proposal is handled as a proposal; once True the student is past the gate
        and their pushes are reviewed as code.
        """
        return self.profile(student).get("theme_status") == "approved"

    def get_theme(self, class_id: str, slug: str) -> dict | None:
        """One claimed theme by its slug, or None if the slug is free."""
        snap = self._theme_ref(class_id, slug).get()
        return snap.to_dict() if snap.exists else None

    def list_themes(self, class_id: str, status: str = "") -> list[dict]:
        """The themes claimed in a class, optionally only those in one status.

        Read by the conflict check (to compare a new theme against the claimed
        ones) and by the lecturer's pending-themes tool. The status filter is
        applied in Python — at a class's scale a handful of docs, no index.
        """
        if class_id == UNASSIGNED:
            return []
        rows = []
        for snap in self._themes_col(class_id).stream():
            data = snap.to_dict() or {}
            if status and data.get("status") != status:
                continue
            rows.append(data)
        return rows

    def digest(self, class_id: str, milestone: str) -> list[dict]:
        """What this class got wrong, most widespread first."""
        snap = self._milestone_ref(class_id, milestone).get()
        counts = (snap.to_dict() or {}).get("counts", {}) if snap.exists else {}
        rows = [
            {
                "rule": rule,
                "students": sorted(data.get("students", [])),
                "occurrences": data.get("occurrences", 0),
            }
            for rule, data in counts.items()
        ]
        return sorted(rows, key=lambda r: (-len(r["students"]), -r["occurrences"], r["rule"]))

    # --- write ---------------------------------------------------------------

    def record(
        self,
        *,
        rid: str,
        student: str,
        class_id: str,
        milestone: str,
        chapters_taught: list[str],
        findings: list[Finding],
        questions: list[dict],
        model: str,
        repo: str = "",
        pr: int = 0,
    ) -> None:
        """Persist one review, and fold it into the student and class pictures.

        All the writes go in one `batch()`: the review, the student profile and
        the class counters commit together or not at all. Three separate `set`s
        could leave the review recorded - and so never retried, by design - while
        a crash between them lost the profile and the class count, an undercount
        that is permanent and invisible.
        """
        per_rule = Counter(f.rule for f in findings)
        # The model reports a directive found in student code as an injection
        # attempt (reviewer rule 4). Persist that it happened, so it is not lost
        # the moment the comment is posted - a detection the lecturer never sees
        # is not a control.
        injections = sum(1 for q in questions if q.get("rule") == "prompt-injection-attempt")
        batch = self.db.batch()

        batch.set(self.db.collection("reviews").document(rid), {
            "student": student,
            "class_id": class_id,
            "milestone": milestone,
            "repo": repo,
            "pr": pr,
            "chapters_taught": chapters_taught,
            "model": model,
            "findings": [f.as_dict() for f in findings],
            "questions": questions,
            "injection_flagged": injections > 0,
            "status": "complete",
            "created_at": firestore.SERVER_TIMESTAMP,
        })

        student_doc = {
            "class_id": class_id,
            "last_seen": firestore.SERVER_TIMESTAMP,
            "prs_reviewed": firestore.Increment(1),
            "recurring": {rule: firestore.Increment(n) for rule, n in per_rule.items()},
        }
        if injections:
            student_doc["injection_attempts"] = firestore.Increment(injections)
        batch.set(self.db.collection("students").document(student), student_doc, merge=True)

        if class_id != UNASSIGNED:
            batch.set(self.db.collection("classes").document(class_id), {
                "class_id": class_id,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)

            # A clean submission has nothing to count. Writing `counts: {}` here
            # would not merge into the running totals - Firestore takes an empty
            # map as an explicit value and wipes them, so one careful student
            # would erase the whole class digest.
            if per_rule:
                batch.set(self._milestone_ref(class_id, milestone), {
                    "class_id": class_id,
                    "milestone": milestone,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                    "counts": {
                        rule: {
                            "occurrences": firestore.Increment(n),
                            "students": firestore.ArrayUnion([student]),
                        }
                        for rule, n in per_rule.items()
                    },
                }, merge=True)

        batch.commit()

    def approve_theme(self, class_id: str, slug: str) -> dict:
        """Approve a pending theme and record it to its student. Returns the
        theme, or {} if the slug is not claimed.

        The theme doc flips to `approved`, and the same batch writes the theme
        onto the student's profile — one commit, so the student is never marked
        as owning a theme the class registry does not show as approved. This is
        the lecturer's step: the automatic check at proposal time only kept the
        slug free; a person still decides the project is one to run.
        """
        ref = self._theme_ref(class_id, slug)
        snap = ref.get()
        if not snap.exists:
            return {}
        data = snap.to_dict() or {}
        student = data.get("student", "")
        batch = self.db.batch()
        batch.set(ref, {
            "status": "approved",
            "approved_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        if student and student != UNASSIGNED:
            batch.set(self.db.collection("students").document(student), {
                "class_id": class_id,
                "theme": data.get("theme", ""),
                "theme_slug": slug,
                "theme_status": "approved",
            }, merge=True)
        batch.commit()
        return {**data, "status": "approved"}

    def release_theme(self, class_id: str, slug: str) -> None:
        """Drop a student's own pending theme so they can claim a different one.

        The revision path's counterpart to `release` for reviews: when a student
        edits `.colectr/project.yml` to a new theme while the old one is still
        pending, the old slug is freed here before the new one is reserved, so a
        student never holds two pending themes and the abandoned slug does not
        block a classmate. Unlike `reject_theme` this is the student's own doing,
        not the lecturer's, so it returns nothing to post back.
        """
        self._theme_ref(class_id, slug).delete()

    def reject_theme(self, class_id: str, slug: str) -> dict:
        """Reject a pending theme. Returns the theme, or {}.

        The slug doc is deleted so the theme does not sit there blocking a
        classmate — a rejection bars *this* student from the theme, not everyone.
        The bar is recorded on the student's own profile (`rejected_themes`), so
        re-pushing the identical `project.yml` does not simply re-reserve it and
        put a decided theme back in the lecturer's queue: the propose path checks
        this list and turns a rejected theme back. The student is free to propose
        a genuinely different one. Reject only touches pending themes — an
        approved theme is already recorded on the profile.
        """
        ref = self._theme_ref(class_id, slug)
        snap = ref.get()
        if not snap.exists:
            return {}
        data = snap.to_dict() or {}
        student = data.get("student", "")
        batch = self.db.batch()
        batch.delete(ref)
        if student and student != UNASSIGNED:
            batch.set(self.db.collection("students").document(student), {
                "rejected_themes": firestore.ArrayUnion([slug]),
            }, merge=True)
        batch.commit()
        return data

    # --- internal ------------------------------------------------------------

    def _milestone_ref(self, class_id: str, milestone: str):
        return (
            self.db.collection("classes").document(class_id)
            .collection("milestones").document(milestone)
        )

    def _themes_col(self, class_id: str):
        return self.db.collection("classes").document(class_id).collection("themes")

    def _theme_ref(self, class_id: str, slug: str):
        return self._themes_col(class_id).document(slug)
