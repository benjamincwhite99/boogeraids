#!/usr/bin/env python3
"""EveryMind "Understanding 988" survey: comment themes.

Usage
-----
    python3 themes.py                         # codes the comments in output/clean_responses.csv
    python3 themes.py --out output --codebook theme_codebook.json
    python3 themes.py --new-sample            # draw fresh validation samples (refuses to replace ones with entries)
    python3 themes.py --new-sample --reviewers 4 --overlap 10

Run clean.py (and validate_clean.py) first. This reads the cleaned bundle and writes output/themes/:
    comment_codes.csv          one row per substantive comment: intent, themes, role group, county, redacted text
    theme_summary.csv          every theme count in the report, long format, with denominators and suppression
    themes_report.md           theme tables, example excerpts, uncoded comments, validation results
    themes_manifest.json       which cleaned bundle, codebook and code produced this, and a SHA-256 of every file

and, once, output/theme_validation_sample_A.xlsx, _B, _C: one file per reviewer, each a fixed random sample of
100 comments per question to check by hand. A few comments appear in every file, to measure how far reviewers agree.
Re-running themes.py reads the entries in all of them and reports how often the keyword rules agree with the people.

Principles
    * Comments are never reworded. Coding uses the cleaned text; excerpts use the redacted text.
    * Themes come from the keyword rules in theme_codebook.json, so a new export is coded the same way.
    * A comment can have several themes. Percentages use substantive comments as the denominator.
    * Keyword rules are a first pass. The validation sample measures how far they can be trusted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
FIELDS = {"most_valuable": "text_most_valuable", "improve": "text_improve"}
QUESTIONS = {
    "most_valuable": "What was the most valuable thing you learned during the training?",
    "improve": "How can we improve this training in the future?",
}
GROUPS = {"Students": lambda d: d["role_group"].eq("Student"), "All other respondents": lambda d: d["role_group"].ne("Student")}
UNSPECIFIC = {"most_valuable": ("mv_unspecific", "No specific theme (e.g. '988', 'the number', 'everything')")}
UNSPECIFIC_QUESTION = ("q_unspecific", "Wants to know more about 988, no specific topic")
BUNDLE = ["comment_codes.csv", "theme_summary.csv", "themes_report.md"]
SAMPLE_PREFIX = "theme_validation_sample_"
SAMPLE_PER_FIELD = 100
SAMPLE_REVIEWERS = 3
SAMPLE_OVERLAP = 10          # comments per question that every reviewer gets, for reviewer agreement
SAMPLE_SEED = 988
EXAMPLES_PER_THEME = 3


# --------------------------------------------------------------------------- helpers
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_comments(obj):
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def fold(text: str) -> str:
    """Lower-case, remove accents, straighten apostrophes, collapse whitespace. Used for matching only."""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip().lower()


def compile_codebook(raw: dict) -> dict:
    """Compile every pattern up front so a bad regex stops the run with its theme id, not a traceback mid-way."""
    cb = strip_comments(raw)
    out, seen = {}, set()

    def comp(where, pats):
        res = []
        for p in pats:
            try:
                res.append(re.compile(p))
            except re.error as e:
                sys.exit(f"ERROR: theme_codebook.json, {where}: pattern {p!r} is not a valid regular expression ({e}).")
        return res

    for field in FIELDS:
        if field not in cb:
            sys.exit(f"ERROR: theme_codebook.json has no '{field}' section.")
        themes = []
        for t in cb[field]["themes"]:
            if t["id"] in seen:
                sys.exit(f"ERROR: theme_codebook.json: theme id {t['id']!r} is used twice.")
            seen.add(t["id"])
            themes.append({**t, "patterns": comp(t["id"], t["patterns"]), "exclude": comp(t["id"], t.get("exclude", [])),
                           "applies_to": t.get("applies_to")})
        intent = {k: comp(f"{field}.intent.{k}", v) for k, v in cb[field].get("intent", {}).items()}
        out[field] = {"themes": themes, "intent": intent}
    return out


def any_match(pats, text) -> bool:
    return any(p.search(text) for p in pats)


def classify_intent(text: str, rules: dict) -> str:
    """question / no_change / suggestion / unclear. Only used for the 'improve' box."""
    if any_match(rules.get("unclear", []), text):
        return "unclear"
    if any_match(rules["question"], text) and not any_match(rules["question_not"], text):
        return "question"
    if any_match(rules["no_change"], text) and not any_match(rules["no_change_not"], text):
        return "no_change"
    return "suggestion"


def code_comment(text: str, spec: dict, field: str) -> tuple[str, list]:
    t = fold(text)
    intent = classify_intent(t, spec["intent"]) if spec["intent"] else ""
    fits = lambda th, it: (not th["applies_to"] or it in th["applies_to"]) and any_match(th["patterns"], t) and not any_match(th["exclude"], t)
    themes = [th["id"] for th in spec["themes"] if fits(th, intent)]
    if intent == "suggestion" and not themes:
        # A bare topic ("warmline", "training process") in a box many used for questions: a question if it names a
        # question topic, otherwise left for a person to read.
        q = [th["id"] for th in spec["themes"] if fits(th, "question")]
        intent, themes = ("question", q) if any(x.startswith("q_") for x in q) else ("unclear", [])
    if intent == "question" and not any(x.startswith("q_") for x in themes):
        themes.append(UNSPECIFIC_QUESTION[0])
    if not themes and field in UNSPECIFIC and intent != "no_change":
        themes = [UNSPECIFIC[field][0]]
    return intent, themes


def labels(spec: dict, field: str) -> dict:
    lab = {t["id"]: t["label"] for t in spec[field]["themes"]}
    if field in UNSPECIFIC:
        lab[UNSPECIFIC[field][0]] = UNSPECIFIC[field][1]
    if field == "improve":
        lab[UNSPECIFIC_QUESTION[0]] = UNSPECIFIC_QUESTION[1]
    return lab


def md_table(frame: pd.DataFrame) -> str:
    cols = [str(c) for c in frame.columns]
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(esc(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(esc(v) for v in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join(lines)


def pct(num, den) -> str:
    return f"{100 * num / den:.1f}%" if den else "n/a"


# --------------------------------------------------------------------------- coding
def load_clean(out: Path) -> tuple[pd.DataFrame, dict]:
    csv, man = out / "clean_responses.csv", out / "run_manifest.json"
    if not csv.exists() or not man.exists():
        sys.exit(f"ERROR: {out} has no cleaned bundle. Run clean.py (then validate_clean.py) first.")
    manifest = json.loads(man.read_text())
    want = manifest.get("files", {}).get("clean_responses.csv")
    if want != sha256(csv):
        sys.exit("ERROR: clean_responses.csv doesn't match run_manifest.json. Re-run clean.py and validate_clean.py.")
    d = pd.read_csv(csv, keep_default_na=False, na_values=[""], dtype={"county": str, "role_group": str})
    need = ["source_row", "role_group", "county", "session_id", "language"] + [f"{c}{s}" for c in FIELDS.values() for s in ("", "_type", "_redacted")]
    missing = [c for c in need if c not in d.columns]
    if missing:
        sys.exit(f"ERROR: clean_responses.csv is missing {missing}. Re-run clean.py.")
    return d, manifest


def code_all(d: pd.DataFrame, spec: dict) -> pd.DataFrame:
    rows = []
    for field, col in FIELDS.items():
        sub = d[d[f"{col}_type"].eq("substantive")]
        for r in sub.itertuples(index=False):
            r = r._asdict()
            intent, themes = code_comment(r[col], spec[field], field)
            rows.append({"source_row": r["source_row"], "field": field, "role_group": r["role_group"], "county": r["county"],
                         "session_id": r["session_id"], "language": r["language"], "intent": intent, "themes": ";".join(themes), "n_themes": len(themes),
                         "text": r[col], "text_redacted": r[f"{col}_redacted"]})
    return pd.DataFrame(rows, columns=["source_row", "field", "role_group", "county", "session_id", "language", "intent", "themes",
                                       "n_themes", "text", "text_redacted"])


def has(codes: pd.DataFrame, theme: str) -> pd.Series:
    return codes["themes"].str.split(";").apply(lambda ts: theme in ts)


def summarise(codes: pd.DataFrame, spec: dict, min_n: int) -> pd.DataFrame:
    """Long table: one row per field x theme (or intent) x group. A group's figures are suppressed when the group has
    fewer than min_n comments to count from (the same rule as the cleaning report). Counts under min_n are kept
    but marked 'few', so they aren't compared."""
    out = []
    for field in FIELDS:
        c = codes[codes["field"].eq(field)]
        everyone = pd.Series(True, index=c.index)
        blocks = []
        if field == "improve":
            for it in ["question", "no_change", "suggestion", "unclear"]:
                blocks.append(("intent", it, it, c["intent"].eq(it), everyone))
        for tid, lab in labels(spec, field).items():
            blocks.append(("theme", tid, lab, has(c, tid), c["intent"].eq("question") if tid.startswith("q_") else everyone))
        for kind, key, lab, hit, base in blocks:
            row = {"field": field, "kind": kind, "id": key, "label": lab,
                   "denominator_is": "question comments" if key.startswith("q_") else "substantive comments"}
            cells = {"All": (int((hit & base).sum()), int(base.sum()))}
            for g, sel in GROUPS.items():
                m = sel(c)
                cells[g] = (int((hit & base & m).sum()), int((base & m).sum()))
            # if one group is suppressed, the other could be worked out from All, so both go
            hide = any(den < min_n for g, (n, den) in cells.items() if g != "All")
            for g, (n, den) in cells.items():
                h = hide and g != "All"
                out.append({**row, "group": g, "n": pd.NA if h else n, "denominator": pd.NA if h else den,
                            "pct": pd.NA if h or not den else round(100 * n / den, 1),
                            "status": f"suppressed (a group has under {min_n} comments)" if h
                            else (f"few (under {min_n})" if n < min_n else "shown")})
    out = pd.DataFrame(out)
    for col in ("n", "denominator"):
        out[col] = out[col].astype("Int64")
    return out


# --------------------------------------------------------------------------- validation sample
SAMPLE_COLS = ["sample_id", "field", "source_row", "comment", "auto_intent", "auto_themes",
               "reviewed", "correct_intent", "wrong_themes", "missing_themes", "notes"]
ENTRY_COLS = ["reviewed", "correct_intent", "wrong_themes", "missing_themes", "notes"]
INTENTS = {
    "question": "The person asks something or says what they still want to know (\"What happens if…?\", \"I want to know how many people call\").",
    "suggestion": "The person suggests a change to the training (\"make it longer\", \"more games\").",
    "no_change": "Praise, or nothing needs changing (\"Nothing, it was great\", \"Keep it up\"). These rows have no themes.",
    "unclear": "None of the above, or you can't tell what they mean.",
}
FIELD_NAMES = {"most_valuable": "most valuable thing learned", "improve": "how to improve the training"}


def sample_paths(out: Path) -> list:
    return sorted([*out.glob(f"{SAMPLE_PREFIX}*.xlsx"), *out.glob(f"{SAMPLE_PREFIX}*.csv")])


def draw_samples(codes: pd.DataFrame, reviewers: int, overlap: int) -> dict:
    """One sample per reviewer (A, B, C, …): SAMPLE_PER_FIELD comments per question each. `overlap` comments per question
    go to every reviewer (to measure agreement); the rest don't repeat across reviewers. Mixed in, not marked."""
    if not 1 <= reviewers <= 26:
        sys.exit("ERROR: --reviewers must be between 1 and 26.")
    if not 0 <= overlap <= SAMPLE_PER_FIELD:
        sys.exit(f"ERROR: --overlap must be between 0 and {SAMPLE_PER_FIELD}.")
    rng = random.Random(SAMPLE_SEED)
    letters = [chr(ord("A") + i) for i in range(reviewers)]
    picks = {x: [] for x in letters}
    for field in FIELDS:
        pool = list(codes.index[codes["field"].eq(field)])
        rng.shuffle(pool)
        shared = pool[:overlap] if reviewers > 1 else []
        rest, k = pool[len(shared):], SAMPLE_PER_FIELD - len(shared)
        for i, x in enumerate(letters):
            picks[x] += shared + rest[i * k:(i + 1) * k]
    out = {}
    for x, idx in picks.items():
        c = codes.loc[sorted(idx)]
        c = pd.concat([c[c["field"].eq(f)] for f in FIELDS])
        out[x] = pd.DataFrame({"sample_id": [f"{x}-{i:03d}" for i in range(1, len(c) + 1)], "field": c["field"].values,
                               "source_row": c["source_row"].values, "comment": c["text_redacted"].values,
                               "auto_intent": c["intent"].values, "auto_themes": c["themes"].values,
                               **{col: "" for col in ENTRY_COLS}})
    return out


def write_sample(path: Path, sample: pd.DataFrame, spec: dict, reviewer: str):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    bold, wrap = Font(bold=True), Alignment(wrap_text=True, vertical="top")
    grey, yellow = PatternFill("solid", fgColor="EDEDED"), PatternFill("solid", fgColor="FFF2CC")
    thin = Side(style="thin", color="BFBFBF")
    n_mv = int(sample["field"].eq("most_valuable").sum())
    n_im = int(sample["field"].eq("improve").sum())

    wb = Workbook()
    # --- instructions first, so they're what opens
    how = wb.active
    how.title = "How to fill in"
    how.column_dimensions["A"].width = 34
    how.column_dimensions["B"].width = 34
    how.column_dimensions["C"].width = 60
    lines = [
        ("h1", f"Checking the comment codes: reviewer {reviewer}"),
        ("", f"Your file has {len(sample)} survey comments ({n_mv} 'most valuable', {n_im} 'improve'). Allow about 1–1.5 hours."),
        ("", ""),
        ("h2", "What this is"),
        ("", "A program sorted every comment into themes using keyword rules. You check whether it got each one right. "
             "Your answers measure how accurate the program is, so mark what you actually think, even if it often disagrees."),
        ("", "Some comments are also in a teammate's file. That's on purpose: it shows how consistently people code. "
             "Don't compare answers until everyone has finished."),
        ("", ""),
        ("h2", "The Sample tab"),
        ("", "Grey columns (A–F) show what the program did. Don't change them."),
        ("", "Yellow columns (G–K) are yours. Sorting and filtering are fine. Don't delete rows or columns, and don't rename the file."),
        ("", "field: 'most_valuable' answers \"" + QUESTIONS["most_valuable"] + "\""),
        ("", "            'improve' answers \"" + QUESTIONS["improve"] + "\""),
        ("", ""),
        ("h2", "For each row"),
        ("", "1. Read the comment (column D)."),
        ("", "2. 'improve' rows only: is auto_intent right? If not, pick the right one in correct_intent (dropdown). "
             "Definitions are below. Leave it blank if auto_intent is right. Leave it blank on 'most_valuable' rows."),
        ("", "3. Check each code in auto_themes (codes are separated by ;). Look up any code you don't know in the Codebook tab."),
        ("", "      • A code that doesn't fit the comment → type it in wrong_themes."),
        ("", "      • A theme from the Codebook that fits but isn't listed → type its code in missing_themes."),
        ("", "      • More than one code → separate them with ;  (e.g.  mv_help_others; mv_warning_signs)"),
        ("", "4. Set reviewed to y (dropdown). Do this on every row you've checked, including rows where the program was "
             "completely right (most of them). Rows without y are not counted."),
        ("", "5. notes (optional): anything odd or hard to decide."),
        ("", ""),
        ("h2", "Rules of thumb"),
        ("", "• A theme fits if the comment says it, not if it's just loosely related. Don't guess what the person meant."),
        ("", "• If you're unsure, keep the program's choice and explain in notes."),
        ("", "• 'improve' rows: im_ codes are for suggestions, q_ codes only for questions, and no_change rows have no codes. "
             "If you change the intent, fix the codes to match (see example 4)."),
        ("", "• mv_unspecific and q_unspecific mean \"nothing more specific\". If you add a specific theme, put the unspecific code in wrong_themes."),
        ("", "• mv_possible_misconception: only if the comment states something wrong about 988 (e.g. \"988 is only for suicide\")."),
        ("", "• Comments in Spanish are coded the same way. [NAME], [EMAIL] and [PHONE] mark details removed automatically."),
        ("", ""),
        ("h2", "Intents ('improve' rows)"),
        *[("intent", f"{k}|{v}") for k, v in INTENTS.items()],
        ("", ""),
        ("h2", "Examples (made up)"),
        ("ex", "Comment|The program said|You fill in"),
        ("ex", "1. That you can text 988 too, not just call|mv_contact_options|reviewed = y  (nothing else: it's right)"),
        ("ex", "2. That anyone can call, even for a friend|mv_anyone_any_age|reviewed = y,  missing_themes = mv_help_others"),
        ("ex", "3. That it's not only for suicide|mv_beyond_suicide; mv_possible_misconception|reviewed = y,  wrong_themes = mv_possible_misconception  "
               "(it states the correct fact)"),
        ("ex", "4. What happens if I call and hang up?|intent suggestion; im_content|reviewed = y,  correct_intent = question,  "
               "wrong_themes = im_content,  missing_themes = q_call_experience"),
        ("ex", "5. More games, and make it longer|intent suggestion; im_more_interactive|reviewed = y,  missing_themes = im_time_pacing"),
        ("ex", "6. Nothing, it was great|intent no_change (no codes)|reviewed = y"),
        ("ex", "7. The number|mv_unspecific|reviewed = y  (nothing more specific fits)"),
        ("", ""),
        ("h2", "When you're done"),
        ("", "Save (keep it .xlsx, keep the file name) and send it back. These are real people's comments: don't upload the file "
             "anywhere else, including AI tools, and delete your copy once it's returned."),
    ]
    for kind, text in lines:
        if kind in ("intent", "ex"):
            parts = text.split("|")
            how.append(parts if kind == "ex" else [parts[0], parts[1]])
            r = how.max_row
            if kind == "intent":
                how.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
            for c in how[r]:
                c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
                if isinstance(c.value, str):     # not the merged-over cell
                    c.alignment, c.data_type = wrap, "s"
            how.cell(r, 1).font = bold
            if text.startswith("Comment|"):
                for c in how[r]:
                    c.font, c.fill = bold, grey
            continue
        how.append([text])
        r = how.max_row
        how.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        c = how.cell(r, 1)
        c.alignment = wrap
        c.font = Font(bold=True, size=14) if kind == "h1" else Font(bold=True, size=12) if kind == "h2" else Font()
        if kind == "" and len(text) > 120:
            how.row_dimensions[r].height = 15 * (len(text) // 120 + 1)

    # --- the sample
    ws = wb.create_sheet("Sample")
    ws.append(SAMPLE_COLS)
    for row in sample.itertuples(index=False):
        ws.append(["" if pd.isna(v) else v for v in row])
    for cell in ws[1]:
        cell.font = bold
        cell.fill = yellow if cell.value in ENTRY_COLS else grey
    for r in ws.iter_rows(min_row=2):
        for cell in r:
            if isinstance(cell.value, str):
                cell.data_type = "s"     # comments stay literal text, never formulas
            if ws.cell(1, cell.column).value in ENTRY_COLS:
                cell.fill = yellow
    widths = {"A": 9, "B": 14, "C": 10, "D": 70, "E": 12, "F": 40, "G": 10, "H": 14, "I": 30, "J": 30, "K": 30}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    for c in ws["D"]:
        c.alignment = wrap
    ws.freeze_panes = "E2"
    last = ws.max_row
    for col, options in [("G", '"y"'), ("H", '"' + ",".join(INTENTS) + '"')]:
        dv = DataValidation(type="list", formula1=options, allow_blank=True)
        dv.error, dv.errorTitle = "Pick a value from the list, or leave it blank.", "Not on the list"
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{last}")

    # --- codebook
    cb = wb.create_sheet("Codebook")
    cb.append(["question", "theme code", "what it means", "only for intent"])
    for field in FIELDS:
        for tid, lab in labels(spec, field).items():
            th = next((t for t in spec[field]["themes"] if t["id"] == tid), {})
            only = ", ".join(th.get("applies_to") or []) or ("question" if tid.startswith("q_") else "")
            cb.append([FIELD_NAMES[field], tid, lab, only])
    for cell in cb[1]:
        cell.font, cell.fill = bold, grey
    cb.column_dimensions["A"].width = 30
    cb.column_dimensions["B"].width = 28
    cb.column_dimensions["C"].width = 80
    cb.column_dimensions["D"].width = 22
    cb.freeze_panes = "A2"
    tmp = path.with_name(f".{path.name}.tmp")
    wb.save(tmp)
    tmp.replace(path)


def read_samples(paths: list) -> pd.DataFrame | None:
    if not paths:
        return None
    parts = []
    for path in paths:
        if path.suffix.lower() == ".csv":
            try:
                s = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            except UnicodeDecodeError:
                s = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp1252")
        else:
            s = pd.read_excel(path, sheet_name="Sample", dtype=str, keep_default_na=False)
        missing = [c for c in SAMPLE_COLS if c not in s.columns]
        if missing:
            sys.exit(f"ERROR: {path.name} is missing columns {missing}. Restore them or draw new samples with --new-sample --force.")
        parts.append(s.assign(file=path.name, reviewer=s["sample_id"].str.split("-").str[0]))
    s = pd.concat(parts, ignore_index=True)
    # Permit an unchanged blank assignment beside its returned CSV, never two reviews.
    drop = []
    source_cols = [c for c in SAMPLE_COLS if c not in ENTRY_COLS]
    for sid, group in s[s["sample_id"].duplicated(keep=False)].groupby("sample_id"):
        populated = group[ENTRY_COLS].apply(lambda c: c.str.strip().ne("")).any(axis=1)
        matching = len(group[source_cols].drop_duplicates()) == 1
        formats = {Path(f).suffix.lower() for f in group["file"]}
        if len(group) == 2 and matching and populated.sum() <= 1 and formats == {".csv", ".xlsx"}:
            keep = group.index[populated][0] if populated.any() else group.index[group.file.str.endswith(".csv")][0]
            drop.extend(i for i in group.index if i != keep)
        else:
            files = sorted(set(group["file"]))
            sys.exit(f"ERROR: {', '.join(files)} contain the same sample rows with duplicate or conflicting entries. Keep one authoritative review.")
    s = s.drop(index=drop).reset_index(drop=True)
    return s


def has_entries(s: pd.DataFrame | None) -> bool:
    return s is not None and s[ENTRY_COLS].apply(lambda c: c.str.strip().ne("")).any().any()


def score_sample(s: pd.DataFrame, codes: pd.DataFrame, spec: dict) -> dict:
    """Agreement between the keyword rules (as they run now) and the reviewers' entries, and between reviewers on the
    comments more than one of them checked."""
    valid_ids = {f: set(labels(spec, f)) for f in FIELDS}
    by_key = {(r.field, str(r.source_row)): r for r in codes.itertuples(index=False)}
    warn, per_theme, intent_ok, intent_n, rows = [], {}, 0, 0, 0
    theme_rows = 0
    scored_reviewers = set()
    judged = {}                      # (field, source_row) -> [(reviewer, themes, intent)]
    split = lambda v: [x.strip().strip('"').strip("'").strip() for x in str(v).replace(",", ";").split(";") if x.strip()]
    for r in s.itertuples(index=False):
        if str(r.reviewed).strip().lower() not in ("y", "yes", "x", "1"):
            if any(str(getattr(r, c)).strip() for c in ENTRY_COLS[1:4]):
                warn.append(f"sample {r.sample_id}: has entries but reviewed isn't y; not counted")
            continue
        cur = by_key.get((r.field, str(r.source_row)))
        if cur is None or cur.text_redacted != r.comment:
            warn.append(f"sample {r.sample_id}: comment no longer matches row {r.source_row} in this export; skipped")
            continue
        # Score against the *current* codes, so rule edits after the review are measured honestly:
        # a person's 'wrong'/'missing' entries refer to the codes they saw, recorded in auto_themes.
        saw = set(split(r.auto_themes))
        wrong, missing = set(split(r.wrong_themes)), set(split(r.missing_themes))
        bad = (saw | wrong | missing) - valid_ids[r.field]
        if bad:
            warn.append(f"sample {r.sample_id}: unknown theme id(s) {sorted(bad)}; row skipped, check original auto codes and reviewer entries")
            continue
        if wrong - saw or wrong & missing:
            warn.append(f"sample {r.sample_id}: inconsistent wrong/missing theme entries; row skipped")
            continue
        truth = (saw - wrong) | missing
        now = set(split(cur.themes))
        want = ""
        if r.field == "improve":
            want = str(r.correct_intent).strip() or str(r.auto_intent).strip()
            if want not in {"question", "suggestion", "no_change", "unclear"}:
                warn.append(f"sample {r.sample_id}: unknown intent {want!r}; row skipped")
                continue
            intent_n += 1
            intent_ok += cur.intent == want
        elif str(r.correct_intent).strip():
            warn.append(f"sample {r.sample_id}: correct_intent applies only to improve rows; intent entry ignored")
        rows += 1
        scored_reviewers.add(r.reviewer)
        allowed = {t["id"]: t["applies_to"] for t in spec[r.field]["themes"]}
        allowed[UNSPECIFIC_QUESTION[0]] = ["question"]
        incompatible = {t for t in truth if r.field == "improve" and
                        (want in {"no_change", "unclear"} or
                         (allowed.get(t) and want not in allowed[t]))}
        if incompatible:
            warn.append(f"sample {r.sample_id}: themes {sorted(incompatible)} conflict with corrected intent {want}; intent scored, themes await reviewer clarification")
        else:
            theme_rows += 1
            for t in valid_ids[r.field]:
                st = per_theme.setdefault(t, {"tp": 0, "fp": 0, "fn": 0})
                st["tp"] += t in now and t in truth
                st["fp"] += t in now and t not in truth
                st["fn"] += t not in now and t in truth
        judged.setdefault((r.field, str(r.source_row)), []).append((r.reviewer, None if incompatible else truth, want))
    pairs = themes_same = intent_pairs = intent_same = 0
    for (field, _), js in judged.items():
        for i in range(len(js)):
            for j in range(i + 1, len(js)):
                if js[i][1] is not None and js[j][1] is not None:
                    pairs += 1
                    themes_same += js[i][1] == js[j][1]
                if field == "improve":
                    intent_pairs += 1
                    intent_same += js[i][2] == js[j][2]
    return {"rows": rows, "per_theme": per_theme, "intent_ok": intent_ok, "intent_n": intent_n, "warnings": warn,
            "reviewers": sorted(scored_reviewers), "theme_rows": theme_rows,
            "rows_total": len(s), "unique_comments": len(judged),
            "agreement": {"pairs": pairs, "themes_same": themes_same, "intent_pairs": intent_pairs, "intent_same": intent_same}}


# --------------------------------------------------------------------------- report
def theme_table(summary: pd.DataFrame, field: str, kind: str, prefix: str | None = None, exclude_prefix: str | None = None):
    s = summary[(summary["field"] == field) & (summary["kind"] == kind)]
    if prefix:
        s = s[s["id"].str.startswith(prefix)]
    if exclude_prefix:
        s = s[~s["id"].str.startswith(exclude_prefix)]
    if s.empty:
        return pd.DataFrame()

    def fmt(r):
        if r.status.startswith("suppressed"):
            return "suppressed"
        if not r.denominator:
            return "n/a"
        return f"{r.pct:.1f}% ({int(r.n)})" + ("†" if r.status.startswith("few") else "")

    dens = {g: s.loc[s["group"] == g, "denominator"].iloc[0] for g in ["All", *GROUPS]}
    head = {g: f"{g} (of {int(dens[g]):,})" if pd.notna(dens[g]) else g for g in dens}
    rows = []
    for key, g in s.groupby("id", sort=False):
        cell = {r.group: r for r in g.itertuples(index=False)}
        rows.append({"Theme": cell["All"].label, "id": f"`{key}`", **{head[k]: fmt(cell[k]) for k in dens}, "_n": cell["All"].n})
    return pd.DataFrame(rows).sort_values("_n", ascending=False, kind="stable").drop(columns="_n")


def examples(codes: pd.DataFrame, field: str, theme: str, k: int) -> list:
    c = codes[(codes["field"] == field) & has(codes, theme)]
    c = c[c["text_redacted"].str.len().between(12, 180)]
    if c.empty:
        return []
    rng = random.Random(f"{SAMPLE_SEED}-{field}-{theme}")
    pick = sorted(rng.sample(list(c.index), min(k, len(c))))
    return [f"\"{codes.at[i, 'text_redacted']}\" ({codes.at[i, 'role_group']}, row {codes.at[i, 'source_row']})" for i in pick]


def build_report(codes, summary, spec, manifest, d, samples, score, min_n) -> str:
    L = []
    w = L.append
    n_mv = int((codes["field"] == "most_valuable").sum())
    n_im = int((codes["field"] == "improve").sum())
    w("# Comment themes: EveryMind \"Understanding 988\" survey\n")
    w(f"Generated {datetime.now():%Y-%m-%d %H:%M} from `clean_responses.csv` ({len(d):,} responses; input "
      f"`{manifest.get('input', {}).get('name', '?')}`). Codebook: `theme_codebook.json`.\n")
    w("> **Read this first.** Themes come from keyword rules, not from a person reading each comment. "
      "A comment can have several themes, so columns don't add up to 100%%. Percentages use **substantive comments** "
      "as the denominator (blank, 'nothing', 'don't know' and 'no questions' answers are excluded); question topics use "
      "question comments. A group with fewer than %d comments to count from is suppressed (with the other group, so it "
      "can't be worked out from the total). † marks counts under %d: shown, but too few to compare groups on. "
      "Excerpts use the automatically redacted text: **read every excerpt before quoting it.** "
      "Section 5 shows how often the rules agree with a person's hand check.\n" % (min_n, min_n))
    if score is None or not score["rows"]:
        w("> ⚠ **Not yet validated.** No rows of the validation sample have been checked yet, so treat every figure "
          "below as provisional.\n")

    else:
        w(f"> **Provisional: partial review and calibration.** {score['rows']} of {len(samples)} sample entries scored "
          f"({score['unique_comments']} distinct comments). These comments informed the rules; "
          "agreement after tuning is not held-out accuracy. Resolve the warnings in section 5.\n")

    # 1. at a glance
    q = codes[(codes["field"] == "improve")]
    w("## 1. At a glance\n")
    glance = [("Substantive 'most valuable' comments", f"{n_mv:,}"), ("Substantive 'improve' comments", f"{n_im:,}")]
    for it, lab in [("question", "…that are questions, not suggestions"), ("no_change", "…that say nothing needs changing / praise"),
                    ("suggestion", "…that suggest a change"), ("unclear", "…that no rule could classify (listed in §6)")]:
        n = int((q["intent"] == it).sum())
        glance.append((lab, f"{n:,} ({pct(n, n_im)})"))
    st = q[q["role_group"].eq("Student")]
    ot = q[q["role_group"].ne("Student")]
    glance.append(("Share of 'improve' comments that are questions: students vs everyone else",
                   f"{pct((st['intent'] == 'question').sum(), len(st))} of {len(st):,} vs "
                   f"{pct((ot['intent'] == 'question').sum(), len(ot))} of {len(ot):,}"))
    w(md_table(pd.DataFrame(glance, columns=["", "Value"])) + "\n")
    by = q.groupby("session_id")["intent"].agg(n="size", q=lambda x: int((x == "question").sum()))
    heavy = by[(by["q"] >= 0.5 * by["n"]) & (by["q"] > 0)]
    nq_all = int(by["q"].sum())
    w("The 'improve' box was often used to ask questions, mostly by students. The question wording invites suggestions, "
      "so these are reported separately (§3) instead of being counted as suggestions.\n")
    if nq_all:
        w(f"**Where the questions come from:** {int(heavy['q'].sum())} of the {nq_all} questions are from {len(heavy)} sessions "
          f"(date × county) in which at least half the answers to this box were questions ({by['n'].gt(0).sum()} sessions answered it). "
          "That pattern suggests those trainers asked people to write their remaining questions here, or that a different "
          "version of the form was used. The data can't tell which. Sessions: "
          + ", ".join(f"{sid} ({r.q} of {r.n})" for sid, r in heavy.sort_values("q", ascending=False).iterrows()) + ".\n")

    # 2. most valuable
    w(f"## 2. Most valuable thing learned ({n_mv:,} comments)\n")
    w(f"*{QUESTIONS['most_valuable']}*\n")
    w(md_table(theme_table(summary, "most_valuable", "theme")) + "\n")
    w("*mv_possible_misconception* flags wording that **might** reflect a misunderstanding (e.g. '988 is only for "
      "suicide'). It is a prompt for staff to read those comments, not a count of people who misunderstood.\n")

    # 3. improve
    w(f"## 3. How to improve the training ({n_im:,} comments)\n")
    w(f"*{QUESTIONS['improve']}*\n")
    w("### 3a. What kind of answer\n")
    w(md_table(theme_table(summary, "improve", "intent")) + "\n")
    w("### 3b. Suggestions (themes found in suggestion and question comments)\n")
    w(md_table(theme_table(summary, "improve", "theme", exclude_prefix="q_")) + "\n")
    nq = int((q["intent"] == "question").sum())
    w(f"### 3c. Topics of the questions people still had ({nq:,} question comments)\n")
    w(f"Denominator: question comments. {int((q[q['intent'] == 'question']['role_group'] == 'Student').sum())} of the {nq} come from "
      "students, so the group columns are suppressed where the other group is too small.\n")
    w(md_table(theme_table(summary, "improve", "theme", prefix="q_")) + "\n")

    # 4. examples
    w("## 4. Example excerpts\n")
    w("Up to %d per theme, picked at random (fixed seed) from comments 12–180 characters long. Redaction is automatic and "
      "can miss names: **check each one before using it.**\n" % EXAMPLES_PER_THEME)
    for field in FIELDS:
        for tid, lab in labels(spec, field).items():
            ex = examples(codes, field, tid, EXAMPLES_PER_THEME)
            if ex:
                w(f"**{lab}** (`{tid}`)\n")
                w("\n".join(f"- {e}" for e in ex) + "\n")

    # 5. validation
    w("## 5. Validation against a hand check\n")
    if score is None:
        w(f"No validation files found. Run `python3 themes.py --new-sample` to create `{SAMPLE_PREFIX}A.xlsx`, `B`, ….\n")
    elif not score["rows"]:
        w(f"{samples['file'].nunique()} validation files ({', '.join(f'`{f}`' for f in sorted(samples['file'].unique()))}) "
          f"hold {len(samples):,} comments waiting to be checked. Mark each row `reviewed = y` once checked "
          "(instructions are in each file), then re-run `themes.py`.\n")
    else:
        w(f"{score['rows']} sample comments checked by hand by {len(score['reviewers'])} reviewer(s) "
          f"({', '.join(score['reviewers'])}). **Precision**: of the themes the rules assigned, the share the "
          "reviewer agreed with. **Recall**: of the themes the reviewer said apply, the share the rules found. "
          "A comment checked by several reviewers counts once per reviewer. "
          "Themes with fewer than 5 hand-coded examples aren't scored.\n")
        w(f"Theme agreement uses {score['theme_rows']} entries; intent agreement uses {score['intent_n']} improve entries. "
          "Invalid codes are excluded; conflicting intent/theme edits are scored for intent only. "
          "Reviewer entries are preserved, including unresolved disagreements.\n")
        rows, tp, fp, fn = [], 0, 0, 0
        for field in FIELDS:
            for tid, lab in labels(spec, field).items():
                st_ = score["per_theme"].get(tid)
                if not st_:
                    continue
                tp, fp, fn = tp + st_["tp"], fp + st_["fp"], fn + st_["fn"]
                support = st_["tp"] + st_["fn"]
                if support < 5 and st_["tp"] + st_["fp"] < 5:
                    rows.append({"Theme": tid, "Hand-coded": support, "Precision": "too few", "Recall": "too few"})
                    continue
                pr = pct(st_["tp"], st_["tp"] + st_["fp"])
                rc = pct(st_["tp"], support)
                rows.append({"Theme": tid, "Hand-coded": support, "Precision": pr, "Recall": rc})
        w(f"Overall: precision {pct(tp, tp + fp)}, recall {pct(tp, tp + fn)}. "
          f"Intent of 'improve' comments correct in {score['intent_ok']} of {score['intent_n']} ({pct(score['intent_ok'], score['intent_n'])}).\n")
        w(md_table(pd.DataFrame(rows)) + "\n")
        w("Any theme below roughly 80% precision or recall should be reported as a rough indication only, or its rules "
          "tightened in `theme_codebook.json`.\n")
        ag = score["agreement"]
        if ag["pairs"]:
            w(f"**Reviewer agreement** (comments checked by more than one reviewer): identical theme codes in "
              f"{ag['themes_same']} of {ag['pairs']} reviewer pairs ({pct(ag['themes_same'], ag['pairs'])})"
              + (f"; same intent in {ag['intent_same']} of {ag['intent_pairs']} ({pct(ag['intent_same'], ag['intent_pairs'])})"
                 if ag["intent_pairs"] else "")
              + ". Low agreement means the theme definitions are ambiguous, not only that the rules are wrong.\n")
        for x in score["warnings"]:
            w(f"- ⚠ {x}")
        if score["warnings"]:
            w("")

    # 6. uncoded
    w("## 6. Comments no rule classified\n")
    un = codes[(codes["field"] == "improve") & (codes["intent"] == "unclear")]
    mvu = codes[(codes["field"] == "most_valuable") & codes["themes"].eq(UNSPECIFIC["most_valuable"][0])]
    w(f"- 'Most valuable' with no specific theme: {len(mvu):,} ({pct(len(mvu), n_mv)}). Mostly very short answers "
      "such as '988' or 'the number'.")
    w(f"- 'Improve' comments no intent rule fitted: {len(un):,} ({pct(len(un), n_im)}).\n")
    w("All are in `comment_codes.csv` (intent `unclear` or theme `mv_unspecific`). If one of them shows a new kind of answer, "
      "add a pattern to `theme_codebook.json` and re-run.\n")
    if len(un):
        w("'Improve' comments with unclear intent:\n")
        w("\n".join(f"- \"{t}\" (row {r})" for t, r in zip(un["text_redacted"], un["source_row"])) + "\n")
    return "\n".join(L)


# --------------------------------------------------------------------------- main
def publish(staging: Path, dest: Path):
    """Swap the new themes folder in; on any failure put the old one back."""
    backup = dest.with_name(f".{dest.name}.backup")
    if backup.exists():
        shutil.rmtree(backup)
    had_old = dest.exists()
    if had_old:
        dest.rename(backup)
    try:
        staging.rename(dest)
    except Exception:
        if had_old:
            backup.rename(dest)
        raise
    if had_old:
        shutil.rmtree(backup, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "output"), help="folder holding the cleaned bundle (default ./output)")
    ap.add_argument("--codebook", default=str(HERE / "theme_codebook.json"))
    ap.add_argument("--config", default=str(HERE / "cleaning_config.json"), help="for the small-cell threshold")
    ap.add_argument("--new-sample", action="store_true", help="draw new validation samples, one file per reviewer")
    ap.add_argument("--reviewers", type=int, default=SAMPLE_REVIEWERS, help=f"with --new-sample: number of files (default {SAMPLE_REVIEWERS})")
    ap.add_argument("--overlap", type=int, default=SAMPLE_OVERLAP,
                    help=f"with --new-sample: comments per question every reviewer gets (default {SAMPLE_OVERLAP})")
    ap.add_argument("--force", action="store_true", help="with --new-sample: replace samples that already have entries")
    a = ap.parse_args(argv)

    out, cb_path = Path(a.out), Path(a.codebook)
    try:
        spec = compile_codebook(json.loads(cb_path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        sys.exit(f"ERROR: codebook {cb_path} not found.")
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {cb_path.name} is not valid JSON ({e}).")
    min_n = int(strip_comments(json.loads(Path(a.config).read_text()))["thresholds"]["small_group_min_n"])

    d, manifest = load_clean(out)
    codes = code_all(d, spec)
    summary = summarise(codes, spec, min_n)

    paths = sample_paths(out)
    if a.new_sample or not paths:
        if not a.force and has_entries(read_samples(paths)):
            sys.exit(f"ERROR: {', '.join(p.name for p in paths)} already have hand-check entries. Add --force to replace them.")
        drawn = draw_samples(codes, a.reviewers, a.overlap)
        for p in paths:
            p.unlink()
        for x, smp in drawn.items():
            write_sample(out / f"{SAMPLE_PREFIX}{x}.xlsx", smp, spec, x)
        print(f"Validation samples written: {', '.join(f'{SAMPLE_PREFIX}{x}.xlsx' for x in drawn)} in {out} "
              f"({', '.join(str(len(v)) for v in drawn.values())} comments)")
        paths = sample_paths(out)
    samples = read_samples(paths)
    score = score_sample(samples, codes, spec)

    dest = out / "themes"
    staging = out / f".themes.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        codes.to_csv(staging / "comment_codes.csv", index=False)
        summary.to_csv(staging / "theme_summary.csv", index=False)
        (staging / "themes_report.md").write_text(build_report(codes, summary, spec, manifest, d, samples, score, min_n),
                                                  encoding="utf-8")
        man = {"created": datetime.now().isoformat(timespec="seconds"),
               "clean_responses_sha256": sha256(out / "clean_responses.csv"),
               "run_manifest_sha256": sha256(out / "run_manifest.json"),
               "codebook": {"file": cb_path.name, "sha256": sha256(cb_path)},
               "code": {"themes.py": sha256(Path(__file__))},
               "python": platform.python_version(), "pandas": pd.__version__,
               "validation": {"files": [p.name for p in paths], "rows_checked": score["rows"],
                              "rows_total": len(samples), "theme_rows": score["theme_rows"],
                              "unique_comments": score["unique_comments"], "reviewers": score["reviewers"],
                              "warnings": score["warnings"], "held_out": False,
                              "source_sha256": {p.name: sha256(p) for p in paths}},
               "files": {f: {"sha256": sha256(staging / f)} for f in BUNDLE}}
        (staging / "themes_manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
        publish(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    n_im = int((codes["field"] == "improve").sum())
    print(f"Coded {len(codes):,} comments ({int((codes['field'] == 'most_valuable').sum()):,} most valuable, {n_im:,} improve). "
          f"Validation rows checked: {score['rows']}. Report: {dest / 'themes_report.md'}")
    for x in score["warnings"]:
        print("WARNING:", x)


if __name__ == "__main__":
    main()
