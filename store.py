"""Firestore persistence for Co-Lectr.

Three collections, as documented in Design.md:

    reviews/{repo}#{pr}#{sha}                 one review: findings and questions
    students/{login}                          the misconception profile, accumulating
    classes/{class}/milestones/{milestone}    what that class got wrong, counted

Two things this deliberately does not store:

* **Student code.** Only rule ids, paths and line numbers. The code already lives
  in the student's own repo, and these are minors' submissions - the less of it
  sits in a cloud database, the fewer consent problems there are.
* **Anything derived by the model, in the counts.** The class picture is built
  from layer-1 rule ids only, because those count exactly.

Counters use Firestore's atomic Increment and ArrayUnion rather than
read-modify-write: two students in the same class can be reviewed at the same
moment by two GitHub Actions runners.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml
from google.cloud import firestore

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


def review_id(repo: str, pr: int, sha: str) -> str:
    """Stable document id. A repo is `owner/name`, and `/` is illegal in an id."""
    return f"{repo}#{pr}#{sha}".replace("/", "_")


@dataclass
class Store:
    db: firestore.Client

    @classmethod
    def open(cls) -> "Store":
        """Credentials come from GOOGLE_APPLICATION_CREDENTIALS."""
        return cls(firestore.Client())

    # --- reads ---------------------------------------------------------------

    def has_review(self, rid: str) -> bool:
        """True if this exact commit was already reviewed.

        This is what stops a student's second push from spending Gemini requests
        on work that has not changed.
        """
        return self.db.collection("reviews").document(rid).get().exists

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
        """Persist one review, and fold it into the student and class pictures."""
        per_rule = Counter(f.rule for f in findings)

        self.db.collection("reviews").document(rid).set({
            "student": student,
            "class_id": class_id,
            "milestone": milestone,
            "repo": repo,
            "pr": pr,
            "chapters_taught": chapters_taught,
            "model": model,
            "findings": [f.as_dict() for f in findings],
            "questions": questions,
            "created_at": firestore.SERVER_TIMESTAMP,
        })

        self.db.collection("students").document(student).set({
            "class_id": class_id,
            "last_seen": firestore.SERVER_TIMESTAMP,
            "prs_reviewed": firestore.Increment(1),
            "recurring": {rule: firestore.Increment(n) for rule, n in per_rule.items()},
        }, merge=True)

        if class_id == UNASSIGNED:
            return

        self.db.collection("classes").document(class_id).set({
            "class_id": class_id,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

        # A clean submission has nothing to count. Writing `counts: {}` here would
        # not merge into the running totals - Firestore takes an empty map as an
        # explicit value and wipes them, so one careful student would erase the
        # whole class digest.
        if per_rule:
            self._milestone_ref(class_id, milestone).set({
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

    # --- internal ------------------------------------------------------------

    def _milestone_ref(self, class_id: str, milestone: str):
        return (
            self.db.collection("classes").document(class_id)
            .collection("milestones").document(milestone)
        )
