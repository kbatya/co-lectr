"""Layer 1 — deterministic facts about a submission.

Three tools, three kinds of fact:

* ``ast``    — what the student actually defined (structure, and whether the
               symbols the assignment asked for exist at all).
* ``ruff``   — lint findings.
* ``pytest`` — do the milestone's provided tests pass.

Every finding carries a stable ``rule`` id so the class aggregator can count
them exactly. Nothing here calls an LLM.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    rule: str  # stable id, e.g. "ruff:E722" or "spec:missing-symbol"
    message: str
    path: str  # relative to the submission root
    line: int
    tool: str  # "ast" | "ruff" | "pytest"

    def as_dict(self) -> dict:
        return asdict(self)


def python_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py")
        if not any(part in {".venv", "__pycache__", ".git"} for part in p.parts)
    )


# --- ast ---------------------------------------------------------------------

def defined_symbols(root: Path) -> dict[str, str]:
    """Map every function/class name to the relative path of the file defining it.

    `ast.walk`, not `tree.body`: a required symbol the student wrote as a method
    on a class - `class Agent: def run_agent(self): ...` - or inside an `if`
    still counts as defined, instead of being reported missing when it is right
    there. Test files are skipped, so a stub `def run_agent(): pass` under
    `tests/` cannot satisfy the spec on the student's behalf.
    """
    found: dict[str, str] = {}
    for path in python_files(root):
        if "tests" in path.relative_to(root).parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.setdefault(node.name, rel)
    return found


def check_syntax(root: Path) -> list[Finding]:
    findings = []
    for path in python_files(root):
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            findings.append(Finding(
                rule="ast:syntax-error",
                message=exc.msg or "syntax error",
                path=str(path.relative_to(root)).replace("\\", "/"),
                line=exc.lineno or 1,
                tool="ast",
            ))
    return findings


def check_required_symbols(root: Path, required: tuple[str, ...]) -> list[Finding]:
    """The assignment spec asked for these names. Are they there?"""
    defined = defined_symbols(root)
    return [
        Finding(
            rule="spec:missing-symbol",
            message=f"the assignment asks for `{name}`, which is not defined anywhere in the submission",
            path=".",
            line=0,
            tool="ast",
        )
        for name in required if name not in defined
    ]


# --- ruff --------------------------------------------------------------------

RUFF_RULES = "E,F,B"  # pycodestyle errors, pyflakes, bugbear


def _ruff_relpath(filename: str, root: Path) -> str:
    """Ruff reports an absolute path; make it relative to the submission root.

    Keeping the directory is what stops `src/agent.py` and `other/agent.py`
    collapsing to the same `agent.py` - which anchored a question to a path that
    is in no file, and which the read tool (resolving from the root) could not
    open either. Matches what `check_syntax` records.
    """
    try:
        return Path(os.path.relpath(filename, root)).as_posix()
    except ValueError:  # different drive on Windows - fall back to the bare name
        return Path(filename).name


def run_ruff(root: Path) -> list[Finding]:
    proc = subprocess.run(
        # --isolated: ignore any ruff config inside the student repo. The course
        # decides which rules apply, not the submission.
        # --ignore-noqa: --isolated covers config files but not inline noqa
        # suppression comments, and one of those would otherwise silently delete
        # a finding from the review and the class digest. The course decides.
        [sys.executable, "-m", "ruff", "check", "--isolated", "--ignore-noqa",
         "--select", RUFF_RULES, "--output-format", "json", "--exit-zero", "."],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"ruff failed: {proc.stderr.strip()}")
    return [
        Finding(
            rule=f"ruff:{item['code']}",
            message=item["message"],
            path=_ruff_relpath(item["filename"], root) if item.get("filename") else ".",
            line=(item.get("location") or {}).get("row", 0),
            tool="ruff",
        )
        for item in json.loads(proc.stdout or "[]")
        if item.get("code")
    ]


# --- pytest ------------------------------------------------------------------

def run_pytest(root: Path, timeout: int = 60) -> list[Finding]:
    """Run the milestone's tests.

    NOTE: this executes student code. Offline it runs on the lecturer's machine;
    in production it must only ever run inside the Cloud Run container.
    """
    if not (root / "tests").exists():
        return []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "--tb=no", "-rf", "tests"],
            cwd=root, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # A suite that never terminates - an infinite loop in the student's code -
        # must not take the whole review down with it. It is itself worth a question.
        return [Finding(
            rule="pytest:timeout",
            message=f"the test run did not finish within {timeout}s",
            path="tests", line=0, tool="pytest",
        )]
    findings = []
    for line in proc.stdout.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            status, _, rest = line.partition(" ")
            findings.append(Finding(
                rule=f"pytest:{status.lower()}",
                message=rest.strip(),
                path="tests",
                line=0,
                tool="pytest",
            ))
    # A non-zero exit that produced no FAILED/ERROR line - a collection error, a
    # usage error, or no tests collected - must be reported, not silently read as
    # a clean submission.
    if not findings and proc.returncode != 0:
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        message = tail[-1].strip() if tail else f"pytest exited with code {proc.returncode}"
        findings.append(Finding(
            rule="pytest:error",
            message=message[:200],
            path="tests", line=0, tool="pytest",
        ))
    return findings


# --- entry point -------------------------------------------------------------

def analyse(root: Path, required_symbols: tuple[str, ...] = (), run_tests: bool = True) -> list[Finding]:
    root = Path(root)
    findings = check_syntax(root)
    findings += check_required_symbols(root, required_symbols)
    findings += run_ruff(root)
    if run_tests:
        findings += run_pytest(root)
    return findings
