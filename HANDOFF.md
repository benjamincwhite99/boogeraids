# Handoff: EveryMind 988 case (UMD BuildFest 2026)

Last updated 2026-11-02. Start a new session with: "Read HANDOFF.md and continue."

## The competition

- Case: EveryMind "Understanding 988" post-training survey, 1,299 responses (`BuildFest_EveryMind_Survey_Data_FULL.xlsx`, case PDF in this folder).
- **Due Sun 2026-11-08, 9 AM ET.** File names follow `Team#_Deliverable`.

| Deliverable | Status |
|---|---|
| Technical artifact (must run, handle new data, include run instructions) | Pipeline done (below), including the one-command `run_all.py`. A visual dashboard is still to do. |
| Demo video, 2 minutes or less | Not started |
| Process memo, 1–2 pages | Not started |
| AI Process Appendix, 1 page | Not started. Error log below. |
| .pptx deck | Not started |

- Rubric: Functionality 20%, Technical Soundness 30%, Business/Social Impact 35%, AI Transparency 15%.
- Other dates: mandatory workshop Mon 2026-11-02 at 7 PM; office hours Thu 11-05; finals 11-17.
- **Judging is anonymous.** Submitted links must be "Anyone with the link" and must not reveal identities. The GitHub repo below is under a personal account, so the submission needs an anonymized copy, made **without** the validation files.
- AI use is allowed and encouraged (case PDF p. 9). The appendix must say how AI was used and how outputs were validated (p. 11).

## Repo and privacy rules

- Repo: https://github.com/benjamincwhite99/boogeraids (public, branch `main`). Latest commits:
  - `4029e3d`: the 3 validation sample xlsx files, force-added to `output/` **at the user's explicit request**, despite the warning that the repo is public.
  - `8404ff7`: themes.py, codebook, tests, README.
  - `4b8dcc5`: cleaning pipeline (transactional publishing, full workbook validation).
- Never commit anything else from the survey data, the case PDF, other outputs, or presenter names. Names live only in the gitignored `redaction_names.local.json`.
- Before every commit, scan staged files for those names, emails and phone numbers.
- Commit and push only when the user asks.
- Claude must **not** fill in the hand-validation files. That's the team's manual validation.

## Files

| File | What it does |
|---|---|
| `clean.py` | Raw export → `output/` bundle: clean CSV/XLSX, change log, group tables, `data_quality_report.md`, `run_manifest.json` with SHA-256 hashes. Config-driven (`cleaning_config.json`). Drops no rows and imputes nothing; doubtful answers are flagged. Publishing is atomic, with a lock file. |
| `validate_clean.py` | Independent re-check of every output value against the raw file. Passes 100/100. |
| `tests/test_edge_cases.py` | 90/90 synthetic edge-case tests for the cleaning. |
| `themes.py` + `theme_codebook.json` | Keyword/regex coding of the two comment questions. Writes `output/themes/` (comment_codes.csv, theme_summary.csv, themes_report.md, themes_manifest.json). |
| `tests/test_themes.py` | 51/51 theme tests. |
| `run_all.py` | Runs clean → validate → themes (optionally the tests first) and stops at the first failure. |
| `README.md` | Run instructions and the rules the pipeline follows. |
| `output/theme_validation_sample_{A,B,C}.xlsx` | One hand-check file per teammate. Each has 200 comments (100 per question). 10 per question are shared by all three, to measure agreement, so 560 comments are distinct. Each file opens on the instructions. |

Run everything: `python3 run_all.py` (add `--tests` to run both test suites first). Same as `python3 clean.py && python3 validate_clean.py && python3 themes.py` (Python 3.9, pandas 2.3.3, openpyxl 3.1.5).

Before handing the validation files out, the old `output/theme_validation_sample.xlsx` (empty, no longer used) should be closed in Excel and deleted.

## Key findings so far (provisional until the hand validation is scored)

- **Comments:** 1,062 substantive "most valuable" comments and 568 substantive "improve" comments.
- **What the "improve" comments are:** 265 questions (46.7%), 173 suggestions, 119 "no change", 11 unclear.
- **Who asks the questions:** questions are 59.6% of students' improve comments, against 1.6% for everyone else. 263 of the 265 come from 12 Anne Arundel sessions where at least half of that session's answers were questions. The likely cause is trainers prompting for questions, or a different version of the form.
- **Most valuable themes:**
  - when/how to use 988: 17.5%
  - not only for suicide: 14.2%
  - no specific theme: 12.1%
  - 24/7 and anywhere: 11.4%
  - anyone can call: 10.6%
- **Adults vs students on "most valuable":**
  - what 988 is: 23.6% vs 5.0%
  - helping others: 18.1% vs 7.7%
  - how 988 differs from 911: 13.2% vs 3.1%
- **Topics of the questions people still had:**
  - usage/history: 26.4%
  - what a call is like: 24.5%
  - who can call, for what and from where: 21.9%
  - mental health and helping someone: 15.5%
  - careers: 9.8%
  - privacy/police: 9.1%
- **Top suggestions:** reach more people (8.8%), more interactive (6.7%), more examples and demos (5.8%).
- **Candidate recommendations:**
  - Add a separate "What questions do you still have?" field to the survey.
  - Write a student FAQ covering privacy, what a call is like, and who can call.
  - Each recommendation still needs an owner, a metric and a cost.
- **Caveat for the appendix:** the keyword rules were tuned on these same comments, so the validation sample is not a held-out test.
- **Cleaning decisions to state in the memo:**
  - "Very agree" and "Satisfied" are left unscored as ambiguous.
  - Group cells with n under 10 are suppressed; n from 10 to 29 is labeled "thin".
  - Changes are retrospective self-report, not measured learning.

## Open questions for EveryMind (workshop, 11-02 at 7 PM)

1. What scores did "Very agree" and "Satisfied" mean? Answering this means setting `likert_map` in the config and re-running.
2. Did the form change during the year? Some sessions skip the likely-to-call and satisfaction questions.
3. Were Anne Arundel student sessions told to write their questions in the "improve" box?
4. Does "county" mean where the training was held, or where the participant lives?
5. Are the Fairfax and Harris trainings in scope? Is 410-768-5522 the crisis line taught in training?
6. How much AI-written code and analysis is acceptable, given that it's disclosed?

## AI appendix: errors Claude made or let through, and how they were caught

- Claimed "17 sessions" when the data showed otherwise; corrected after checking.
- Importing into Excel turned the literal answer "None" into a blank. Fixed with `keep_default_na=False`.
- A batch of responses dated Oct 20 crashed date parsing. Fixed with value-by-value parsing.
- Claude blamed one crash on clean.py when the cause was elsewhere.
- Three "misconception" rules flagged accurate statements ("911 is for physical emergencies", US-only, call back). Removed.
- The redaction missed presenter names in comments. Found by reading the excerpts and added to the local name list.
- Several reviews from an outside tool (Codex) found gaps that Claude then fixed: partial publishing, and a workbook check that covered only some columns.
- The team still has to add representative prompts of their own (keep a prompt log) and their validation numbers.

## Next steps, in order

1. **Team:** fill in validation files A, B and C. Return them to `output/` and run `python3 themes.py`; §5 of the report gives precision, recall and reviewer agreement. Report any theme under 80% as rough, or tighten its rules.
2. **Workshop answers:** apply them to the config and re-run.
3. **Claude:**
   - draft 3–4 recommendations, each with an owner, a metric and a cost
   - draft the memo
   - draft the AI appendix
4. **Claude:** make a simple visual dashboard for the demo video. (`run_all.py` is done.)
5. **Fri–Sat:** deck (.pptx) and video. **Sat night:** anonymized repo link, a test run from a fresh clone, and final file naming.
