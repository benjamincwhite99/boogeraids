#!/usr/bin/env python3
"""Independent check of clean.py's output against the raw export.

Deliberately does NOT import clean.py or read its config: every expected value is re-derived
from the raw file with simple, separately written rules, then compared to output/.

    python3 validate_clean.py [raw.xlsx] [--out output]

Exits with status 1 if any check fails.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("raw", nargs="?", default=str(HERE / "BuildFest_EveryMind_Survey_Data_FULL.xlsx"))
ap.add_argument("--out", default=str(HERE / "output"))
a = ap.parse_args()

raw = (pd.read_csv(a.raw, dtype=object, keep_default_na=False, na_values=[""]) if a.raw.lower().endswith(".csv")
       else pd.read_excel(a.raw, dtype=object))
clean = pd.read_csv(Path(a.out) / "clean_responses.csv", low_memory=False)
report = (Path(a.out) / "data_quality_report.md").read_text()
log = pd.read_csv(Path(a.out) / "change_log.csv")

# Find raw columns by header keywords (written separately from clean.py's config, so column order doesn't matter).
KEYS = {"id": None, "start": ["start time"], "end": ["completion time"], "lang": ["language"], "role": ["are you a"],
        "tdate": ["date of your training"], "county": ["which county"],
        "post_identify": ["disagree or agree", "can identify"], "post_describe": ["disagree or agree", "can describe"],
        "post_when": ["disagree or agree", "i know when"], "pre_identify": ["think back", "could identify"],
        "pre_describe": ["think back", "could describe"], "pre_when": ["think back", "knew when"],
        "sat_engaged": ["aspects of the training", "engaged"], "sat_knowledge": ["aspects of the training", "subject matter"],
        "sat_organized": ["aspects of the training", "organized"], "recommend": ["recommend"], "likely_call": ["likely are you to call"],
        "valuable": ["most valuable"], "improve": ["improve"]}
R = {}
for key, words in KEYS.items():
    hits = [c for c in raw.columns if (str(c).strip().lower() == "id" if words is None
                                       else all(w in " ".join(str(c).replace("\xa0", " ").split()).lower() for w in words))]
    if len(hits) != 1:
        sys.exit(f"validator: could not identify raw column '{key}' (matches: {hits})")
    R[key] = hits[0]
raw = raw.set_index(raw[R["id"]].astype(int))
clean = clean.set_index("response_id")

results = []
names_file = HERE / "redaction_names.local.json"   # gitignored list of real names to redact
NAMES = json.loads(names_file.read_text()).get("names", []) if names_file.exists() else []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


# 1. Rows and IDs ----------------------------------------------------------------
check("row count preserved", len(raw) == len(clean), f"raw {len(raw)} vs clean {len(clean)}")
missing, added = sorted(set(raw.index) - set(clean.index)), sorted(set(clean.index) - set(raw.index))
check("same set of response IDs", not missing and not added, f"missing from output {missing[:10]}, not in raw {added[:10]}")
check("no duplicate IDs in output", not clean.index.duplicated().any())
shared = [i for i in raw.index if i in set(clean.index)]   # remaining checks run on IDs present in both
raw, clean = raw.loc[shared], clean.loc[shared]

# 2. Likert items: independent label -> score mapping ----------------------------
def score(v):
    if pd.isna(v):
        return None
    s = " ".join(str(v).replace("\xa0", " ").split()).lower()
    table = {"strongly disagree": 1, "disagree": 2, "neither agree nor disagree": 3, "neither agree or disagree": 3,
             "agree": 4, "strongly agree": 5, "very agree": None, "satisfied": None}   # ambiguous labels: unscored
    return table.get(s) if s else None   # unknown labels: the pipeline must leave them blank (and flag them)


for col in ["post_identify", "post_describe", "post_when", "pre_identify", "pre_describe", "pre_when",
            "sat_engaged", "sat_knowledge", "sat_organized"]:
    exp = raw[R[col]].map(score)
    got = clean[col]
    mismatch = ~((exp.isna() & got.isna()) | (exp == got))
    check(f"{col}: every value matches raw label", mismatch.sum() == 0, f"{int(mismatch.sum())} mismatches")
    check(f"{col}: blanks preserved (none filled)", exp.isna().sum() == got.isna().sum(), f"raw blanks {int(exp.isna().sum())}, clean {int(got.isna().sum())}")

amb = {"very agree", "satisfied"}
n_amb = int(sum(raw[R[c]].map(lambda v: pd.notna(v) and " ".join(str(v).replace("\xa0", " ").split()).lower() in amb).sum()
                for c in ["sat_engaged", "sat_knowledge", "sat_organized"]))
check("ambiguous label cells counted correctly in report", f"{n_amb} cells in" in report, f"expected {n_amb}")

# 3. 0-10 scales -------------------------------------------------------------------
for col, rc in [("recommend_0_10", "recommend"), ("likely_call_0_10", "likely_call")]:
    exp = pd.to_numeric(raw[R[rc]], errors="coerce")
    got = clean[col]
    mismatch = ~((exp.isna() & got.isna()) | (exp == got))
    check(f"{col}: identical to raw", mismatch.sum() == 0, f"{int(mismatch.sum())} mismatches")
    check(f"{col}: within 0-10", got.dropna().between(0, 10).all())

# 4. Categorical fields ----------------------------------------------------------------
rl = raw[R["role"]].astype(str).str.lower()
for key, needle in [("is_student", "student"), ("is_school_staff", "school staff"), ("is_caregiver_parent", "caregiver"),
                    ("is_other_community", "other (e.g")]:
    check(f"{key} matches raw role text", (rl.str.contains(needle, regex=False) == clean[key].astype(bool)).all())
n_roles = rl.map(lambda s: len([p for p in s.split(";") if p.strip()]))
check("role_group = 'Multiple roles' exactly when >1 role", ((n_roles > 1) == (clean["role_group"] == "Multiple roles")).all())

ck = raw[R["county"]].astype(str).str.replace("\xa0", " ").str.strip().str.lower()
first_word = clean["county"].str.replace("UNRECOGNIZED:", "", regex=False).str.lower().str.split().str[0]
check("county: cleaned name is the start of the raw text", all(r.startswith(f) for r, f in zip(ck, first_word)))
unrec = clean["county"].astype(str).str.startswith("UNRECOGNIZED")
check("county: every unrecognized value is flagged, none forced into a category", (clean.loc[unrec, "flag_unmapped_value"].astype(bool)).all())
check("language: Spanish exactly when raw starts with 'Español'",
      ((raw[R["lang"]].astype(str).str.startswith("Español")) == (clean["language"] == "Spanish")).all())

# 5. Text: wording unchanged ---------------------------------------------------------
for col, rc in [("text_most_valuable", "valuable"), ("text_improve", "improve")]:
    exp = raw[R[rc]].map(lambda v: None if pd.isna(v) or not " ".join(str(v).replace("\xa0", " ").split()) else " ".join(str(v).replace("\xa0", " ").split()))
    got = clean[col].where(clean[col].notna(), None)
    mismatch = [(i, e, g) for i, e, g in zip(raw.index, exp, got) if not (e is None and g is None) and e != g]
    check(f"{col}: wording identical to raw (whitespace only)", not mismatch, f"{len(mismatch)} mismatches {mismatch[:2]}")
    orig = clean[f"{col}_original"]
    check(f"{col}_original: identical to raw export", all((pd.isna(o) and pd.isna(x)) or str(o) == str(x) for o, x in zip(orig, raw[R[rc]])))
    typ = clean[f"{col}_type"]
    check(f"{col}_type: 'blank' exactly when there is no text", ((typ == "blank") == got.isna().values).all())
    red = clean[f"{col}_redacted"].dropna()
    if NAMES:
        check(f"{col}_redacted: listed names removed", not red.str.contains("|".join(re.escape(n) for n in NAMES)).any())
    diff = clean[col].notna() & (clean[col] != clean[f"{col}_redacted"])
    check(f"{col}_redacted: differs from original only where a token was inserted",
          clean.loc[diff, f"{col}_redacted"].str.contains(r"\[(?:NAME|PHONE|EMAIL)\]").all(), f"{int(diff.sum())} redacted")

# 6. Derived values ------------------------------------------------------------------
for k in ["identify", "describe", "when"]:
    diff = clean[f"post_{k}"] - clean[f"pre_{k}"]
    check(f"change_{k} = post - pre (blank if either is blank)", ((diff.isna() & clean[f"change_{k}"].isna()) | (diff == clean[f"change_{k}"])).all())
check("no combined knowledge score in output", not any(c.startswith("knowledge_") for c in clean.columns))
dur = (pd.to_datetime(raw[R["end"]]) - pd.to_datetime(raw[R["start"]])).dt.total_seconds()
check("duration_sec matches raw timestamps", (dur.values == clean["duration_sec"].values).all())
review = ["flag_possible_scale_reversal", "flag_possible_duplicate", "flag_repeated_id", "flag_fast_completion", "flag_long_completion"]
check("sensitivity sample = no review flag", (clean["in_sensitivity_sample"] == ~clean[review].any(axis=1)).all())
check("long flag = duration > 3600s", (clean["flag_long_completion"] == (dur.values > 3600)).all())
check("no column excludes rows by default", "include_in_core_sample" not in clean.columns)
sd = (clean[["post_identify", "post_describe", "post_when"]] == 1).all(axis=1)
pos = (clean["recommend_0_10"] >= 7) | (clean["satisfaction_mean"] >= 4)
check("scale-reversal flag matches its definition", (clean["flag_possible_scale_reversal"] == (sd & pos)).all())
check("fast flag = duration < 20s", (clean["flag_fast_completion"] == (dur.values < 20)).all())
dups = clean[clean["flag_possible_duplicate"]]
cols = ["post_identify", "post_describe", "post_when", "pre_identify", "pre_describe", "pre_when", "sat_engaged",
        "sat_knowledge", "sat_organized", "recommend_0_10", "likely_call_0_10", "text_most_valuable", "text_improve", "session_id"]
same = all(clean.loc[[i, int(o)], cols].astype(str).nunique().max() == 1 for i, o in dups["duplicate_of_id"].items())
check("every flagged duplicate is truly identical to its original", same, f"{len(dups)} flagged")

# 7. Change log is complete --------------------------------------------------------------
for col in ["post_identify", "sat_engaged", "county", "role_group", "recommend_0_10"]:
    check(f"change_log counts for {col} sum to row count", log.loc[log["column"] == col, "count"].sum() == len(raw))

# 8. Headline numbers in the report, recomputed from raw ---------------------------------
blank_lc = int(pd.to_numeric(raw[R["likely_call"]], errors="coerce").isna().sum())
check("report: likely-to-call blank count", f"{blank_lc:,} of {len(raw):,} blank" in report)
rec = pd.to_numeric(raw[R["recommend"]], errors="coerce").dropna()
nps = round(100 * ((rec >= 9).sum() - (rec <= 6).sum()) / len(rec))
check("report: overall NPS", f"| Likely to recommend: NPS | {nps:+d} | {len(rec)} |" in report, f"expected {nps:+d} ({len(rec)})")
for item, label in [("identify", "Identify mental health challenges and crises"), ("describe", "Describe 988 and how it works"),
                    ("when", "Know when to use 988")]:
    pre = raw[R[f"pre_{item}"]].map(score).astype(float)
    post = raw[R[f"post_{item}"]].map(score).astype(float)
    ok = pre.notna() & post.notna()
    pre, post = pre[ok], post[ok]
    b, a2 = 100 * (pre >= 4).mean(), 100 * (post >= 4).mean()
    want = f"| {label} | {int(ok.sum())} | {int((~ok).sum())} | {b:.1f}% | {a2:.1f}% | {a2 - b:+.1f} pts | {pre.mean():.2f} | {post.mean():.2f} | {(post - pre).mean():+.2f}"
    check(f"report: all-responses knowledge row ({item})", want in report, want)
nsd = int((raw[[R["post_identify"], R["post_describe"], R["post_when"]]].map(score) == 1).all(axis=1).sum())
check("report: all-post-strongly-disagree count", f"{nsd} responses rate all three after-training" in report)
cn = raw[R["county"]].astype(str).str.replace("\xa0", " ").str.strip().str.lower()
only_student = raw[R["role"]].astype(str).map(lambda s: [p.strip().lower() for p in s.split(";") if p.strip()] == ["student"])
for stat in [f"Anne Arundel | {int(cn.str.startswith('anne arundel').sum())}", f"Prince George's | {int(cn.str.startswith('prince george').sum())}",
             f"Student | {int(only_student.sum())}"]:
    check(f"report: composition '{stat}'", f"| {stat} |" in report)


# 9. Group tables: small-group rules and independent recomputation ------------------------
MIN_N = 10
gt = pd.read_csv(Path(a.out) / "group_tables.csv")
by_col = {"Role": "role_group", "County (training affiliation)": "county", "Form language": "language", "County, students only": "county"}
check("group tables: every displayed group has n >= 10", (gt["group_n"] >= MIN_N).all(), str(gt.loc[gt["group_n"] < MIN_N, ["table", "group"]].drop_duplicates().values.tolist()))
check("group tables: no value shown for a cell with n < 10", not ((gt["status"] == "shown") & (gt["metric_n"] < MIN_N)).any())
check("group tables: suppressed cells carry no value", gt.loc[gt["status"] != "shown", "value"].isna().all())
hidden = gt[gt["status"].isin(["suppressed", "complementary"])].groupby(["table", "metric"])["metric_n"].sum()
check("group tables: hidden cells per measure total 0 or >= 10 (no recovery by subtraction)", (hidden >= MIN_N).all(), str(hidden[hidden < MIN_N].to_dict()))
rec_raw = pd.to_numeric(raw[R["recommend"]], errors="coerce")
lc_raw = pd.to_numeric(raw[R["likely_call"]], errors="coerce")
bad = []
for (table, group), g in gt.groupby(["table", "group"]):
    rows = clean[by_col[table]].fillna("(blank)").astype(str).isin(g["members"].iloc[0].split("; "))
    if g["filter"].iloc[0] == "students only":
        rows &= clean["role_group"] == "Student"
    ids = clean.index[rows]
    if len(ids) != g["group_n"].iloc[0]:
        bad.append((table, group, "n")); continue
    for _, m in g[g["status"] == "shown"].iterrows():
        mid = m["metric"]
        if mid.startswith("agree_change_"):
            k = mid.split("_")[-1]
            pre, post = raw.loc[ids, R[f"pre_{k}"]].map(score), raw.loc[ids, R[f"post_{k}"]].map(score)
            ok = pre.notna() & post.notna()
            exp = round(100 * (post[ok] >= 4).mean() - 100 * (pre[ok] >= 4).mean(), 1)
        elif mid.startswith("agree_sat_"):
            s = raw.loc[ids, R[mid[len("agree_"):]]].map(score).dropna()
            exp = round(100 * (s >= 4).mean(), 1)
        elif mid == "recommend_nps":
            s = rec_raw.loc[ids].dropna()
            exp = round(100 * ((s >= 9).sum() - (s <= 6).sum()) / len(s), 1)
        else:
            s = lc_raw.loc[ids].dropna()
            exp = round(100 * (s >= 9).mean(), 1)
        if abs(exp - m["value"]) > 0.051:
            bad.append((table, group, mid, exp, m["value"]))
check("group tables: every shown figure recomputed from raw", not bad, str(bad[:5]))
for table, g in gt.drop_duplicates(["table", "group"]).groupby("table"):
    total = len(clean) if g["filter"].iloc[0] != "students only" else int((clean["role_group"] == "Student").sum())
    check(f"group tables: '{table}' groups add up to the total", g["group_n"].sum() == total)

# ---------------------------------------------------------------------------------------
w = max(len(n) for n, _, _ in results)
fails = 0
for name, ok, detail in results:
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(w)}  {'' if ok else detail}")
print(f"\n{len(results) - fails}/{len(results)} checks passed")
sys.exit(1 if fails else 0)
