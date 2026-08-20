from pathlib import Path

from co_lectr.aggregator import digest, render
from co_lectr.layer1 import Finding, analyse

SAMPLES = Path(__file__).parent.parent / "samples"


def finding(rule, student_file="a.py"):
    return Finding(rule=rule, message=f"msg for {rule}", path=student_file, line=1, tool="ruff")


def test_digest_orders_by_number_of_students():
    results = {
        "s1": [finding("ruff:E722"), finding("ruff:F401")],
        "s2": [finding("ruff:E722")],
        "s3": [finding("ruff:E722")],
    }
    counts = digest(results)
    assert counts[0].rule == "ruff:E722"
    assert counts[0].student_count == 3
    assert counts[0].students == ("s1", "s2", "s3")


def test_occurrences_counts_repeats_within_one_submission():
    counts = digest({"s1": [finding("ruff:E722"), finding("ruff:E722")]})
    assert counts[0].student_count == 1
    assert counts[0].occurrences == 2


def test_render_hides_findings_below_threshold():
    text = render(digest({"s1": [finding("ruff:F841")]}), class_size=1, threshold=2)
    assert "ruff:F841" not in text


def test_planted_mistake_surfaces_across_the_sample_class():
    results = {
        p.name: analyse(p, required_symbols=("run_agent", "load_config"), run_tests=False)
        for p in sorted(SAMPLES.iterdir()) if p.is_dir()
    }
    top = digest(results)[0]
    assert top.rule == "ruff:E722"
    assert top.student_count == 7
    assert "7/10" in render(digest(results), class_size=len(results))
