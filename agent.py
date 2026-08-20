"""Co-Lectr's root agent - what `adk web` and `adk run` load.

The lecturer talks to this one: "review student_03", "what did the class get
wrong?". It calls the same layer-1 checks the batch CLI uses, so the facts it
reasons about are computed by ruff/ast/pytest, not by the model.

Batch reviews over a whole folder go through co_lectr.cli instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.adk.agents import LlmAgent

from co_lectr.aggregator import digest, render
from co_lectr.layer1 import analyse
from co_lectr.reviewer import MODEL, REVIEW_POLICY

# Every tool path is resolved under this root and checked against it. Point it at
# the real course checkout with COLECTR_SUBMISSIONS_ROOT.
SUBMISSIONS_ROOT = Path(
    os.environ.get("COLECTR_SUBMISSIONS_ROOT", Path(__file__).parent / "samples")
).resolve()

INSTRUCTION = REVIEW_POLICY + """
HOW YOU WORK
You are talking to the lecturer, not to the student. Your tools:

- list_submissions() - which students have submitted.
- run_checks(submission, required_symbols) - the layer-1 facts for one student. Call this before
  reviewing anyone. Never review from the code alone.
- read_file(submission, path) - the code around a finding, so your question names what is actually
  there.
- class_digest(required_symbols) - what the whole class got wrong, counted exactly.

If the lecturer names a student, run the checks, read what you need, then give your questions.
If they ask about the class, use class_digest and say which gap is worth reteaching and why.
If they have not said which chapters have been taught, ask before you review - rule 2 depends on it.

OUTPUT
Plain readable text for the lecturer. Each question on its own line, prefixed with the file and
line it refers to, like `agent.py:14 - <question>`. These are the questions that would be posted to
the student's pull request, so they must be ready to send as they stand.
"""


def _submission_dir(submission: str) -> Path | None:
    target = (SUBMISSIONS_ROOT / submission).resolve()
    if not target.is_relative_to(SUBMISSIONS_ROOT) or not target.is_dir():
        return None
    return target


def _symbols(required_symbols: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in required_symbols.split(",") if s.strip())


def list_submissions() -> dict:
    """List the student submissions available to review.

    Returns:
        dict with "submissions": the student folder names.
    """
    return {"submissions": sorted(p.name for p in SUBMISSIONS_ROOT.iterdir() if p.is_dir())}


def run_checks(submission: str, required_symbols: str = "") -> dict:
    """Run the deterministic layer-1 checks (ast, ruff) on one student's submission.

    Args:
        submission: the student's folder name, e.g. "student_03".
        required_symbols: comma-separated names the assignment asks for, e.g. "run_agent,load_config".

    Returns:
        dict with "submission" and "findings", or "error" if there is no such submission.
    """
    root = _submission_dir(submission)
    if root is None:
        return {"error": f"no submission called {submission}"}
    # run_tests=False: pytest executes student code, which only happens inside the
    # Cloud Run container, never in the lecturer's dev UI.
    findings = analyse(root, required_symbols=_symbols(required_symbols), run_tests=False)
    return {"submission": submission, "findings": [f.as_dict() for f in findings]}


def read_file(submission: str, path: str, start_line: int = 1, end_line: int = 400) -> dict:
    """Read part of a file from one student's submission.

    Args:
        submission: the student's folder name, e.g. "student_03".
        path: file path inside that submission, e.g. "agent.py".
        start_line: first line to return (1-based).
        end_line: last line to return.

    Returns:
        dict with "path" and "text", or "error" if the file is not in that submission.
    """
    root = _submission_dir(submission)
    if root is None:
        return {"error": f"no submission called {submission}"}
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return {"error": f"{path} is not a file inside {submission}"}
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"path": path, "text": "\n".join(lines[max(0, start_line - 1):end_line])}


def class_digest(required_symbols: str = "") -> dict:
    """Count what the whole class got wrong, from the layer-1 findings.

    Args:
        required_symbols: comma-separated names the assignment asks for.

    Returns:
        dict with "class_size" and "digest", a ranked text summary of the shared gaps.
    """
    submissions = sorted(p for p in SUBMISSIONS_ROOT.iterdir() if p.is_dir())
    results = {
        p.name: analyse(p, required_symbols=_symbols(required_symbols), run_tests=False)
        for p in submissions
    }
    return {"class_size": len(results), "digest": render(digest(results), class_size=len(results))}


root_agent = LlmAgent(
    name="co_lectr",
    model=MODEL,
    description="Reviews student Python submissions with questions, and reports what the class got wrong.",
    instruction=INSTRUCTION,
    tools=[list_submissions, run_checks, read_file, class_digest],
)
