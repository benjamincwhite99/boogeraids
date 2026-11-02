#!/usr/bin/env python3
"""Tests for dashboard.py using a synthetic export (no real survey data).

    python3 tests/test_dashboard.py

Exits with status 1 if any test fails.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_edge_cases import row, write_xlsx  # noqa: E402

results = []
SECRET = "Zebra-Quokka private remark"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / script), *map(str, args)], capture_output=True, text=True)


def text_of(page: str) -> str:
    page = re.sub(r"<style>.*?</style>|<script>.*?</script>", "", page, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dashboard_test_"))
    try:
        p = run("dashboard.py", "--out", tmp / "none")
        check("no bundle -> clear message", p.returncode != 0 and "run_all.py" in p.stderr, p.stderr[-300:])

        # 30 responses: describe goes 0% -> 100% agree; 10 promoters, 20 passives -> NPS +33.
        rows = [row(i, pre_describe="Disagree", post_describe="Strongly agree", pre_identify="Agree", recommend=10 if i <= 10 else 8,
                    valuable=f"{SECRET} {i}" if i <= 12 else "anyone can call 24/7",
                    improve="How many people call 988?" if i <= 15 else "More interactive activities please")
                for i in range(1, 31)]
        write_xlsx(rows, tmp / "export.xlsx")
        out = tmp / "out"
        for script, args in [("clean.py", [tmp / "export.xlsx"]), ("validate_clean.py", [tmp / "export.xlsx"]), ("themes.py", [])]:
            c = run(script, *args, "--out", out)
            check(f"{script} runs on the synthetic export", c.returncode == 0, (c.stdout + c.stderr)[-400:])

        p = run("dashboard.py", "--out", out)
        page_path = out / "dashboard.html"
        check("dashboard.py runs end to end", p.returncode == 0 and page_path.exists(), p.stderr[-400:])
        page = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
        txt = text_of(page)
        check("headline: response count", "30 responses" in txt, txt[:600])
        check("headline: describe change computed from pairs (+100 points)",
              "Describe 988 and how it works: +100 points" in txt and "0% agreed before the training (recalled) and 100% after" in txt, txt[:900])
        check("headline: NPS (+33)", "NPS +33" in txt, txt[:600])
        check("students vs everyone else hidden when a group is under the minimum size", "rate the training far lower" not in txt)
        check("fonts embedded in the page", page.count("data:font/woff2;base64,") == 3)
        check("no comment text in the page", SECRET.lower() not in page.lower())
        check("self-contained: no external URLs", not re.search(r"(src|href)=[\"']?(https?:)?//", page))
        check("provisional banner while no rows are hand-checked", "Comment themes are provisional" in txt)
        check("no half-written temp file left", not (out / ".dashboard.html.tmp").exists())
        check("every chart has a table view", page.count("Show the numbers") == 7, str(page.count("Show the numbers")))

        # Changed cleaned file: refuse.
        csv = out / "clean_responses.csv"
        original = csv.read_bytes()
        csv.write_bytes(original + b"\n")
        p = run("dashboard.py", "--out", out)
        check("tampered clean_responses.csv -> refuses", p.returncode != 0 and "run_manifest.json" in p.stderr, p.stderr[-300:])
        csv.write_bytes(original)

        # Re-cleaning without re-running themes: refuse, so themes and figures can't come from different runs.
        c = run("clean.py", tmp / "export.xlsx", "--out", out)
        p = run("dashboard.py", "--out", out)
        check("themes from an older bundle -> refuses", c.returncode == 0 and p.returncode != 0 and "Re-run themes.py" in p.stderr,
              p.stderr[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    width = max(len(n) for n, _, _ in results)
    fails = 0
    for name, ok, detail in results:
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else detail}")
    print(f"\n{len(results) - fails}/{len(results)} dashboard tests passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
