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

TARGET = ReviewTarget(repo="kbatya/co-lectr", pr=7, sha="deadbeef")


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


def test_run_review_skips_a_commit_already_reviewed(monkeypatch):
    client = FakeClient({"agent.py": "x = 1\n"})

    class FakeStore:
        def has_review(self, rid):
            return True

    questions = asyncio.run(
        run_review(TARGET, client=client, chapters=[], required=[], store=FakeStore())
    )

    assert questions == []
    assert client.posted == []
