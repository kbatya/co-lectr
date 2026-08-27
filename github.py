"""The two GitHub REST calls the delivery path needs: fetch a PR's code, and
post the review back as one comment.

Only two operations, both small, so this uses `urllib` from the standard library
rather than adding a dependency. Auth is a fine-grained personal access token
(contents: read, pull requests: write) — enough for the pilot on a throwaway
repo. A GitHub App is the scale answer (Design.md step 5) and would slot in
behind these same two methods without the caller changing.

The tarball is student-controlled input, so it is extracted with tarfile's
`data` filter: an archive member that tries to escape the destination is
rejected rather than written outside it.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

API = "https://api.github.com"


@dataclass
class GitHubClient:
    token: str
    api: str = API

    def _open(self, req: Request):
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        return urlopen(req)

    def fetch_source(self, repo: str, sha: str, dest: Path) -> Path:
        """Download the repo tarball at `sha` and extract it under `dest`.

        Returns the single top-level directory GitHub wraps the archive in
        (`owner-repo-<sha>/`). That directory is the submission root the review
        runs over — `.colectr/class.yml` and all.
        """
        url = f"{self.api}/repos/{repo}/tarball/{sha}"
        with self._open(Request(url)) as resp:
            raw = resp.read()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            tar.extractall(dest, filter="data")
        roots = [p for p in Path(dest).iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise RuntimeError(f"expected one top-level dir in the tarball, got {roots}")
        return roots[0]

    def post_comment(self, repo: str, pr: int, body: str) -> None:
        """Add one issue comment to the pull request (questions, not fixes)."""
        url = f"{self.api}/repos/{repo}/issues/{pr}/comments"
        req = Request(url, data=json.dumps({"body": body}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        with self._open(req):
            pass
