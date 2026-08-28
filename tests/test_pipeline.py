"""The seam — fetch, review, post — wired together with fakes.

The fetch and the model are stubbed, so these pin the orchestration itself: a
comment is posted with the questions, and a commit already reviewed is skipped
without a second post (GitHub retries deliveries).
"""

import asyncio
from pathlib import Path

from co_lectr import pipeline
from co_lectr.pipeline import format_comment, run_review
from co_lectr.web import ReviewTarget

TARGET = ReviewTarget(repo="kbatya/co-lectr", pr=7, sha="deadbeef", student="ada-lovelace")


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
