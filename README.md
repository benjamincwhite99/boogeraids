# EveryMind 988 Survey: Cleaning Pipeline

Turns a raw Microsoft Forms export of the "Understanding 988" post-training survey into an analysis-ready dataset and a data-quality report.

## Data not included

The survey export, case materials and all outputs are **not** in this repository (see `.gitignore`), because the comments contain personal information. To run it:

1. Put the export (e.g. `BuildFest_EveryMind_Survey_Data_FULL.xlsx`) in this folder.
2. Copy `redaction_names.example.json` to `redaction_names.local.json` and list any real people's names found in comments. This file is gitignored.

## Run it

```bash
pip install pandas openpyxl
python3 clean.py                       # cleans BuildFest_EveryMind_Survey_Data_FULL.xlsx
python3 clean.py new_export.xlsx       # any newer export (.xlsx or .csv); column order doesn't matter
python3 validate_clean.py              # independent check of the output against the raw file
```

Outputs go to `output/`:

| File | What it is |
|---|---|
| `clean_responses.csv` / `.xlsx` | One row per response. Cleaned values, derived scores, `flag_*` columns. The XLSX adds a data dictionary, change log, flag summary and the untouched raw export. |
| `change_log.csv` | Every raw → cleaned value mapping with counts and the rule applied. |
| `group_tables.csv` | Every group comparison in the report in long format (group, members, n, value, suppression status). Ready for a dashboard. |
| `data_quality_report.md` | Issues found, what was done, completeness, sample composition, form-version evidence, and baseline figures with sample sizes. |

## Rules the pipeline follows

- **The source file is never modified. No rows dropped, no answers imputed.** Rows in = rows out. Blanks stay blank.
- **Doubtful responses are flagged, never excluded by default.** Primary figures use every valid answer. Review flags (possible scale reversal, duplicates, repeated IDs, very short or long completions) feed a side-by-side sensitivity check (`in_sensitivity_sample`).
- **Ambiguous rating labels ("Very agree", "Satisfied") are left unscored** until EveryMind confirms their meaning. The report counts them and shows what changes if they're scored.
- **Self-rated change is reported item by item**, with valid pairs only, and described as retrospective self-report, not measured learning.
- **Small groups:** groups under 10 are pooled into "Other (…)", cells under 10 are suppressed with complementary suppression (nothing recoverable by subtraction), and groups of 10–29 are labeled "thin".
- **Comments are never reworded.** The exact export text is in `*_original`, response types are in `*_type`, and only the `*_redacted` columns should be shared outside EveryMind.

## When a new export has something unexpected

If a new label, county or role shows up, the script prints a warning. It leaves the value blank and flags the row (`flag_unmapped_value`), and the report lists it in §2a. Add the value to `cleaning_config.json` and re-run. No code changes are needed.

## Who runs it and when

Program staff can run it after each batch of trainings (e.g. monthly) with Python installed. Review §2/§2a of the report for new issues, then use `clean_responses.csv` for analysis or the dashboard.
