"""The GitHub client — the fetch and the post, both without touching the network.

`urlopen` is monkeypatched to a fake, so these pin the two things that would
otherwise only show up live: the tarball is unwrapped to its single root
directory, and a comment is POSTed as JSON to the pull request's issue endpoint.
"""

import io
import json
import tarfile

import pytest

from co_lectr import github
from co_lectr.github import GitHubClient


class FakeResp:
    def __init__(self, data=b"", headers=None):
        self._data = data
        self.headers = headers or {}
        self.captured = None

    def read(self, amt=None):
        return self._data if amt is None else self._data[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_tarball(files: dict[str, str], top: str = "kbatya-co-lectr-abc123") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_fetch_source_unwraps_to_the_single_root(monkeypatch, tmp_path):
    tarball = make_tarball({"agent.py": "x = 1\n", ".colectr/class.yml": "class: 12a\n"})
    monkeypatch.setattr(github, "urlopen", lambda req, *a, **k: FakeResp(tarball))

    root = GitHubClient("tok").fetch_source("kbatya/co-lectr", "abc123", tmp_path)

    assert root.name == "kbatya-co-lectr-abc123"
    assert (root / "agent.py").read_text() == "x = 1\n"
    assert (root / ".colectr" / "class.yml").is_file()


def test_post_comment_sends_json_to_the_issue_endpoint(monkeypatch):
    seen = {}

    def fake_urlopen(req, *a, **k):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.get_header("Authorization")
        return FakeResp(b"{}")

    monkeypatch.setattr(github, "urlopen", fake_urlopen)

    GitHubClient("tok").post_comment("kbatya/co-lectr", 7, "a question")

    assert seen["url"] == "https://api.github.com/repos/kbatya/co-lectr/issues/7/comments"
    assert seen["method"] == "POST"
    assert seen["body"] == {"body": "a question"}
    assert seen["auth"] == "Bearer tok"


def test_fetch_source_rejects_an_oversized_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(github, "urlopen", lambda req, *a, **k: FakeResp(
        b"", headers={"Content-Length": str(github.MAX_COMPRESSED + 1)}))
    with pytest.raises(RuntimeError, match="over the"):
        GitHubClient("tok").fetch_source("kbatya/co-lectr", "abc123", tmp_path)


def test_fetch_source_rejects_a_download_past_the_cap(monkeypatch, tmp_path):
    # No Content-Length, so the actual bytes read must catch it.
    monkeypatch.setattr(github, "MAX_COMPRESSED", 10)
    tarball = make_tarball({"agent.py": "x = 1\n" * 50})
    monkeypatch.setattr(github, "urlopen", lambda req, *a, **k: FakeResp(tarball))
    with pytest.raises(RuntimeError, match="download limit"):
        GitHubClient("tok").fetch_source("kbatya/co-lectr", "abc123", tmp_path)


def test_fetch_source_rejects_too_many_members(monkeypatch, tmp_path):
    monkeypatch.setattr(github, "MAX_MEMBERS", 1)
    tarball = make_tarball({"a.py": "x\n", "b.py": "y\n"})
    monkeypatch.setattr(github, "urlopen", lambda req, *a, **k: FakeResp(tarball))
    with pytest.raises(RuntimeError, match="members"):
        GitHubClient("tok").fetch_source("kbatya/co-lectr", "abc123", tmp_path)


def test_fetch_source_rejects_an_oversized_expansion(monkeypatch, tmp_path):
    monkeypatch.setattr(github, "MAX_EXTRACTED", 5)
    tarball = make_tarball({"agent.py": "x = 1\n" * 20})
    monkeypatch.setattr(github, "urlopen", lambda req, *a, **k: FakeResp(tarball))
    with pytest.raises(RuntimeError, match="extracts to"):
        GitHubClient("tok").fetch_source("kbatya/co-lectr", "abc123", tmp_path)


def test_fetch_source_rejects_a_path_that_escapes_the_destination(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo("../escape.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(github, "urlopen", lambda req, *a, **k: FakeResp(buf.getvalue()))

    with pytest.raises(tarfile.TarError):
        GitHubClient("tok").fetch_source("kbatya/co-lectr", "abc123", tmp_path)
    assert not (tmp_path.parent / "escape.py").exists()
