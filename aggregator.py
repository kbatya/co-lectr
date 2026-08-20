"""Class-level aggregation of layer-1 findings.

The digest is built from layer 1 only. Deterministic rule ids count exactly —
"17/28 students used a bare `except:`" is arithmetic, not an impression.
Layer 2 prose does not aggregate and is deliberately not used here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from co_lectr.layer1 import Finding


@dataclass(frozen=True)
class RuleCount:
    rule: str
    students: tuple[str, ...]  # who hit it, sorted
    occurrences: int  # total across the class, incl. repeats within one submission
    example: str  # one representative message

    @property
    def student_count(self) -> int:
        return len(self.students)


def digest(results: dict[str, list[Finding]]) -> list[RuleCount]:
    """results: student id -> their layer-1 findings. Most widespread rule first."""
    by_rule: dict[str, list[tuple[str, Finding]]] = defaultdict(list)
    for student, findings in results.items():
        for f in findings:
            by_rule[f.rule].append((student, f))

    counts = [
        RuleCount(
            rule=rule,
            students=tuple(sorted({student for student, _ in hits})),
            occurrences=len(hits),
            example=hits[0][1].message,
        )
        for rule, hits in by_rule.items()
    ]
    return sorted(counts, key=lambda c: (-c.student_count, -c.occurrences, c.rule))


def render(counts: list[RuleCount], class_size: int, threshold: int = 2) -> str:
    """Lecturer-facing text. Only rules hit by >= threshold students are reteach signal."""
    widespread = [c for c in counts if c.student_count >= threshold]
    if not widespread:
        return "No finding was shared by more than one student - nothing to reteach as a class."

    lines = ["Class digest - shared gaps, most widespread first", ""]
    for c in widespread:
        lines.append(f"  {c.student_count}/{class_size}  {c.rule}  - {c.example}")
        lines.append(f"           {', '.join(c.students)}")
    isolated = [c for c in counts if c.student_count < threshold]
    if isolated:
        lines += ["", f"({len(isolated)} further finding(s) hit one student each - individual feedback, not a class gap.)"]
    return "\n".join(lines)
