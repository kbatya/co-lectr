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
from .store import (
    UNASSIGNED,
    Store,
    class_id_from,
    review_id,
    spec_id,
    student_id,
    theme_slug,
)
from .themes import (
    format_theme_comment,
    parse_project,
    theme_change_notice,
    theme_conflict,
)
from .web import ReviewTarget


def _split(value: str) -> list[str]:
    """Comma-separated env value → list, blanks dropped."""
    return [part.strip() for part in value.split(",") if part.strip()]


def format_comment(questions: list[dict], model: str, findings_count: int = 0, notice: str = "") -> str:
    """One PR comment. Questions, never fixes — each anchored to file:line.

    The empty-questions message is gated on whether layer 1 found anything, not on
    the questions alone: telling a student "no findings" when layer 1 did flag
    something (the model raised nothing in scope, or its output did not parse) is a
    falsehood, and the falsehood is invisible.

    `notice` is an optional line prepended above the review — used when an approved
    student's project.yml names a changed theme, so the heads-up and the questions
    arrive as one comment instead of two.
    """
    lead = f"{notice}\n\n" if notice else ""
    header = lead + "### Co-Lectr review\n\n"
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

        # Project theme gate. A student names their project in
        # .colectr/project.yml, and the theme must be unique in the class before
        # they build on it. Until the theme is approved, a PR that carries a
        # proposal is handled as one — reserved or turned back — not reviewed as
        # code; once approved the file stays in the repo but is past the gate, so
        # normal reviews resume. The proposal file is read first, so the store is
        # only consulted when a proposal is actually present, and an unassigned
        # submission (no class to be unique within) is never gated.
        proposal = parse_project(root) if store is not None and class_id != UNASSIGNED else None
        theme_notice = ""
        if proposal is not None:
            prof = store.profile(student)
            if prof.get("theme_status") != "approved":
                return await handle_proposal(
                    target, proposal, client=client, store=store,
                    class_id=class_id, student=student, model=model,
                )
            # Past the gate, but the file now names a different theme than the
            # approved project. The change is ignored — switching an approved
            # project is the lecturer's call — and the student is told so once,
            # folded into this review rather than as a second comment.
            approved_slug = prof.get("theme_slug", "")
            if approved_slug and theme_slug(proposal["theme"]) != approved_slug:
                theme_notice = theme_change_notice(prof.get("theme", ""))

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
                format_comment(questions, model, findings_count=len(findings), notice=theme_notice),
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


async def handle_proposal(
    target: ReviewTarget,
    proposal: dict,
    *,
    client: GitHubClient,
    store: Store,
    class_id: str,
    student: str,
    model: str = MODEL,
) -> list[dict]:
    """Reserve a proposed theme, or tell the student why it can't be — one comment.

    The order is exact-then-semantic, cheap check first. The slug is the atomic
    lock on the same words; if it is already held, either it is this student's own
    earlier proposal (a re-push while pending — acknowledged silently, so a branch
    that keeps moving is not spammed) or a classmate's, which is a clash. Only a
    theme whose slug is free runs the model conflict check against the themes
    already claimed; a theme that survives both is reserved, pending the lecturer.

    Returns [] always — a proposal produces no review questions; the value keeps
    the same shape run_review returns so the webhook path does not branch on it.
    """
    theme, spec = proposal["theme"], proposal["spec"]
    slug = theme_slug(theme)
    if not slug:
        client.post_comment(target.repo, target.pr, format_theme_comment("unreadable"))
        return []

    profile = store.profile(student)

    # A theme the lecturer already rejected must not simply reappear on a re-push.
    # The bar is recorded on the student's profile; re-proposing the same words is
    # turned back rather than re-reserved and put back in the lecturer's queue.
    if slug in profile.get("rejected_themes", []):
        client.post_comment(target.repo, target.pr, format_theme_comment("previously_rejected"))
        return []

    # The student's own pending themes. Approved students never reach here (the
    # gate returns first), so these are all pending. If one already matches this
    # slug the file is unchanged since it was reserved — a re-push, acknowledged
    # in silence so a branch that keeps moving is not commented on every push.
    mine = [t for t in store.list_themes(class_id, status="pending") if t.get("student") == student]
    if any(t.get("slug") == slug for t in mine):
        return []

    # An exact-slug clash with a classmate — the atomic half, before the model.
    existing = store.get_theme(class_id, slug)
    if existing is not None and existing.get("student") != student:
        client.post_comment(target.repo, target.pr, format_theme_comment("taken"))
        return []

    # The semantic half: the same idea in different words as a classmate's theme.
    others = [t for t in store.list_themes(class_id) if t.get("student") != student]
    conflict = await theme_conflict(theme, spec, others, model=model)
    if conflict is not None:
        client.post_comment(target.repo, target.pr, format_theme_comment(
            "conflict", conflict_theme=conflict.get("theme", ""),
        ))
        return []  # their prior pending theme, if any, is left standing — B was rejected, keep A

    # The theme is clear. Reserve the new slug BEFORE releasing any earlier pending
    # proposal by this student — so a lost race on the new slug can never destroy a
    # valid reservation the student already held.
    if not store.reserve_theme(
        class_id, slug, student=student, theme=theme, spec=spec,
        repo=target.repo, pr=target.pr,
    ):
        # Lost the atomic race on the exact slug between the read above and here.
        # Re-read: if the winner is this student's own concurrent delivery it is
        # already reserved as theirs — stay silent; otherwise a classmate got it,
        # and any prior pending theme of this student is left untouched.
        won = store.get_theme(class_id, slug)
        if won is not None and won.get("student") == student:
            return []
        client.post_comment(target.repo, target.pr, format_theme_comment("taken"))
        return []

    # Reservation won — now it is safe to free any earlier pending proposal, so the
    # student holds exactly one and the abandoned slug does not block a classmate.
    for t in mine:
        if t.get("slug") != slug:
            store.release_theme(class_id, t["slug"])

    client.post_comment(target.repo, target.pr, format_theme_comment("reserved", theme=theme))
    return []


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
