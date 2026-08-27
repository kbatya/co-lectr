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

import os
import tempfile
from pathlib import Path

from .github import GitHubClient
from .layer1 import analyse
from .reviewer import MODEL, review
from .store import Store, class_id_from, review_id
from .web import ReviewTarget


def _split(value: str) -> list[str]:
    """Comma-separated env value → list, blanks dropped."""
    return [part.strip() for part in value.split(",") if part.strip()]


def format_comment(questions: list[dict], model: str) -> str:
    """One PR comment. Questions, never fixes — each anchored to file:line."""
    header = "### Co-Lectr review\n\n"
    if not questions:
        return header + (
            "Nothing to ask this time — layer 1 found no findings in scope. "
            "Explain your key choices in your own words when you get the chance."
        )
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

    One repo per student for the year, so the repo owner is the student id — no
    author lookup needed for the profile or the class digest.
    """
    rid = review_id(target.repo, target.pr, target.sha)
    if store is not None and store.has_review(rid):
        return []  # this commit is already reviewed and commented — GitHub retried

    student = target.repo.split("/")[0]
    with tempfile.TemporaryDirectory() as tmp:
        root = client.fetch_source(target.repo, target.sha, Path(tmp))
        findings = analyse(root, required_symbols=tuple(required), run_tests=run_tests)
        class_id = class_id_from(root)
        recurring = store.profile(student).get("recurring", {}) if store else {}
        questions = await review(root, findings, chapters, recurring)

    client.post_comment(target.repo, target.pr, format_comment(questions, model))

    if store is not None:
        store.record(
            rid=rid, student=student, class_id=class_id, milestone=milestone,
            chapters_taught=chapters, findings=findings, questions=questions,
            model=model, repo=target.repo, pr=target.pr,
        )
    return questions


def _open_store() -> Store | None:
    """Firestore is opt-in for the pilot: `COLECTR_FIRESTORE=1` turns it on.

    In Cloud Run the credentials are the ambient service account, so this does
    not look for a key file the way the CLI does.
    """
    if os.environ.get("COLECTR_FIRESTORE") != "1":
        return None
    return Store.open()


def config_from_env() -> dict:
    """The knobs the service is configured with, read once per delivery."""
    return {
        "client": GitHubClient(os.environ["GITHUB_TOKEN"]),
        "chapters": _split(os.environ.get("COLECTR_CHAPTERS", "")),
        "required": _split(os.environ.get("COLECTR_REQUIRE", "")),
        "run_tests": os.environ.get("COLECTR_RUN_TESTS") == "1",
        "store": _open_store(),
        "milestone": os.environ.get("COLECTR_MILESTONE", "pilot"),
        "model": MODEL,
    }
