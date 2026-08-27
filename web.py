"""The public HTTPS endpoint — Cloud Run's half of the delivery path.

Design.md steps 3-4: a student opens a pull request, GitHub posts the event
here, and Co-Lectr reviews it. This module is only the front door. Its job is
narrow on purpose:

  GET  /         a health check, so a deploy can be verified with one curl.
  POST /webhook  a GitHub event — verified, then routed to a review target.

What arrives at /webhook is a GitHub payload, not student code, and it is
trusted only as far as its signature: every POST is checked against
GITHUB_WEBHOOK_SECRET with a constant-time compare before a single field is
read. An event that is not a pull request we act on, or an action we do not
review, is acknowledged and dropped — GitHub retries on anything but a 2xx, so
"nothing to do here" is still a success.

Fetching the PR head, running the two-layer review over it and posting the
questions back as review comments is the next step; it hangs off the
`ReviewTarget` this returns. Nothing in this file imports the review core, so
the endpoint stays cheap to start and simple to reason about.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import asdict, dataclass

from flask import Flask, jsonify, request

app = Flask(__name__)

# The pull-request actions worth a review. A new PR, a push to its branch, or a
# reopen all mean fresh head code; edits to the title, labels or reviewers do not.
REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened"}


@dataclass(frozen=True)
class ReviewTarget:
    """The one commit a review would run against, pulled out of the payload."""

    repo: str  # "owner/name"
    pr: int
    sha: str  # the PR head commit


def signature_ok(body: bytes, header: str | None, secret: str) -> bool:
    """Did this body really come from GitHub, signed with our shared secret?

    GitHub sends `sha256=<hex>` in X-Hub-Signature-256, an HMAC of the raw body.
    No secret configured means we cannot verify anything, so we refuse rather
    than wave the request through — an unsigned webhook is an open door to a
    model that influences a grade.
    """
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def review_target(payload: dict) -> ReviewTarget | None:
    """The PR this event asks us to review, or None if it is not one we act on.

    Reads only after the signature has been checked. Missing fields mean a
    payload shaped unlike a pull-request event, which is treated the same as an
    action we ignore: dropped, not crashed.
    """
    if payload.get("action") not in REVIEWABLE_ACTIONS:
        return None
    pr = payload.get("pull_request")
    repo = payload.get("repository")
    if not isinstance(pr, dict) or not isinstance(repo, dict):
        return None
    try:
        return ReviewTarget(repo=repo["full_name"], pr=pr["number"], sha=pr["head"]["sha"])
    except (KeyError, TypeError):
        return None


@app.get("/")
def health():
    """Liveness. A judge — or Cloud Run — can hit this with no credentials."""
    return jsonify(service="co-lectr", status="ok"), 200


@app.post("/webhook")
def webhook():
    if not signature_ok(
        request.get_data(),
        request.headers.get("X-Hub-Signature-256"),
        os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
    ):
        return jsonify(error="signature verification failed"), 401

    # Signature good — now the headers and body can be read.
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":  # GitHub's handshake when a webhook is first configured
        return jsonify(pong=True), 200
    if event != "pull_request":
        return jsonify(ignored=f"event:{event}"), 200

    target = review_target(request.get_json(silent=True) or {})
    if target is None:
        return jsonify(ignored="action"), 200

    # The seam (Design.md step 5). With a token configured, fetch the head, run
    # the two-layer review and post the questions — on a background thread, so a
    # slow Gemini call never holds GitHub's ~10s delivery open. Without a token
    # the receiver still acknowledges and names what would be reviewed.
    if os.environ.get("GITHUB_TOKEN"):
        dispatch_review(target)
    else:
        app.logger.info("would review %s PR#%s @ %s (no GITHUB_TOKEN set)",
                        target.repo, target.pr, target.sha)
    return jsonify(accepted=asdict(target)), 202


def dispatch_review(target: ReviewTarget) -> None:
    """Run the review off the request thread.

    The review core is imported lazily inside the worker so the health check and
    cold start never pay for ADK, Gemini or Firestore — the endpoint stays cheap
    to start, and only a real pull request pulls the heavy path in.
    """
    import threading

    def worker() -> None:
        import asyncio

        from co_lectr.pipeline import config_from_env, run_review
        try:
            asyncio.run(run_review(target, **config_from_env()))
        except Exception:
            app.logger.exception("review of %s PR#%s @ %s failed",
                                 target.repo, target.pr, target.sha)

    threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    # Local runs only. In the container gunicorn imports `co_lectr.web:app` and
    # binds $PORT itself — see the Dockerfile.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
