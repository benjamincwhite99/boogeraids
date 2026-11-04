#!/usr/bin/env python3
"""Run the whole pipeline in one command: clean -> validate -> themes -> dashboard.

Usage
-----
    python3 run_all.py                                 # uses the default export in this folder
    python3 run_all.py path/to/new_export.xlsx         # a newer Microsoft Forms export (.xlsx or .csv)
    python3 run_all.py export.xlsx --config cleaning_config.json --out output
    python3 run_all.py --tests                         # run the synthetic test suites first

Stops at the first step that fails and exits with that step's status. If validation fails, the cleaned
bundle in --out has already been published but must not be used until the problem is fixed and the run
repeated; themes and the dashboard are not regenerated from it.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "BuildFest_EveryMind_Survey_Data_FULL.xlsx"


def step(name, args):
    print(f"\n=== {name} ===", flush=True)
    t = time.time()
    rc = subprocess.run([sys.executable, *map(str, args)], cwd=HERE).returncode
    if rc != 0:
        print(f"\n{name} FAILED (exit {rc}). Later steps were not run.", file=sys.stderr)
        if name == "Validate":
            print("Do not use the files in the output folder until validation passes.", file=sys.stderr)
        sys.exit(rc)
    print(f"--- {name} OK ({time.time() - t:.1f}s)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", default=str(DEFAULT_INPUT))
    ap.add_argument("--config", default=str(HERE / "cleaning_config.json"))
    ap.add_argument("--out", default=str(HERE / "output"))
    ap.add_argument("--tests", action="store_true", help="run the synthetic test suites in tests/ first")
    a = ap.parse_args(argv)

    if a.tests:
        step("Cleaning tests", [HERE / "tests" / "test_edge_cases.py"])
        step("Theme tests", [HERE / "tests" / "test_themes.py"])
        step("Intervention tests", [HERE / "tests" / "test_interventions.py"])
        step("Dashboard tests", [HERE / "tests" / "test_dashboard.py"])
    step("Clean", [HERE / "clean.py", a.input, "--config", a.config, "--out", a.out])
    step("Validate", [HERE / "validate_clean.py", a.input, "--config", a.config, "--out", a.out])
    step("Themes", [HERE / "themes.py", "--config", a.config, "--out", a.out])
    step("Dashboard", [HERE / "dashboard.py", "--config", a.config, "--out", a.out])
    print(f"\nAll steps passed. Open {Path(a.out) / 'dashboard.html'} in a browser.\n"
          f"Reports: {Path(a.out) / 'data_quality_report.md'} and {Path(a.out) / 'themes' / 'themes_report.md'}")


if __name__ == "__main__":
    main()
