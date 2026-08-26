"""The webhook receiver.

Signature verification is a security control, so it is tested from both sides:
a body signed with the wrong secret is refused, and only the exact HMAC is let
through. The routing tests pin what counts as a reviewable event and what is
dropped, since GitHub retries anything that is not a 2xx.
"""

import hashlib
import hmac
import json

import pytest

from co_lectr.web import ReviewTarget, app, review_target, signature_ok

SECRET = "shh-its-a-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    return app.test_client()


def post(client, payload: dict, event: str = "pull_request", secret: str = SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        data=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sign(body, secret)},
        content_type="application/json",
    )


def pr_event(action="opened", repo="kbatya/co-lectr", number=7, sha="abc123"):
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "head": {"sha": sha}},
    }


# --- signature ---------------------------------------------------------------

def test_a_correct_signature_passes():
    body = b'{"hello": "world"}'
    assert signature_ok(body, sign(body), SECRET) is True


def test_the_wrong_secret_is_refused():
    body = b'{"hello": "world"}'
    assert signature_ok(body, sign(body, "not-the-secret"), SECRET) is False


def test_no_secret_configured_refuses_everything():
    body = b"{}"
    assert signature_ok(body, sign(body), "") is False


def test_a_missing_signature_header_is_refused():
    assert signature_ok(b"{}", None, SECRET) is False


# --- routing -----------------------------------------------------------------

def test_the_health_check_answers_without_credentials():
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_an_unsigned_webhook_is_rejected(client):
    resp = client.post("/webhook", data=b"{}", headers={"X-GitHub-Event": "ping"})
    assert resp.status_code == 401


def test_a_body_signed_with_the_wrong_secret_is_rejected(client):
    resp = post(client, pr_event(), secret="wrong")
    assert resp.status_code == 401


def test_a_ping_is_answered(client):
    body = b"{}"
    resp = client.post(
        "/webhook", data=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    assert resp.get_json()["pong"] is True


def test_a_reviewable_pr_is_accepted_with_its_target(client):
    resp = post(client, pr_event(action="opened", number=7, sha="deadbeef"))
    assert resp.status_code == 202
    assert resp.get_json()["accepted"] == {
        "repo": "kbatya/co-lectr", "pr": 7, "sha": "deadbeef",
    }


def test_an_action_we_do_not_review_is_dropped(client):
    resp = post(client, pr_event(action="labeled"))
    assert resp.status_code == 200
    assert resp.get_json()["ignored"] == "action"


def test_an_event_that_is_not_a_pull_request_is_dropped(client):
    body = json.dumps({"zen": "..."}).encode()
    resp = client.post(
        "/webhook", data=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ignored"] == "event:push"


# --- review_target parsing ---------------------------------------------------

@pytest.mark.parametrize("action", sorted({"opened", "synchronize", "reopened"}))
def test_review_target_is_built_for_reviewable_actions(action):
    target = review_target(pr_event(action=action))
    assert target == ReviewTarget(repo="kbatya/co-lectr", pr=7, sha="abc123")


@pytest.mark.parametrize("payload", [
    {"action": "closed", "repository": {"full_name": "r"}, "pull_request": {"number": 1, "head": {"sha": "x"}}},
    {"action": "opened"},                                    # no pull_request / repository
    {"action": "opened", "repository": {}, "pull_request": {}},  # shaped wrong
    {},                                                      # empty
])
def test_review_target_is_none_when_there_is_nothing_to_review(payload):
    assert review_target(payload) is None
