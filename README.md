# EveryMind 988 Survey: Cleaning Pipeline

Turns a raw Microsoft Forms export of the "Understanding 988" post-training survey into an analysis-ready dataset and a data-quality report.

## Data not included

The survey export, case materials and all outputs are **not** in this repository (see `.gitignore`), because the comments contain personal information. To run it:

1. Put the export (e.g. `BuildFest_EveryMind_Survey_Data_FULL.xlsx`) in this folder.
2. Copy `redaction_names.example.json` to `redaction_names.local.json` and list any real people's names found in comments. This file is gitignored.

## Run it

```bash
pip install -r requirements.txt       # versions this was built and tested with (Python 3.9)
python3 run_all.py                     # everything below in order: clean -> validate -> themes; stops at the first failure
python3 run_all.py new_export.xlsx --tests   # a newer export, running the synthetic test suites first

# or one step at a time:
python3 clean.py                       # cleans BuildFest_EveryMind_Survey_Data_FULL.xlsx
python3 clean.py new_export.xlsx       # any newer export (.xlsx or .csv); column order doesn't matter
python3 validate_clean.py              # independent check of the output against the raw file (add --config if you used one)
python3 tests/test_edge_cases.py       # synthetic edge cases a future export might contain (no real data needed)
python3 themes.py                      # comment themes (see below)
```

**Reading the cleaned CSV:** use `pd.read_csv("output/clean_responses.csv", keep_default_na=False, na_values=[""])`.
pandas' default would turn literal answers such as "None" or "NA" into blanks.

Outputs go to `output/`:

| File | What it is |
|---|---|
| `clean_responses.csv` / `.xlsx` | One row per response. Cleaned values, derived scores, `flag_*` columns. The XLSX adds a data dictionary, change log, flag summary and the untouched raw export. |
| `change_log.csv` | Every raw → cleaned value mapping with counts and the rule applied. |
| `group_tables.csv` | Every group comparison in the report in long format (group, members, n, value, suppression status). Ready for a dashboard. |
| `data_quality_report.md` | Issues found, what was done, completeness, sample composition, response-coverage patterns, and baseline figures with sample sizes. |
| `run_manifest.json` | Which input, config and code produced this bundle, package versions, and a fingerprint (SHA-256) of every file above. |

**A run publishes all of these together or not at all.** Everything is built in a temporary folder first. The previous bundle is then moved aside and the new one moved in. If any step fails, including a single file move, the previous bundle is put back exactly as it was. `validate_clean.py` checks every file against `run_manifest.json`, so files from different runs can't be mixed unnoticed. Only one run can write to an output folder at a time (a `.clean.lock` file; a lock left by a killed run is cleared automatically). Other files you keep in `output/` (e.g. review notes) are left alone.

**Open `clean_responses.xlsx` in Excel, not the CSV.** The workbook stores all survey text as plain text, so a comment such as `=1+1` never becomes a formula. Characters an Excel file can't hold (e.g. a line break pasted from Word) appear as `�` there. The CSVs keep the exact original text for analysis in Python or R, but Excel may treat a CSV cell starting with `=` as a formula. The report (issue 13) counts any such comments.

**Inputs:** `.xlsx` or `.csv`. CSVs can be UTF-8 or Windows-1252, which is Excel's default "CSV" on Windows. The report records which one was used. Old `.xls` files need to be re-saved as `.xlsx`.

## Comment themes

```bash
python3 themes.py                      # after clean.py + validate_clean.py
python3 tests/test_themes.py           # synthetic tests for the theme rules
```

`themes.py` codes both open-ended questions with the keyword rules in `theme_codebook.json` and writes `output/themes/`:

| File | What it is |
|---|---|
| `comment_codes.csv` | One row per substantive comment: intent, themes, role group, county, session, redacted text. |
| `theme_summary.csv` | Every count in the report (long format), with denominators and suppression status. |
| `themes_report.md` | Theme tables by students vs everyone else, where the questions came from, example excerpts, uncoded comments, validation results. |
| `themes_manifest.json` | Which cleaned file, codebook and code produced it, plus a SHA-256 of every file. |

- **A comment can have several themes**, so columns don't add to 100%. Denominators are substantive comments (question topics use question comments).
- **The "improve" box gets an intent first** (question, no change, suggestion, unclear). In some sessions most people used it to ask questions instead of suggesting changes, so questions are reported separately.
- **It refuses to run on a cleaned file that doesn't match its `run_manifest.json`**, so themes always describe a complete, checked bundle.
- **Keyword rules are a first pass, so check them by hand.** The first run creates one file per reviewer: `output/theme_validation_sample_A.xlsx`, `_B`, `_C` (100 random comments per question each, fixed seed). 10 comments per question are in every file, to measure how far reviewers agree; the rest don't repeat, so three reviewers check 560 different comments. Each file opens on step-by-step instructions with worked examples. Reviewers mark each row `reviewed = y` and note any wrong or missing themes. Put the returned files back in `output/` and re-run `themes.py`: §5 of the report shows precision and recall per theme and reviewer agreement. Re-runs never overwrite entries (`--new-sample --force` is the only way to replace them; add `--reviewers N` or `--overlap N` to change the split).
- **Changing a rule:** edit the patterns in `theme_codebook.json` and re-run. A bad pattern stops the run and names the theme.

## Rules the pipeline follows

- **The source file is never modified. No rows dropped, no answers imputed.** Rows in = rows out. Blanks stay blank.
- **Doubtful responses are flagged, never excluded by default.** Primary figures use every valid answer. Review flags (possible scale reversal, duplicates, repeated IDs, very short or long completions) feed a side-by-side sensitivity check (`in_sensitivity_sample`).
- **Ambiguous rating labels ("Very agree", "Satisfied") are left unscored** until EveryMind confirms their meaning. The report counts them and shows what changes if they're scored.
- **Self-rated change is reported item by item**, with valid pairs only, and described as retrospective self-report, not measured learning.
- **Small groups:** groups under 10 are pooled into "Other (…)", and cells under 10 are suppressed, with complementary suppression so that nothing is recoverable by subtraction **within a table**. Comparing different tables could still narrow down a small group, so treat these as internal working files, not an approved public release. Each figure is labeled "⚠ thin" when **its own** valid n is 10–29.
- **Comments are never reworded.** The exact export text is in `*_original`, and response types are in `*_type`. The `*_redacted` columns replace listed names (any capitalization), "Mr./Ms./Dr. + name", emails and phone numbers. **This is a preliminary, automated pass, so read every excerpt before quoting or sharing it.**
- **Dates are read one value at a time** from a fixed list of unambiguous formats (`2026-04-28`, `April 28, 2026`, …), so row order never matters. Slash dates use the order set in `dates.slash_date_order` (US month/day/year by default). Anything else is left blank and flagged.
- **Possible duplicates** compare the actual answers (all role selections, the exact rating labels, full comments), and point to the first copy by `duplicate_of_row`, which works even if it has no ID.
- **Invalid answers are flagged, never forced.** Unknown labels, counties and roles, and 0–10 answers that aren't whole numbers in range, are left blank, flagged `flag_unmapped_value`, and listed in the report. A role answer that mixes known and unknown selections (e.g. "Student;Volunteer") goes into its own "Includes unrecognized role" group, so it never counts as a single-role student.

## Confirming an ambiguous label

If EveryMind confirms what "Very agree" or "Satisfied" meant, set its score in `likert_map` in `cleaning_config.json` and re-run. It is then scored, logged and reported like any other label.

## Changing the config

Rating labels, role labels, language prefixes, county aliases, thresholds and the slash-date order can all be changed in `cleaning_config.json`. `clean.py` checks the config first and stops with a clear message if a value can't be applied (e.g. a rating of 7). `validate_clean.py` reads the same settings and applies them with its own code. Pass it the same `--config` you ran with; it checks the config against the run's manifest. It compares every value in every sheet of the workbook against its source.

## When a new export has something unexpected

If a new label, county or role shows up, the script prints a warning. It leaves the value blank and flags the row (`flag_unmapped_value`), and the report lists it in §2a. Add the value to `cleaning_config.json` and re-run. No code changes are needed.

## Who runs it and when

Program staff can run it after each batch of trainings (e.g. monthly) with Python installed: run `clean.py`, then `validate_clean.py`. Only use the outputs if validation passes. Review §2/§2a of the report for new issues, then use `clean_responses.csv` for analysis or the dashboard.
