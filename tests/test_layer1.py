from pathlib import Path

from co_lectr.layer1 import analyse, check_syntax, defined_symbols, run_pytest, run_ruff

SAMPLES = Path(__file__).parent.parent / "samples"
REQUIRED = ("run_agent", "load_config")


def rules(root, **kw):
    return {f.rule for f in analyse(root, required_symbols=REQUIRED, run_tests=False, **kw)}


def test_bare_except_is_found():
    assert "ruff:E722" in rules(SAMPLES / "student_01")


def test_inline_noqa_does_not_hide_a_finding(tmp_path):
    # A one-word `# noqa` must not delete a finding from the review or the class
    # digest: --isolated covers config, --ignore-noqa covers inline suppressions.
    (tmp_path / "agent.py").write_text("import os  # noqa\n", encoding="utf-8")
    assert "ruff:F401" in {f.rule for f in run_ruff(tmp_path)}


def test_ruff_findings_keep_their_directory(tmp_path):
    # src/agent.py and other/agent.py must stay distinct, not both collapse to
    # `agent.py` and anchor a question to a path in no file.
    (tmp_path / "src" / "utils").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    (tmp_path / "src" / "utils" / "agent.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "other" / "agent.py").write_text("import sys\n", encoding="utf-8")
    paths = sorted(f.path for f in run_ruff(tmp_path))
    assert paths == ["other/agent.py", "src/utils/agent.py"]


def test_a_required_symbol_defined_as_a_method_counts(tmp_path):
    # A student who puts run_agent on a class has defined it - not a missing symbol.
    (tmp_path / "agent.py").write_text(
        "class Agent:\n    def run_agent(self):\n        pass\n", encoding="utf-8")
    assert "run_agent" in defined_symbols(tmp_path)


def test_a_stub_under_tests_does_not_satisfy_the_spec(tmp_path):
    # A stub in tests/ must not count as the student defining the required symbol.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def run_agent():\n    pass\n", encoding="utf-8")
    assert "run_agent" not in defined_symbols(tmp_path)


def test_a_hanging_test_suite_yields_a_timeout_finding(tmp_path):
    # An infinite loop must not take the whole review down - it becomes a finding.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_hang():\n    time.sleep(30)\n", encoding="utf-8")
    assert "pytest:timeout" in {f.rule for f in run_pytest(tmp_path, timeout=2)}


def test_a_collection_error_is_reported_not_read_as_clean(tmp_path):
    # A non-zero pytest exit with no FAILED line (here an import error) must not
    # be silently treated as a passing submission.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text(
        "import nonexistent_module_xyz\n\ndef test_x():\n    pass\n", encoding="utf-8")
    rules = {f.rule for f in run_pytest(tmp_path)}
    assert "pytest:error" in rules


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
