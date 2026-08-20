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
    """Map top-level function/class name -> relative path of the file defining it."""
    found: dict[str, str] = {}
    for path in python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.setdefault(node.name, str(path.relative_to(root)).replace("\\", "/"))
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


def run_ruff(root: Path) -> list[Finding]:
    proc = subprocess.run(
        # --isolated: ignore any ruff config inside the student repo. The course
        # decides which rules apply, not the submission.
        [sys.executable, "-m", "ruff", "check", "--isolated", "--select", RUFF_RULES,
         "--output-format", "json", "--exit-zero", "."],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"ruff failed: {proc.stderr.strip()}")
    return [
        Finding(
            rule=f"ruff:{item['code']}",
            message=item["message"],
            path=item["filename"].replace("\\", "/").split("/")[-1] if item.get("filename") else ".",
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
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "--tb=no", "-rf", "tests"],
        cwd=root, capture_output=True, text=True, timeout=timeout,
    )
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
