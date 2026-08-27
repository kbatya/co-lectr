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
    def __init__(self, data=b""):
        self._data = data
        self.captured = None

    def read(self):
        return self._data

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
