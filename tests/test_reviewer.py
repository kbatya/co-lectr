from pathlib import Path

from co_lectr.reviewer import build_prompt, build_reviewer, parse_questions

SAMPLES = Path(__file__).parent.parent / "samples"


def test_parse_questions_tolerates_a_code_fence():
    raw = 'Sure:\n```json\n[{"path": "agent.py", "line": 14, "question": "What does a bare except catch?"}]\n```'
    assert parse_questions(raw)[0]["line"] == 14


def test_parse_questions_returns_empty_on_prose():
    assert parse_questions("I could not produce JSON.") == []


def test_submission_code_is_delimited_as_data():
    prompt = build_prompt(SAMPLES / "student_01", [], ["ch1"])
    assert prompt.index("LAYER-1 FINDINGS") < prompt.index("<student_submission>")
    assert prompt.rstrip().endswith("</student_submission>")


def test_read_tool_refuses_paths_outside_the_submission():
    agent = build_reviewer(SAMPLES / "student_01")
    read = agent.tools[0].func if hasattr(agent.tools[0], "func") else agent.tools[0]
    assert "error" in read("../student_02/agent.py")
    assert "error" not in read("agent.py")
