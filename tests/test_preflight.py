#!/usr/bin/env python3
"""Checks for the non-destructive new-export intake report."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_edge_cases import row, write_xlsx  # noqa: E402


def inspect(path):
    result = subprocess.run([sys.executable, str(ROOT / "preflight.py"), str(path), "--json"], capture_output=True, text=True)
    return result.returncode, json.loads(result.stdout)


def main():
    with tempfile.TemporaryDirectory(prefix="preflight_test_") as tmp:
        tmp = Path(tmp)
        valid = tmp / "valid.xlsx"
        write_xlsx([row(1)], valid)
        code, report = inspect(valid)
        assert code == 0 and report["ready"], report
        assert report["rows"] == 1 and len(report["columns"]) == 20, report

        broken = tmp / "broken.csv"
        broken.write_text("unexpected column\nvalue\n", encoding="utf-8")
        code, report = inspect(broken)
        assert code == 1 and not report["ready"], report
        assert report["column_problems"], report
    print("2/2 preflight tests passed")


if __name__ == "__main__":
    main()
