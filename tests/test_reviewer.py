from pathlib import Path

from co_lectr.layer1 import Finding
from co_lectr.reviewer import (
    INSTRUCTION,
    REVIEW_POLICY,
    build_prompt,
    build_reviewer,
    parse_questions,
    repeat_rules,
)

SAMPLES = Path(__file__).parent.parent / "samples"


def finding(rule, line=1):
    return Finding(rule=rule, message=rule, path="agent.py", line=line, tool="ruff")


def test_parse_questions_tolerates_a_code_fence():
    raw = 'Sure:\n```json\n[{"path": "agent.py", "line": 14, "question": "What does a bare except catch?"}]\n```'
    assert parse_questions(raw)[0]["line"] == 14


def test_parse_questions_returns_empty_on_prose():
    assert parse_questions("I could not produce JSON.") == []


def test_submission_code_is_delimited_as_data():
    prompt = build_prompt(SAMPLES / "student_01", [], ["ch1"])
    assert prompt.index("LAYER-1 FINDINGS") < prompt.index("<student_submission>")
    assert prompt.rstrip().endswith("</student_submission>")


def test_a_rule_hit_again_is_named_as_a_repeat():
    prompt = build_prompt(SAMPLES / "student_01", [finding("ruff:E722")], ["ch1"], {"ruff:E722": 3})
    assert "PRIOR HISTORY" in prompt
    assert "ruff:E722: hit 3 time(s)" in prompt


def test_a_rule_the_student_fixed_is_not_brought_up():
    """The profile remembers it; this submission does not hit it. Nothing to ask about."""
    prompt = build_prompt(SAMPLES / "student_01", [finding("ruff:F401")], ["ch1"], {"ruff:E722": 3})
    assert "PRIOR HISTORY" not in prompt
    assert repeat_rules([finding("ruff:F401")], {"ruff:E722": 3}) == {}


def test_a_first_offence_carries_no_history():
    prompt = build_prompt(SAMPLES / "student_01", [finding("ruff:E722")], ["ch1"], {})
    assert "PRIOR HISTORY" not in prompt
    assert build_prompt(SAMPLES / "student_01", [finding("ruff:E722")], ["ch1"]) == prompt


def test_history_is_trusted_context_not_submitted_code():
    prompt = build_prompt(SAMPLES / "student_01", [finding("ruff:E722")], ["ch1"], {"ruff:E722": 3})
    assert prompt.index("PRIOR HISTORY") < prompt.index("<student_submission>")


def test_only_the_batch_reviewer_is_told_about_history():
    """agent.py shares REVIEW_POLICY but has no profile to read - it must not be
    invited to claim a history it cannot see."""
    assert "PRIOR HISTORY" in INSTRUCTION
    assert "PRIOR HISTORY" not in REVIEW_POLICY


def test_read_tool_refuses_paths_outside_the_submission():
    agent = build_reviewer(SAMPLES / "student_01")
    read = agent.tools[0].func if hasattr(agent.tools[0], "func") else agent.tools[0]
    assert "error" in read("../student_02/agent.py")
    assert "error" not in read("agent.py")
