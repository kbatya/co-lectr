from pathlib import Path

from co_lectr.layer1 import analyse, check_syntax, defined_symbols

SAMPLES = Path(__file__).parent.parent / "samples"
REQUIRED = ("run_agent", "load_config")


def rules(root, **kw):
    return {f.rule for f in analyse(root, required_symbols=REQUIRED, run_tests=False, **kw)}


def test_bare_except_is_found():
    assert "ruff:E722" in rules(SAMPLES / "student_01")


def test_clean_submission_has_no_findings():
    assert rules(SAMPLES / "student_10") == set()


def test_missing_required_symbol_is_reported():
    assert "spec:missing-symbol" in rules(SAMPLES / "student_06")
    assert "spec:missing-symbol" not in rules(SAMPLES / "student_01")


def test_defined_symbols_maps_name_to_file():
    assert defined_symbols(SAMPLES / "student_01")["run_agent"] == "agent.py"


def test_syntax_error_is_a_finding(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    findings = check_syntax(tmp_path)
    assert [f.rule for f in findings] == ["ast:syntax-error"]


def test_student_ruff_config_cannot_disable_rules(tmp_path):
    (tmp_path / "agent.py").write_text("try:\n    pass\nexcept:\n    pass\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("lint.select = []\n", encoding="utf-8")
    assert "ruff:E722" in {f.rule for f in analyse(tmp_path, run_tests=False)}
