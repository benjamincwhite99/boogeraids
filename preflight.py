#!/usr/bin/env python3
"""Inspect a new survey export without changing the published output.

Usage:
    python3 preflight.py new_export.xlsx
    python3 preflight.py new_export.csv --json

The result answers one question before a full pipeline run: can this export be
processed with the current data contract? It never writes to ``output/``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from clean import ChangeLog, clean, load_raw, norm, validate_config
from themes import strip_comments

HERE = Path(__file__).resolve().parent


def column_check(raw, spec: dict) -> tuple[dict, list]:
    """Match configured columns, collecting problems instead of stopping early."""
    found, problems = {}, []
    normalized = {header: norm(header) for header in raw.columns}
    for key, rule in spec.items():
        if "exact" in rule:
            hits = [header for header, value in normalized.items() if value == rule["exact"].lower()]
        else:
            hits = [header for header, value in normalized.items() if all(part in value for part in rule["all"])]
        if len(hits) == 1:
            found[key] = hits[0]
        else:
            problems.append({"field": key, "matches": hits, "message": f"expected one matching column; found {len(hits)}"})
    return found, problems


def inspect(path: Path, config_path: Path = HERE / "cleaning_config.json") -> dict:
    """Return a JSON-safe, non-destructive compatibility report for one export."""
    cfg = strip_comments(json.loads(config_path.read_text(encoding="utf-8")))
    validate_config(cfg)
    try:
        raw, encoding = load_raw(path)
    except SystemExit as exc:
        return {"ready": False, "input": path.name, "errors": [str(exc)], "warnings": []}

    mapped, column_problems = column_check(raw, cfg["columns"])
    extras = [str(header) for header in raw.columns if header not in mapped.values()]
    report = {
        "ready": not column_problems and len(raw) > 0,
        "input": path.name,
        "encoding": encoding,
        "rows": int(len(raw)),
        "columns": {key: str(value) for key, value in mapped.items()},
        "column_problems": column_problems,
        "extra_columns": extras,
        "warnings": [],
        "unmapped_values": [],
        "flag_counts": {},
    }
    if len(raw) == 0:
        report["errors"] = ["The export contains no responses."]
        return report
    if column_problems:
        report["errors"] = ["Required survey columns could not be mapped. Update cleaning_config.json before running the pipeline."]
        return report

    try:
        cleaned, meta = clean(raw, cfg, ChangeLog())
    except (KeyError, ValueError, TypeError) as exc:
        report.update(ready=False, errors=[f"The current rules could not inspect this export: {exc}"])
        return report

    report["unmapped_values"] = meta["unmapped"]
    report["flag_counts"] = {
        field: int(cleaned[field].sum())
        for field in cleaned.columns if field.startswith("flag_") and int(cleaned[field].sum())
    }
    if extras:
        report["warnings"].append(f"{len(extras)} extra column(s) will not be used by the current dashboard.")
    if meta["unmapped"]:
        report["warnings"].append("Some labels are not covered by the current configuration; they would be retained, blanked where needed, and flagged for review.")
    report["errors"] = []
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="new Microsoft Forms export (.xlsx or .csv)")
    parser.add_argument("--config", default=str(HERE / "cleaning_config.json"))
    parser.add_argument("--json", action="store_true", help="print the complete report as JSON")
    args = parser.parse_args(argv)
    report = inspect(Path(args.input), Path(args.config))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        state = "READY" if report["ready"] else "NOT READY"
        print(f"{state}: {report['input']}")
        print(f"Rows: {report.get('rows', 0):,}; mapped columns: {len(report.get('columns', {}))}")
        for error in report["errors"]:
            print(f"ERROR: {error}")
        for warning in report["warnings"]:
            print(f"WARNING: {warning}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
