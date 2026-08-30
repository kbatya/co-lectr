"""The seam — fetch, review, post — wired together with fakes.

The fetch and the model are stubbed, so these pin the orchestration itself: a
comment is posted with the questions, and a commit already reviewed is skipped
without a second post (GitHub retries deliveries).
"""

import asyncio
from pathlib import Path

from co_lectr import pipeline
from co_lectr.pipeline import format_comment, run_review
from co_lectr.store import theme_slug
from co_lectr.web import ReviewTarget

TARGET = ReviewTarget(repo="kbatya/co-lectr", pr=7, sha="deadbeef", student="ada-lovelace")

CHESS = theme_slug("A chess engine")
GO = theme_slug("A go engine")


class FakeClient:
    def __init__(self, files):
        self.files = files
        self.posted = []

    def fetch_source(self, repo, sha, dest):
        root = Path(dest) / "repo-root"
        root.mkdir()
        for name, content in self.files.items():
            (root / name).write_text(content)
        return root

    def post_comment(self, repo, pr, body):
        self.posted.append((repo, pr, body))


def test_format_comment_lists_each_question_anchored_to_a_line():
    body = format_comment(
        [{"path": "agent.py", "line": 14, "question": "what happens to the error?"}],
        "gemini-3.5-flash",
    )
    assert "`agent.py:14`" in body
    assert "what happens to the error?" in body
    assert "I don't fix" in body


def test_format_comment_says_so_when_there_is_nothing_to_ask():
    assert "Nothing to ask" in format_comment([], "gemini-3.5-flash")


def test_format_comment_does_not_claim_clean_when_layer1_found_something():
    # Empty questions but non-zero findings: the comment must not tell the student
    # layer 1 found nothing - it did; the model just raised nothing in scope.
    body = format_comment([], "gemini-3.5-flash", findings_count=3)
    assert "Layer 1 flagged 3" in body
    assert "clean pass" not in body


def test_run_review_fetches_reviews_and_posts_one_comment(monkeypatch):
    client = FakeClient({"agent.py": "x = 1\n"})
    monkeypatch.setattr(pipeline, "analyse", lambda root, required_symbols=(), run_tests=False: [])
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")

    async def fake_review(root, findings, chapters, recurring):
        return [{"path": "agent.py", "line": 1, "rule": "", "question": "why bare except?"}]

    monkeypatch.setattr(pipeline, "review", fake_review)

    questions = asyncio.run(run_review(TARGET, client=client, chapters=["ch1"], required=[]))

    assert len(questions) == 1
    assert len(client.posted) == 1
    repo, pr, body = client.posted[0]
    assert (repo, pr) == ("kbatya/co-lectr", 7)
    assert "why bare except?" in body


def test_run_review_skips_a_commit_another_delivery_already_claimed(monkeypatch):
    # reserve() returns False when the review id is already taken — the losing
    # delivery must not fetch, review or post.
    client = FakeClient({"agent.py": "x = 1\n"})

    class FakeStore:
        def reserve(self, rid, *, student, milestone):
            return False

    questions = asyncio.run(
        run_review(TARGET, client=client, chapters=[], required=[], store=FakeStore())
    )

    assert questions == []
    assert client.posted == []


def test_run_review_releases_the_claim_when_the_review_fails_before_posting(monkeypatch):
    # A failure before the comment goes out must hand the claim back so a
    # redelivery can retry — but nothing was posted, so no duplicate risk.
    client = FakeClient({"agent.py": "x = 1\n"})
    monkeypatch.setattr(pipeline, "analyse", lambda root, required_symbols=(), run_tests=False: [])
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")

    async def boom(root, findings, chapters, recurring):
        raise RuntimeError("gemini fell over")

    monkeypatch.setattr(pipeline, "review", boom)

    class FakeStore:
        def __init__(self):
            self.released = []

        def reserve(self, rid, *, student, milestone):
            return True

        def class_config(self, class_id):
            return {}

        def profile(self, student):
            return {}

        def release(self, rid):
            self.released.append(rid)

    store = FakeStore()
    try:
        asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))
    except RuntimeError:
        pass

    assert store.released and client.posted == []


def test_run_review_records_the_pr_author_as_the_student_not_the_repo_owner(monkeypatch):
    # The repo owner is the course org (kbatya); the student is the PR author.
    client = FakeClient({"agent.py": "x = 1\n"})
    monkeypatch.setattr(pipeline, "analyse", lambda root, required_symbols=(), run_tests=False: [])
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")

    async def fake_review(root, findings, chapters, recurring):
        return []

    monkeypatch.setattr(pipeline, "review", fake_review)

    class RecordingStore:
        def __init__(self):
            self.recorded = {}

        def reserve(self, rid, *, student, milestone):
            return True

        def class_config(self, class_id):
            return {}

        def profile(self, student):
            return {}

        def record(self, **kw):
            self.recorded = kw

    store = RecordingStore()
    asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert store.recorded["student"] == "ada-lovelace"


def test_run_review_posts_the_comment_before_recording(monkeypatch):
    # The comment must go out before the review is recorded: recording first, then
    # failing to post, would mark the commit reviewed and never comment.
    order = []
    client = FakeClient({"agent.py": "x = 1\n"})
    real_post = client.post_comment
    client.post_comment = lambda repo, pr, body: (order.append("post"), real_post(repo, pr, body))
    monkeypatch.setattr(pipeline, "analyse", lambda root, required_symbols=(), run_tests=False: [])
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")

    async def fake_review(root, findings, chapters, recurring):
        return []

    monkeypatch.setattr(pipeline, "review", fake_review)

    class OrderStore:
        def reserve(self, rid, *, student, milestone):
            return True

        def class_config(self, class_id):
            return {}

        def profile(self, student):
            return {}

        def record(self, **kw):
            order.append("record")

    asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=OrderStore()))

    assert order == ["post", "record"]


def test_run_review_uses_per_class_config_over_the_env_defaults(monkeypatch):
    # The class doc's chapters/required/milestone override the service-wide env
    # defaults, so two classes at different chapters review correctly.
    client = FakeClient({"agent.py": "x = 1\n"})
    seen = {}
    monkeypatch.setattr(
        pipeline, "analyse",
        lambda root, required_symbols=(), run_tests=False: seen.setdefault("required", required_symbols) or [],
    )
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12b")

    async def fake_review(root, findings, chapters, recurring):
        seen["chapters"] = chapters
        return []

    monkeypatch.setattr(pipeline, "review", fake_review)

    class ConfiguredStore:
        def class_config(self, class_id):
            return {"chapters": ["ch9 advanced"], "required": ["solve"], "milestone": "ch9"}

        def reserve(self, rid, *, student, milestone):
            seen["milestone"] = milestone
            return True

        def profile(self, student):
            return {}

        def record(self, **kw):
            seen["recorded_milestone"] = kw["milestone"]

    asyncio.run(run_review(
        TARGET, client=client, chapters=["ch1"], required=["old"],
        milestone="pilot", store=ConfiguredStore(),
    ))

    assert seen["chapters"] == ["ch9 advanced"]
    assert seen["required"] == ("solve",)
    assert seen["milestone"] == "ch9"
    assert seen["recorded_milestone"] == "ch9"


# --- the project theme gate -------------------------------------------------

class ThemeClient:
    """Like FakeClient, but writes the nested `.colectr/project.yml`."""

    def __init__(self, theme):
        self.theme = theme
        self.posted = []

    def fetch_source(self, repo, sha, dest):
        root = Path(dest) / "repo-root"
        (root / ".colectr").mkdir(parents=True)
        (root / ".colectr" / "project.yml").write_text(
            f"theme: {self.theme}\nspec: a spec\n", encoding="utf-8")
        (root / "agent.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def post_comment(self, repo, pr, body):
        self.posted.append((repo, pr, body))


class ThemeStore:
    """An in-memory stand-in for the theme registry, keyed by slug like Firestore.

    profile() drives the gate: a student is past it once their profile carries
    theme_status == "approved" (approve_theme would have written it).
    """

    def __init__(self, themes=None, profiles=None):
        self.themes = {t["slug"]: dict(t) for t in (themes or [])}  # slug -> doc
        self.profiles = profiles or {}   # student -> profile dict
        self.reserved = []
        self.released = []

    def profile(self, student):
        return self.profiles.get(student, {})

    def has_approved_theme(self, student):
        return self.profile(student).get("theme_status") == "approved"

    def get_theme(self, class_id, slug):
        return self.themes.get(slug)

    def list_themes(self, class_id, status=""):
        return [t for t in self.themes.values() if not status or t.get("status") == status]

    def reserve_theme(self, class_id, slug, *, student, theme, spec, repo="", pr=0):
        if slug in self.themes:
            return False
        self.themes[slug] = {"slug": slug, "student": student, "theme": theme,
                             "spec": spec, "status": "pending", "repo": repo, "pr": pr}
        self.reserved.append((slug, student, theme))
        return True

    def release_theme(self, class_id, slug):
        self.themes.pop(slug, None)
        self.released.append(slug)


def _no_conflict(monkeypatch):
    async def clear(theme, spec, existing, model=""):
        return None
    monkeypatch.setattr(pipeline, "theme_conflict", clear)


def test_a_unique_proposal_is_reserved_and_skips_code_review(monkeypatch):
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")
    _no_conflict(monkeypatch)
    client = ThemeClient("A chess engine")
    store = ThemeStore()

    questions = asyncio.run(run_review(TARGET, client=client, chapters=["ch1"], required=[], store=store))

    assert questions == []
    assert store.reserved and store.reserved[0][2] == "A chess engine"
    assert len(client.posted) == 1
    assert "reserved" in client.posted[0][2].lower()


def test_a_conflicting_theme_is_turned_back_and_not_reserved(monkeypatch):
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")

    async def clash(theme, spec, existing, model=""):
        return {"slug": "chess-engine", "theme": "A chess engine", "reason": "both chess AIs"}
    monkeypatch.setattr(pipeline, "theme_conflict", clash)

    client = ThemeClient("chess-playing AI")
    store = ThemeStore(themes=[{"slug": "chess-engine", "theme": "A chess engine",
                                "student": "someone", "status": "pending"}])

    asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert store.reserved == []
    assert "too close" in client.posted[0][2].lower()
    assert "A chess engine" in client.posted[0][2]


def test_the_same_theme_by_another_student_is_taken_without_a_model_call(monkeypatch):
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")

    async def boom(theme, spec, existing, model=""):
        raise AssertionError("the conflict check must not run on an exact-slug clash")
    monkeypatch.setattr(pipeline, "theme_conflict", boom)

    client = ThemeClient("A chess engine")
    store = ThemeStore(themes=[{"slug": CHESS, "student": "someone-else",
                                "theme": "A chess engine", "status": "pending"}])

    asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert store.reserved == []
    assert "already claimed this exact theme" in client.posted[0][2]


def test_a_students_own_pending_theme_is_acknowledged_without_a_repeat_comment(monkeypatch):
    # A re-push while the theme is pending (same slug) must not spam the PR.
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")
    client = ThemeClient("A chess engine")
    store = ThemeStore(themes=[{"slug": CHESS, "student": TARGET.student,
                                "theme": "A chess engine", "status": "pending"}])

    questions = asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert questions == []
    assert client.posted == []
    assert store.reserved == []


def test_revising_a_pending_theme_reserves_the_new_slug_then_releases_the_old(monkeypatch):
    # G2 + #1: editing project.yml to a new theme reserves the new slug FIRST, then
    # frees the old one — so the old slug ends up gone and a classmate is unblocked.
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")
    _no_conflict(monkeypatch)
    client = ThemeClient("A go engine")  # revised from the chess one below
    store = ThemeStore(themes=[{"slug": CHESS, "student": TARGET.student,
                                "theme": "A chess engine", "status": "pending"}])

    asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert store.released == [CHESS]                     # the old proposal is gone
    assert store.reserved and store.reserved[0][0] == GO
    assert CHESS not in store.themes                     # slug freed for classmates
    assert "reserved" in client.posted[0][2].lower()


def test_a_failed_revision_does_not_destroy_the_prior_reservation(monkeypatch):
    # #1: if the new slug is lost to a classmate's race, the student's existing
    # pending theme must be left intact — never released before the reserve wins.
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")
    _no_conflict(monkeypatch)
    client = ThemeClient("A go engine")

    class RaceLostStore(ThemeStore):
        def reserve_theme(self, class_id, slug, *, student, theme, spec, repo="", pr=0):
            # A classmate got GO first; the whole point is A must survive this.
            self.themes[slug] = {"slug": slug, "student": "someone-else", "status": "pending"}
            return False

    store = RaceLostStore(themes=[{"slug": CHESS, "student": TARGET.student,
                                   "theme": "A chess engine", "status": "pending"}])
    asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert store.released == []          # the old reservation was NOT released
    assert CHESS in store.themes         # student A still holds their chess theme
    assert "already claimed this exact theme" in client.posted[0][2]


def test_a_previously_rejected_theme_is_turned_back_not_re_reserved(monkeypatch):
    # #3: a rejected theme is recorded on the profile; re-pushing the same words is
    # refused rather than put back in the lecturer's queue.
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")

    async def boom(theme, spec, existing, model=""):
        raise AssertionError("a rejected theme must not reach the model check")
    monkeypatch.setattr(pipeline, "theme_conflict", boom)

    client = ThemeClient("A chess engine")
    store = ThemeStore(profiles={TARGET.student: {"rejected_themes": [CHESS]}})

    asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert store.reserved == []
    assert "turned this theme down" in client.posted[0][2].lower()


def test_own_concurrent_delivery_losing_the_reserve_race_stays_silent(monkeypatch):
    # G4: if the winner of the exact-slug race is this student's own concurrent
    # delivery, the loser must not post "a classmate claimed it".
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")
    _no_conflict(monkeypatch)
    client = ThemeClient("A chess engine")

    class RacingStore(ThemeStore):
        def reserve_theme(self, class_id, slug, *, student, theme, spec, repo="", pr=0):
            # Simulate a concurrent delivery by the same student winning first.
            self.themes[slug] = {"slug": slug, "student": student, "theme": theme,
                                 "status": "pending"}
            return False

    store = RacingStore()
    questions = asyncio.run(run_review(TARGET, client=client, chapters=[], required=[], store=store))

    assert questions == []
    assert client.posted == []  # silent — it is our own claim, not a clash


def test_an_approved_student_is_reviewed_not_gated(monkeypatch):
    # Once the theme is approved the project.yml stays in the repo but is past the
    # gate: the PR is reviewed as code, not handled as a proposal.
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")
    monkeypatch.setattr(pipeline, "analyse", lambda root, required_symbols=(), run_tests=False: [])

    async def fake_review(root, findings, chapters, recurring):
        return [{"path": "agent.py", "line": 1, "rule": "", "question": "why?"}]
    monkeypatch.setattr(pipeline, "review", fake_review)

    client = ThemeClient("A chess engine")  # matches the approved theme

    class ApprovedStore(ThemeStore):
        def class_config(self, class_id):
            return {}

        def reserve(self, rid, *, student, milestone):
            return True

        def record(self, **kw):
            pass

    store = ApprovedStore(profiles={TARGET.student: {
        "theme_status": "approved", "theme_slug": CHESS, "theme": "A chess engine"}})
    asyncio.run(run_review(TARGET, client=client, chapters=["ch1"], required=[], store=store))

    # A review comment (a question), not a theme comment, and no change notice.
    assert len(client.posted) == 1
    assert "why?" in client.posted[0][2]
    assert "Heads up" not in client.posted[0][2]


def test_an_approved_student_who_changed_their_theme_is_told_in_the_review(monkeypatch):
    # G3: an approved student whose project.yml now names a different theme is told
    # the change is ignored — folded into the review, not a second comment.
    monkeypatch.setattr(pipeline, "class_id_from", lambda root: "12a")
    monkeypatch.setattr(pipeline, "analyse", lambda root, required_symbols=(), run_tests=False: [])

    async def fake_review(root, findings, chapters, recurring):
        return [{"path": "agent.py", "line": 1, "rule": "", "question": "why?"}]
    monkeypatch.setattr(pipeline, "review", fake_review)

    client = ThemeClient("A go engine")  # differs from the approved chess theme

    class ApprovedStore(ThemeStore):
        def class_config(self, class_id):
            return {}

        def reserve(self, rid, *, student, milestone):
            return True

        def record(self, **kw):
            pass

    store = ApprovedStore(profiles={TARGET.student: {
        "theme_status": "approved", "theme_slug": CHESS, "theme": "A chess engine"}})
    asyncio.run(run_review(TARGET, client=client, chapters=["ch1"], required=[], store=store))

    assert len(client.posted) == 1  # one comment, both notice and questions
    body = client.posted[0][2]
    assert "Heads up" in body and "A chess engine" in body
    assert "why?" in body
