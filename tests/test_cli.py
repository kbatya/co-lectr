"""The offline run reports one digest per class, not one across the folder."""

import sys
from pathlib import Path

from co_lectr import cli

SAMPLES = Path(__file__).parent.parent / "samples"


def test_digests_are_per_class(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["co-lectr", str(SAMPLES), "--no-tests", "--no-store"])
    cli.main()
    out = capsys.readouterr().out

    assert "Class 12a - 6 submission(s)" in out
    assert "Class 12b - 4 submission(s)" in out
    assert "4/6  ruff:E722" in out
    assert "3/4  ruff:E722" in out
    # 7/10 is what pooling the two classes together used to report.
    assert "7/10" not in out
