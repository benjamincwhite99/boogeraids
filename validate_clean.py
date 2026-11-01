#!/usr/bin/env python3
"""Independent check of clean.py's output against the raw export.

Deliberately does NOT import clean.py. Every expected value is re-derived from the raw file with
separately written code and compared to the output bundle. The validator does read the *settings* in
cleaning_config.json (rating labels, role labels, language prefixes, thresholds, date order), so that
supported config changes validate, but it applies them with its own logic.

It also checks that the bundle is complete and consistent: every file is present and matches the run
manifest, the workbook opens and matches the CSVs, no survey text became an Excel formula, and every
group table, group and measure exists with independently recomputed denominators and values.

    python3 validate_clean.py [raw.xlsx|raw.csv] [--out output] [--config cleaning_config.json]

Exits with status 1 if any check fails.
"""
import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd
from dateutil import parser as dateparser

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("raw", nargs="?", default=str(HERE / "BuildFest_EveryMind_Survey_Data_FULL.xlsx"))
ap.add_argument("--out", default=str(HERE / "output"))
ap.add_argument("--config", default=str(HERE / "cleaning_config.json"))
a = ap.parse_args()
OUT = Path(a.out)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def finish():
    w = max(len(n) for n, _, _ in results)
    fails = 0
    for name, ok, detail in results:
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(w)}  {'' if ok else detail}")
    print(f"\n{len(results) - fails}/{len(results)} checks passed")
    sys.exit(1 if fails else 0)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------------------- settings from config
def strip(o):
    return {k: strip(v) for k, v in o.items() if not k.startswith("_")} if isinstance(o, dict) else o


CFG = strip(json.loads(Path(a.config).read_text()))
LIKERT = CFG["likert_map"]                                   # label -> 1-5 or None (unscored)
UNRESOLVED = {l for l in CFG["likert_ambiguous_labels"]["labels"] if LIKERT.get(l) is None}
ROLE_KEY = CFG["role_map"]                                   # label -> key
ROLE_NAME = CFG["role_display"]                              # key -> display name
LANG = CFG["language_map"]                                   # prefix -> language
TH = CFG["thresholds"]
MIN_N, THIN_N = TH["small_group_min_n"], TH.get("thin_group_n", 30)
SLASH = CFG.get("dates", {}).get("slash_date_order")

# ------------------------------------------------------------------------------- load
EXPECTED_FILES = ["clean_responses.csv", "change_log.csv", "group_tables.csv", "data_quality_report.md", "clean_responses.xlsx", "run_manifest.json"]
missing_files = [f for f in EXPECTED_FILES if not (OUT / f).exists()]
check("bundle: every output file present", not missing_files, f"missing {missing_files}")
if missing_files:
    finish()

LIT = dict(keep_default_na=False, na_values=[""])           # keep literal answers such as "None" as text
def read_raw(path):
    if not path.lower().endswith(".csv"):
        return pd.read_excel(path, dtype=object, **LIT)
    for enc in ("utf-8-sig", "cp1252"):                       # UTF-8, then Excel's default CSV encoding on Windows
        try:
            return pd.read_csv(path, dtype=object, encoding=enc, **LIT)
        except UnicodeDecodeError:
            pass
    sys.exit(f"validator: {path} is neither UTF-8 nor Windows-1252 text")


raw = read_raw(a.raw)
clean = pd.read_csv(OUT / "clean_responses.csv", low_memory=False, **LIT)
clean_str = pd.read_csv(OUT / "clean_responses.csv", dtype=object, **LIT)   # exact text, no type guessing
for c in clean.columns:                                      # text and preserved originals must stay exact strings ("10 ", "007")
    if c.endswith(("_original", "_redacted")) or c in ("text_most_valuable", "text_improve"):
        clean[c] = clean_str[c]
log = pd.read_csv(OUT / "change_log.csv", **LIT)
gt = pd.read_csv(OUT / "group_tables.csv", **LIT)
report = (OUT / "data_quality_report.md").read_text()

# ------------------------------------------------------------------------------- 1. bundle integrity
try:
    manifest = json.loads((OUT / "run_manifest.json").read_text())
    ok = all(sha256(OUT / f) == h for f, h in manifest["files"].items()) and set(manifest["files"]) == set(EXPECTED_FILES) - {"run_manifest.json"}
    check("bundle: every file matches the run manifest (one run, nothing mixed or altered)", ok,
          str([f for f, h in manifest.get("files", {}).items() if (OUT / f).exists() and sha256(OUT / f) != h]))
    check("bundle: manifest was made from this raw file", manifest["input"]["sha256"] == sha256(a.raw))
    check("bundle: validating with the same config the run used", manifest["config"]["sha256"] == sha256(a.config),
          f"run used {manifest['config'].get('name')}; pass it with --config")
    check("bundle: manifest row count matches", manifest["rows"] == len(clean) == len(raw))
except (json.JSONDecodeError, KeyError) as e:
    check("bundle: run manifest readable", False, repr(e))

# ------------------------------------------------------------------------------- independent helpers
KEYS = {"id": None, "start": ["start time"], "end": ["completion time"], "lang": ["language"], "role": ["are you a"],
        "tdate": ["date of your training"], "county": ["which county"],
        "post_identify": ["disagree or agree", "can identify"], "post_describe": ["disagree or agree", "can describe"],
        "post_when": ["disagree or agree", "i know when"], "pre_identify": ["think back", "could identify"],
        "pre_describe": ["think back", "could describe"], "pre_when": ["think back", "knew when"],
        "sat_engaged": ["aspects of the training", "engaged"], "sat_knowledge": ["aspects of the training", "subject matter"],
        "sat_organized": ["aspects of the training", "organized"], "recommend": ["recommend"], "likely_call": ["likely are you to call"],
        "valuable": ["most valuable"], "improve": ["improve"]}


def nrm(v):
    s = str(v)
    for ch in ("\xa0", "‎", "​", "﻿"):
        s = s.replace(ch, " ")
    return " ".join(s.split()).lower()


def blank(v):
    return v is None or (not isinstance(v, str) and pd.isna(v)) or nrm(v) == ""


R = {}
for key, words in KEYS.items():
    hits = [c for c in raw.columns if (nrm(c) == "id" if words is None else all(w in nrm(c) for w in words))]
    if len(hits) != 1:
        sys.exit(f"validator: could not identify raw column '{key}' (matches: {hits})")
    R[key] = hits[0]

names_file = HERE / "redaction_names.local.json"
NAMES = json.loads(names_file.read_text()).get("names", []) if names_file.exists() else []
NAMES += CFG.get("redaction", {}).get("names", [])


def score(v):
    return None if blank(v) else LIKERT.get(nrm(v))        # unknown or unresolved labels -> unscored


def whole_0_10(v):
    if blank(v):
        return None
    try:
        x = float(str(v).strip())
    except ValueError:
        return None
    return int(x) if 0 <= x <= 10 and x == int(x) else None


def role_tokens(v):
    return [] if blank(v) else [nrm(p) for p in str(v).split(";") if nrm(p)]


def expected_role_group(v):
    toks = role_tokens(v)
    if any(t not in ROLE_KEY for t in toks):
        return "Includes unrecognized role"
    keys = {ROLE_KEY[t] for t in toks}
    return None if not keys else ROLE_NAME[next(iter(keys))] if len(keys) == 1 else "Multiple roles"


def expected_language(v):
    if blank(v):
        return None
    n = nrm(v)
    return next((lab for pre, lab in LANG.items() if n.startswith(pre)), "UNMAPPED")


def vdate(v):
    """Independent date reading (dateutil), used only to confirm dates the pipeline did parse."""
    if blank(v):
        return None
    if not isinstance(v, str):
        return pd.Timestamp(v)
    s = " ".join(v.split())
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}.*", s) and SLASH is None:
        return None
    ts = pd.Timestamp(dateparser.parse(s, dayfirst=(SLASH == "DMY")))
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


# ------------------------------------------------------------------------------- 2. rows and IDs
_rid = pd.to_numeric(raw[R["id"]], errors="coerce")
raw_ids = _rid.where(_rid % 1 == 0).astype("Int64")
check("row count preserved", len(raw) == len(clean), f"raw {len(raw)} vs clean {len(clean)}")
out_ids = clean["response_id"].astype("Int64")
if len(raw) == len(clean):
    same_ids = ((raw_ids.isna().values & out_ids.isna().values) | (raw_ids.values == out_ids.values).fillna(False)).all()
    check("same response IDs, in the same order", same_ids)
    check("source_row = spreadsheet row of each response", (clean["source_row"] == pd.RangeIndex(2, len(raw) + 2)).all())
    raw.index = clean.index = pd.RangeIndex(len(raw))
else:
    r_set, c_set = set(raw_ids.dropna()), set(out_ids.dropna())
    check("same set of response IDs", r_set == c_set, f"missing from output {sorted(r_set - c_set)[:10]}, not in raw {sorted(c_set - r_set)[:10]}")
    finish()
check("no duplicate IDs introduced", not out_ids.dropna().duplicated().any() or raw_ids.dropna().duplicated().any())
UNM = clean["unmapped_fields"].fillna("").astype(str)

# ------------------------------------------------------------------------------- 3. ratings
LIK = ["post_identify", "post_describe", "post_when", "pre_identify", "pre_describe", "pre_when", "sat_engaged", "sat_knowledge", "sat_organized"]
for col in LIK:
    exp = raw[R[col]].map(score)
    got = clean[col]
    mism = ~((exp.isna() & got.isna()) | (exp == got))
    check(f"{col}: every value matches raw label (per config)", mism.sum() == 0, f"{int(mism.sum())} mismatches")
    unknown = raw[R[col]].map(lambda v: not blank(v) and nrm(v) not in LIKERT)
    check(f"{col}: unknown labels blank and flagged", (got[unknown].isna() & UNM[unknown].str.contains(col)).all())
n_amb = int(sum(raw[R[c]].map(lambda v: not blank(v) and nrm(v) in UNRESOLVED).sum() for c in ["sat_engaged", "sat_knowledge", "sat_organized"]))
check("unscored ambiguous cells counted correctly in report", f"{n_amb} cells in" in report, f"expected {n_amb}")
for col, rc in [("recommend_0_10", "recommend"), ("likely_call_0_10", "likely_call")]:
    exp = raw[R[rc]].map(whole_0_10)
    got = clean[col]
    check(f"{col}: whole numbers 0-10 identical to raw, anything else blank", (~((exp.isna() & got.isna()) | (exp == got))).sum() == 0)
    bad = raw[R[rc]].map(lambda v: not blank(v)) & exp.isna()
    check(f"{col}: invalid answers flagged with original kept", (UNM[bad].str.contains(col) & clean.loc[bad, f"{col}_original"].notna()).all())

# ------------------------------------------------------------------------------- 4. categories
toks = raw[R["role"]].map(role_tokens)
for label, key in ROLE_KEY.items():
    check(f"is_{key}: true exactly when '{label}' was selected (exact token)",
          (toks.map(lambda ts, l=label: any(ROLE_KEY.get(t) == key for t in ts)) == clean[f"is_{key}"].astype(str).eq("True")).all())
exp_rg = raw[R["role"]].map(expected_role_group)
check("role_group matches selections (incl. 'Multiple roles' / 'Includes unrecognized role')",
      ((exp_rg.isna() & clean["role_group"].isna()) | (exp_rg == clean["role_group"])).all())
check("unrecognized role selections are flagged", UNM[exp_rg == "Includes unrecognized role"].str.contains("role_group").all())
ck = raw[R["county"]].map(lambda v: "" if blank(v) else nrm(v).replace("’", "'"))
first = clean["county"].astype("string").str.replace("UNRECOGNIZED:", "", regex=False).str.lower().str.split().str[0]
check("county: cleaned name is the start of the raw text", all((pd.isna(f) and r == "") or (pd.notna(f) and r.startswith(f)) for r, f in zip(ck, first)))
check("county: blank exactly when the raw cell is blank", (clean["county"].isna() == (ck == "")).all())
unrec = clean["county"].astype(str).str.startswith("UNRECOGNIZED")
check("county: unrecognized values flagged, never forced into a county", UNM[unrec].str.contains("county").all())
exp_lang = raw[R["lang"]].map(expected_language)
mapped = exp_lang.notna() & (exp_lang != "UNMAPPED")
check("language matches config prefixes", (clean.loc[mapped, "language"] == exp_lang[mapped]).all() and clean.loc[exp_lang.isna(), "language"].isna().all())

# ------------------------------------------------------------------------------- 5. dates
for col, rc in [("start_time", "start"), ("end_time", "end"), ("training_date", "tdate")]:
    got = pd.to_datetime(clean[col])
    parsed = got.notna()
    try:
        exp = raw.loc[parsed, R[rc]].map(vdate)
        same = all(e is not None and pd.Timestamp(e) == g for e, g in zip(exp, got[parsed]))
    except (ValueError, OverflowError):
        same = False
    check(f"{col}: every parsed value matches an independent reading of the raw cell", same)
    present = raw[R[rc]].map(lambda v: not blank(v))
    check(f"{col}: unreadable values are blank and flagged", UNM[present & ~parsed].str.contains(col).all())
st, en = pd.to_datetime(clean["start_time"]), pd.to_datetime(clean["end_time"])
dur = (en - st).dt.total_seconds()
check("duration_sec = end - start", ((dur.isna() & clean["duration_sec"].isna()) | (dur == clean["duration_sec"])).all())

# ------------------------------------------------------------------------------- 6. text
for col, rc in [("text_most_valuable", "valuable"), ("text_improve", "improve")]:
    exp = raw[R[rc]].map(lambda v: None if blank(v) else " ".join(str(v).replace("\xa0", " ").replace("‎", "").replace("​", "").replace("﻿", "").split()))
    got = clean[col]
    mism = [(i, e, g) for i, e, g in zip(raw.index, exp, got) if not (pd.isna(e) and pd.isna(g)) and e != g]
    check(f"{col}: wording identical to raw (whitespace only)", not mism, f"{len(mism)} mismatches {mism[:2]}")
    check(f"{col}_original: identical to raw export",
          all((blank(o) and blank(x)) or str(o) == str(x) for o, x in zip(clean[f"{col}_original"], raw[R[rc]])))
    check(f"{col}_type: 'blank' exactly when there is no text", ((clean[f"{col}_type"] == "blank") == got.isna()).all())
    red = clean[f"{col}_redacted"].dropna().astype(str)
    if NAMES:
        check(f"{col}_redacted: listed names removed (any capitalization)", not red.str.contains("|".join(re.escape(n) for n in NAMES), case=False).any())
    diff = got.notna() & (got != clean[f"{col}_redacted"])
    check(f"{col}_redacted: differs only where a token was inserted", clean.loc[diff, f"{col}_redacted"].astype(str).str.contains(r"\[(?:NAME|PHONE|EMAIL)\]").all())

# ------------------------------------------------------------------------------- 7. derived values and flags
for k in ["identify", "describe", "when"]:
    d = clean[f"post_{k}"] - clean[f"pre_{k}"]
    check(f"change_{k} = post - pre", ((d.isna() & clean[f"change_{k}"].isna()) | (d == clean[f"change_{k}"])).all())
check("no combined knowledge score in output", not any(c.startswith("knowledge_") for c in clean.columns))
B = lambda c: clean[c].astype(str).eq("True")                # noqa: E731  (booleans read back from CSV)
review = ["flag_possible_scale_reversal", "flag_possible_duplicate", "flag_repeated_id", "flag_fast_completion", "flag_long_completion"]
check("sensitivity sample = no review flag", (B("in_sensitivity_sample") == ~pd.concat([B(c) for c in review], axis=1).any(axis=1)).all())
check(f"fast flag = duration < {TH['fast_completion_seconds']}s (config)", (B("flag_fast_completion") == (dur < TH["fast_completion_seconds"])).all())
check(f"long flag = duration > {TH['long_completion_seconds']}s (config)", (B("flag_long_completion") == (dur > TH["long_completion_seconds"])).all())
post = clean[["post_identify", "post_describe", "post_when"]]
sd = post.notna().all(axis=1) & (post == 1).all(axis=1)
pos = (clean["recommend_0_10"] >= 7) | (clean["satisfaction_mean"] >= 4)
check("scale-reversal flag matches its definition", (B("flag_possible_scale_reversal") == (sd & pos.fillna(False))).all())
check("repeated-ID flag = ID present and repeated", (B("flag_repeated_id") == (out_ids.notna() & out_ids.duplicated(keep=False)).values).all())

# duplicates: each flagged pair must be identical on normalized raw answers, same session, within the window
def answer_key(i):
    row = raw.loc[i]
    return json.dumps([sorted(set(role_tokens(row[R["role"]]))), clean.loc[i, "county"] if pd.notna(clean.loc[i, "county"]) else None,
                       clean.loc[i, "language"] if pd.notna(clean.loc[i, "language"]) else None, str(clean.loc[i, "training_date"])]
                      + [None if blank(row[R[c]]) else nrm(row[R[c]]) for c in LIK + ["recommend", "likely_call"]]
                      + [None if pd.isna(clean.loc[i, c]) else clean.loc[i, c] for c in ["text_most_valuable", "text_improve"]], default=str)


row_of = {int(sr): i for i, sr in clean["source_row"].items()}
bad_dups = []
for i in clean.index[B("flag_possible_duplicate")]:
    j = row_of.get(int(clean.loc[i, "duplicate_of_row"])) if pd.notna(clean.loc[i, "duplicate_of_row"]) else None
    gap = (st[i] - st[j]).total_seconds() / 60 if j is not None else None
    if j is None or answer_key(i) != answer_key(j) or clean.loc[i, "session_id"] != clean.loc[j, "session_id"] or not (0 <= gap <= TH["duplicate_window_minutes"]):
        bad_dups.append(int(clean.loc[i, "source_row"]))
check("every flagged duplicate is identical to the response at duplicate_of_row", not bad_dups, f"rows {bad_dups[:10]}")

# ------------------------------------------------------------------------------- 8. change log
for col in ["post_identify", "sat_engaged", "county", "role_group", "recommend_0_10"]:
    check(f"change_log counts for {col} sum to row count", log.loc[log["column"] == col, "count"].sum() == len(raw))


# ------------------------------------------------------------------------------- 9. headline figures in the report
def pct_txt(num, n):
    return "n/a" if n == 0 else f"{100 * num / n:.1f}%"


check("report: likely-to-call blank count", f"{int(clean['likely_call_0_10'].isna().sum()):,} of {len(raw):,} blank" in report)
rec = raw[R["recommend"]].map(whole_0_10).dropna()
nps_txt = "n/a" if len(rec) == 0 else f"{round(100 * ((rec >= 9).sum() - (rec <= 6).sum()) / len(rec)):+d}"
check("report: overall NPS", f"| Likely to recommend: NPS | {nps_txt} | {len(rec)} |" in report, f"expected {nps_txt} ({len(rec)})")
for item, label in [("identify", "Identify mental health challenges and crises"), ("describe", "Describe 988 and how it works"), ("when", "Know when to use 988")]:
    pre = raw[R[f"pre_{item}"]].map(score).astype(float)
    pst = raw[R[f"post_{item}"]].map(score).astype(float)
    ok = pre.notna() & pst.notna()
    pre, pst = pre[ok], pst[ok]
    if ok.sum() == 0:
        want = f"| {label} | 0 | {len(raw)} |"
    else:
        b, a2 = 100 * (pre >= 4).mean(), 100 * (pst >= 4).mean()
        want = f"| {label} | {int(ok.sum())} | {int((~ok).sum())} | {b:.1f}% | {a2:.1f}% | {a2 - b:+.1f} pts | {pre.mean():.2f} | {pst.mean():.2f} | {(pst - pre).mean():+.2f}"
    check(f"report: knowledge row ({item})", want in report, want)
for col, label in [("sat_engaged", "Presenter engaged participants"), ("sat_knowledge", "Presenter knew the subject matter"), ("sat_organized", "Training was well organized")]:
    s = raw[R[col]].map(score).dropna()
    want = f"| {label}: % agree (4-5) | {pct_txt((s >= 4).sum(), len(s))} | {len(s)} | {len(raw) - len(s)} |"
    check(f"report: satisfaction row ({col})", want in report, want)
lcv = raw[R["likely_call"]].map(whole_0_10).dropna()
want = f"| Likely to call 988: % scoring 9-10 | {pct_txt((lcv >= 9).sum(), len(lcv))} | {len(lcv)} | {len(raw) - len(lcv)} |"
check("report: likely-to-call 9-10 row", want in report, want)
nsd = int(raw[[R["post_identify"], R["post_describe"], R["post_when"]]].apply(lambda r: all(score(v) == 1 for v in r), axis=1).sum())
check("report: all-post-strongly-disagree count", f"{nsd} responses rate all three after-training" in report)
cnt = exp_rg.value_counts()
for g in ["Student"]:
    if cnt.get(g, 0):
        check(f"report: composition '{g} | {cnt[g]}'", f"| {g} | {cnt[g]} |" in report)

# ------------------------------------------------------------------------------- 10. group tables
TABLES = {"Role": ("role_group", None), "County (training affiliation)": ("county", None), "Form language": ("language", None),
          "County, students only": ("county", "Student")}
METRICS = ["agree_change_identify", "agree_change_describe", "agree_change_when", "agree_sat_engaged", "agree_sat_knowledge",
           "agree_sat_organized", "recommend_nps", "likely_call_9_10"]
expected_tables = {t for t, (_, only) in TABLES.items() if only is None or (clean["role_group"] == only).any()}
check("group tables: exactly the expected tables are present", set(gt["table"]) == expected_tables,
      f"missing {sorted(expected_tables - set(gt['table']))}, unexpected {sorted(set(gt['table']) - expected_tables)}")


def recompute(ids, mid):
    """(valid n, value) for one measure, from the raw file."""
    if mid.startswith("agree_change_"):
        k = mid.split("_")[-1]
        pre, pst = raw.loc[ids, R[f"pre_{k}"]].map(score), raw.loc[ids, R[f"post_{k}"]].map(score)
        ok = pre.notna() & pst.notna()
        n = int(ok.sum())
        return n, (round(100 * (pst[ok] >= 4).mean() - 100 * (pre[ok] >= 4).mean(), 1) if n else None)
    if mid.startswith("agree_sat_"):
        s = raw.loc[ids, R[mid[len("agree_"):]]].map(score).dropna()
        return len(s), (round(100 * (s >= 4).mean(), 1) if len(s) else None)
    col = "recommend" if mid == "recommend_nps" else "likely_call"
    s = raw.loc[ids, R[col]].map(whole_0_10).dropna()
    if not len(s):
        return 0, None
    return len(s), round(100 * ((s >= 9).sum() - (s <= 6).sum()) / len(s), 1) if mid == "recommend_nps" else round(100 * (s >= 9).mean(), 1)


problems = {"membership": [], "metrics": [], "denominator": [], "value": [], "status": [], "label": []}
for table in expected_tables & set(gt["table"]):
    col, only = TABLES[table]
    scope = clean.index if only is None else clean.index[clean["role_group"] == only]
    labels = clean.loc[scope, col].fillna("(blank)").astype(str)
    t = gt[gt["table"] == table]
    groups = t.drop_duplicates("group")
    members = {g: m.split("; ") for g, m in zip(groups["group"], groups["members"])}
    flat = [m for ms in members.values() for m in ms]
    if sorted(flat) != sorted(labels.unique()) or len(flat) != len(set(flat)):
        problems["membership"].append(table)
        continue
    total = len(scope)
    for g, ms in members.items():
        ids = scope[labels.isin(ms).values]
        cells = t[t["group"] == g]
        if int(cells["group_n"].iloc[0]) != len(ids):
            problems["membership"].append((table, g))
        if sorted(cells["metric"]) != sorted(METRICS):
            problems["metrics"].append((table, g))
        for _, c in cells.iterrows():
            n, val = recompute(ids, c["metric"])
            if int(c["metric_n"]) != n:
                problems["denominator"].append((table, g, c["metric"], n, c["metric_n"]))
            shown = c["status"] == "shown"
            v = pd.to_numeric(c["value"], errors="coerce")
            if shown:
                if val is None or not math.isfinite(v) or abs(v - val) > 0.051:
                    problems["value"].append((table, g, c["metric"], val, c["value"]))
                if n < MIN_N and total >= MIN_N:
                    problems["status"].append((table, g, c["metric"], "shown with n<min"))
                if (c["cell_reliability"] == "thin") != (n < THIN_N):
                    problems["label"].append((table, g, c["metric"]))
            elif pd.notna(v):
                problems["status"].append((table, g, c["metric"], "suppressed cell has a value"))
    if total >= MIN_N:
        if (groups["group_n"] < MIN_N).any():
            problems["status"].append((table, "group under min shown"))
        hidden = t[t["status"].isin(["suppressed", "complementary"])].groupby("metric")["metric_n"].sum()
        if (hidden.between(1, MIN_N - 1)).any():
            problems["status"].append((table, "hidden cells recoverable by subtraction", hidden[hidden.between(1, MIN_N - 1)].to_dict()))
    elif (t["status"] == "shown").any():
        problems["status"].append((table, f"table has <{MIN_N} responses but shows values"))
check("group tables: groups partition the rows, with correct sizes", not problems["membership"], str(problems["membership"][:5]))
check("group tables: every group has every measure exactly once", not problems["metrics"], str(problems["metrics"][:5]))
check("group tables: every denominator recomputed from raw", not problems["denominator"], str(problems["denominator"][:5]))
check("group tables: every shown value is a number matching raw", not problems["value"], str(problems["value"][:5]))
check("group tables: suppression rules hold (no small cells, no subtraction leaks)", not problems["status"], str(problems["status"][:5]))
check(f"group tables: 'thin' label exactly when a figure's own n < {THIN_N}", not problems["label"], str(problems["label"][:5]))

# ------------------------------------------------------------------------------- 11. workbook (every value of every sheet)
XLSX_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def same_cell(x, expected):
    """x = value read from the workbook; expected = source value. Control characters an .xlsx can't hold
    are expected as U+FFFD; numbers compare by value (8 vs 8.0); timestamps compare as timestamps."""
    if isinstance(expected, str):
        expected = XLSX_ILLEGAL.sub("\ufffd", expected)
    if (blank(x) and blank(expected)) or str(x) == str(expected):
        return True
    num = (int, float)
    if isinstance(x, num) and not isinstance(x, bool) and isinstance(expected, str):
        try:
            return float(expected) == float(x) and expected.strip() == expected   # "4" vs 4, but "10 " is not 10
        except ValueError:
            return False
    if isinstance(x, num) and isinstance(expected, num) and not isinstance(x, bool) and not isinstance(expected, bool):
        return float(x) == float(expected)
    if hasattr(x, "year") and isinstance(expected, str):
        try:
            return pd.Timestamp(x) == pd.Timestamp(expected)
        except ValueError:
            return False
    return False


def frame_diffs(sheet_df, source_df):
    """List of (column, row, workbook value, expected) for every mismatch, plus column/row shape problems."""
    if list(sheet_df.columns) != [str(c) for c in source_df.columns]:
        return [("columns", None, list(sheet_df.columns)[:5], list(source_df.columns)[:5])]
    if len(sheet_df) != len(source_df):
        return [("rows", None, len(sheet_df), len(source_df))]
    out = []
    for i, c in enumerate(source_df.columns):
        for r, (x, e) in enumerate(zip(sheet_df.iloc[:, i], source_df.iloc[:, i])):
            if not same_cell(x, e):
                out.append((c, r + 2, x, e))
                break                                         # one example per column is enough
    return out


try:
    import openpyxl
    wb = openpyxl.load_workbook(OUT / "clean_responses.xlsx")
    expected_sheets = ["clean_responses", "data_dictionary", "change_log", "flag_summary", "group_tables", "raw_original"]
    check("workbook: opens with exactly the expected sheets", wb.sheetnames == expected_sheets, str(wb.sheetnames))
    formulas = [(ws.title, c.coordinate) for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"]
    check("workbook: no cell is an Excel formula (survey text stays text)", not formulas, str(formulas[:5]))

    def sheet(name):
        rows = list(wb[name].iter_rows(values_only=True)) if name in wb.sheetnames else [()]
        return pd.DataFrame(rows[1:], columns=[str(c) for c in rows[0]])

    flag_cols = [c for c in clean.columns if c.startswith(("flag_", "pattern_"))]
    expected_flags = pd.DataFrame({"flag": flag_cols, "responses": [int(B(c).sum()) for c in flag_cols]})
    expected_dict_cols = list(clean.columns)
    log_str = pd.read_csv(OUT / "change_log.csv", dtype=object, **LIT)
    gt_str = pd.read_csv(OUT / "group_tables.csv", dtype=object, **LIT)
    clean_str.index = clean.index
    for name, source in [("clean_responses", clean_str), ("change_log", log_str), ("group_tables", gt_str), ("raw_original", raw),
                         ("flag_summary", expected_flags)]:
        d = frame_diffs(sheet(name), source)
        check(f"workbook: every value in '{name}' matches its source", not d, str(d[:3])[:300])
    dd = sheet("data_dictionary")
    check("workbook: data dictionary lists every column with its non-blank count",
          list(dd.get("column", [])) == expected_dict_cols and
          all(int(n) == int(clean[c].notna().sum()) for c, n in zip(dd["column"], dd["non_blank"])) and dd["description"].notna().all())
except Exception as e:                                       # a corrupt or unreadable workbook is a failure, not a crash
    check("workbook: readable", False, repr(e)[:200])

finish()
