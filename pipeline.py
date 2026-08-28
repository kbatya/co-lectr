"""Step 5 — the seam. Turn a `ReviewTarget` into questions on the pull request.

    fetch the PR head  →  analyse (layer 1)  →  review (layer 2)  →  one comment

This is the delivery path the webhook hangs off. Everything but the fetch and the
post is the same review core the CLI runs offline; nothing here is GitHub-specific
except `github.py`, which this calls. It runs inside the Cloud Run container, off
the webhook's background thread, so a slow Gemini call never holds GitHub's
delivery open.

Idempotency: GitHub retries a delivery that does not answer in time, and the same
commit must not be reviewed — or commented — twice. The commit sha already names
the work, so `reviews/{repo}#{pr}#{sha}` is the natural guard: if it is already
stored, this returns without spending a request or posting again.

Running `pytest` executes student code, so it is off unless `COLECTR_RUN_TESTS=1`
is set explicitly on the service — the container is the only place it may ever run.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("co_lectr")

from .github import GitHubClient
from .layer1 import analyse
from .reviewer import MODEL, review
from .store import UNASSIGNED, Store, class_id_from, review_id, spec_id, student_id
from .web import ReviewTarget


def _split(value: str) -> list[str]:
    """Comma-separated env value → list, blanks dropped."""
    return [part.strip() for part in value.split(",") if part.strip()]


def format_comment(questions: list[dict], model: str, findings_count: int = 0) -> str:
    """One PR comment. Questions, never fixes — each anchored to file:line.

    The empty-questions message is gated on whether layer 1 found anything, not on
    the questions alone: telling a student "no findings" when layer 1 did flag
    something (the model raised nothing in scope, or its output did not parse) is a
    falsehood, and the falsehood is invisible.
    """
    header = "### Co-Lectr review\n\n"
    if not questions:
        if findings_count:
            body = (
                f"Layer 1 flagged {findings_count} item(s), but there was nothing in "
                "scope to turn into a question this time. Explain your key choices in "
                "your own words when you get the chance."
            )
        else:
            body = (
                "Nothing to ask this time — a clean pass from layer 1. "
                "Explain your key choices in your own words when you get the chance."
            )
        return header + body
    lines = []
    for q in questions:
        where = f"`{q.get('path') or '?'}:{q.get('line', 0)}`"
        lines.append(f"- {where} — {q['question']}")
    intro = (
        "Work through these — I ask, I don't fix. Explain each in your own words; "
        "the point is that you can.\n\n"
    )
    footer = (
        f"\n\n<sub>{len(questions)} question(s). Facts from ruff · ast · pytest, "
        f"outside the model; questions from {model}.</sub>"
    )
    return header + intro + "\n".join(lines) + footer


async def run_review(
    target: ReviewTarget,
    *,
    client: GitHubClient,
    chapters: list[str],
    required: list[str],
    run_tests: bool = False,
    store: Store | None = None,
    milestone: str = "pilot",
    model: str = MODEL,
) -> list[dict]:
    """Fetch the head, review it, post the questions. Returns the questions.

    The student is the pull-request author (`target.student`), not the repo
    owner: provisioning fans one repo per student out of a course org, so the
    owner segment is the org — the same for every student — and only the author
    login tells them apart in the profile and the class digest.

    The review id folds in the assignment spec as well as the commit sha, so a
    redelivery after a new chapter is taught is reviewed again — scoped to the
    new syllabus — rather than skipped as "already seen" and lost from the new
    milestone's digest.
    """
    student = student_id(target.student)
    with tempfile.TemporaryDirectory() as tmp:
        root = client.fetch_source(target.repo, target.sha, Path(tmp))
        class_id = class_id_from(root)
        # Per-class review scope from the class doc, falling back to the env
        # defaults: two classes at different chapters are then reviewed correctly
        # without a redeploy (Design.md). The class the submission names decides
        # its own milestone, chapters and required symbols; the spec id below
        # folds them in, so a class advancing a milestone re-reviews and lands in
        # the new digest. Computed before the reserve, which depends on the spec.
        if store is not None and class_id != UNASSIGNED:
            cfg = store.class_config(class_id)
            chapters = cfg.get("chapters") or chapters
            required = cfg.get("required") or required
            milestone = cfg.get("milestone") or milestone

        rid = review_id(
            target.repo, target.pr, f"{target.sha}.{spec_id(milestone, required, chapters)}"
        )
        if store is not None and not store.reserve(rid, student=student, milestone=milestone):
            return []  # another delivery already claimed this exact commit+spec

        try:
            findings = analyse(root, required_symbols=tuple(required), run_tests=run_tests)
            recurring = store.profile(student).get("recurring", {}) if store else {}
            questions = await review(root, findings, chapters, recurring)
            flagged = [q for q in questions if q.get("rule") == "prompt-injection-attempt"]
            if flagged:
                log.warning(
                    "prompt-injection-attempt detected in %s PR#%s by %s at %s",
                    target.repo, target.pr, student,
                    ", ".join(f"{q.get('path')}:{q.get('line')}" for q in flagged),
                )
            client.post_comment(
                target.repo, target.pr,
                format_comment(questions, model, findings_count=len(findings)),
            )
        except Exception:
            # Nothing was posted, so hand the claim back: a redelivery of this
            # commit may retry. Once the comment is out the claim stands — a failed
            # `record` must not become a second comment to the student.
            if store is not None:
                store.release(rid)
            raise

    if store is not None:
        store.record(
            rid=rid, student=student, class_id=class_id, milestone=milestone,
            chapters_taught=chapters, findings=findings, questions=questions,
            model=model, repo=target.repo, pr=target.pr,
        )
    return questions


def _open_store() -> Store | None:
    """Persistence is on by default; `COLECTR_FIRESTORE=0` turns it off.

    The class digest is the whole point of the delivery path, and it only fills
    up if reviews are stored — so the deployed service persists unless explicitly
    told not to. In Cloud Run the credentials are the ambient service account, so
    this does not look for a key file the way the CLI does.
    """
    if os.environ.get("COLECTR_FIRESTORE") == "0":
        return None
    return Store.open()


def config_from_env() -> dict:
    """The knobs the service is configured with, read once per delivery."""
    return {
        "client": GitHubClient(os.environ["GITHUB_TOKEN"]),
        "chapters": _split(os.environ.get("COLECTR_CHAPTERS", "")),
        "required": _split(os.environ.get("COLECTR_REQUIRE", "")),
        "run_tests": os.environ.get("COLECTR_RUN_TESTS") == "1",
        "store": _open_store(),  # on unless COLECTR_FIRESTORE=0
        "milestone": os.environ.get("COLECTR_MILESTONE", "pilot"),
        "model": MODEL,
    }
