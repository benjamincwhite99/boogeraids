#!/usr/bin/env python3
"""Independent check of clean.py's output against the raw export.

Deliberately does NOT import clean.py: every expected value is re-derived from the raw file with
simple, separately written rules, then compared to output/. The only thing read from the config is
whether EveryMind has confirmed a score for an ambiguous label ('Very agree' / 'Satisfied').

    python3 validate_clean.py [raw.xlsx|raw.csv] [--out output] [--config cleaning_config.json]

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
ap.add_argument("--config", default=str(HERE / "cleaning_config.json"), help="only used to see which ambiguous labels EveryMind has confirmed")
a = ap.parse_args()

raw = (pd.read_csv(a.raw, dtype=object, keep_default_na=False, na_values=[""]) if a.raw.lower().endswith(".csv")
       else pd.read_excel(a.raw, dtype=object, keep_default_na=False, na_values=[""]))   # keep literal 'None'/'NA' answers
# keep_default_na=False: otherwise pandas turns literal answers like "None" back into blanks on read
clean = pd.read_csv(Path(a.out) / "clean_responses.csv", low_memory=False, keep_default_na=False, na_values=[""])
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
_rid = pd.to_numeric(raw[R["id"]], errors="coerce")
raw_ids = _rid.where(_rid % 1 == 0).astype("Int64")      # non-whole IDs (e.g. 1.5) count as unusable, like in the output

results = []
names_file = HERE / "redaction_names.local.json"   # gitignored list of real names to redact
NAMES = json.loads(names_file.read_text()).get("names", []) if names_file.exists() else []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


# 1. Rows and IDs ----------------------------------------------------------------
check("row count preserved", len(raw) == len(clean), f"raw {len(raw)} vs clean {len(clean)}")
out_ids = clean["response_id"].astype("Int64")
if len(raw) == len(clean):
    # clean.py keeps row order, so rows line up by position; this also works when an ID is blank or malformed
    same_ids = ((raw_ids.isna().values & out_ids.isna().values) | (raw_ids.values == out_ids.values).fillna(False)).all()
    check("same response IDs, in the same order", same_ids)
    raw.index = clean.index = pd.RangeIndex(len(raw))
else:
    # rows were added or lost: compare the IDs present in both
    r_set, c_set = set(raw_ids.dropna()), set(out_ids.dropna())
    check("same set of response IDs", r_set == c_set, f"missing from output {sorted(r_set - c_set)[:10]}, not in raw {sorted(c_set - r_set)[:10]}")
    raw, clean = raw.set_index(raw_ids), clean.set_index(out_ids)
    shared = [i for i in raw.index if i in c_set]
    raw, clean = raw.loc[shared], clean.loc[shared]
check("no duplicate IDs in output", not out_ids.dropna().duplicated().any() or raw_ids.dropna().duplicated().any())

# 2. Likert items: independent label -> score mapping ----------------------------
# The one thing read from the config: whether EveryMind has confirmed a score for an ambiguous label.
# Unconfirmed (null) labels must stay unscored; everything else below is written independently.
_cfg_map = json.loads(Path(a.config).read_text()).get("likert_map", {})
CONFIRMED = {l: _cfg_map.get(l) for l in ("very agree", "satisfied")}


def score(v):
    if pd.isna(v):
        return None
    s = " ".join(str(v).replace("\xa0", " ").split()).lower()
    table = {"strongly disagree": 1, "disagree": 2, "neither agree nor disagree": 3, "neither agree or disagree": 3,
             "agree": 4, "strongly agree": 5, **CONFIRMED}
    return table.get(s) if s else None   # unknown labels: the pipeline must leave them blank (and flag them)


for col in ["post_identify", "post_describe", "post_when", "pre_identify", "pre_describe", "pre_when",
            "sat_engaged", "sat_knowledge", "sat_organized"]:
    exp = raw[R[col]].map(score)
    got = clean[col]
    mismatch = ~((exp.isna() & got.isna()) | (exp == got))
    check(f"{col}: every value matches raw label", mismatch.sum() == 0, f"{int(mismatch.sum())} mismatches")
    check(f"{col}: blanks preserved (none filled)", exp.isna().sum() == got.isna().sum(), f"raw blanks {int(exp.isna().sum())}, clean {int(got.isna().sum())}")

amb = {l for l, sc in CONFIRMED.items() if sc is None}
n_amb = int(sum(raw[R[c]].map(lambda v: pd.notna(v) and " ".join(str(v).replace("\xa0", " ").split()).lower() in amb).sum()
                for c in ["sat_engaged", "sat_knowledge", "sat_organized"]))
check("ambiguous label cells counted correctly in report", f"{n_amb} cells in" in report, f"expected {n_amb}")

# 3. 0-10 scales -------------------------------------------------------------------
for col, rc in [("recommend_0_10", "recommend"), ("likely_call_0_10", "likely_call")]:
    exp = pd.to_numeric(raw[R[rc]], errors="coerce")
    exp = exp.where(exp.between(0, 10) & (exp % 1 == 0))       # only whole numbers 0-10 are valid
    got = clean[col]
    mismatch = ~((exp.isna() & got.isna()) | (exp == got))
    check(f"{col}: identical to raw", mismatch.sum() == 0, f"{int(mismatch.sum())} mismatches")
    check(f"{col}: within 0-10", got.dropna().between(0, 10).all())

# 4. Categorical fields ----------------------------------------------------------------
rl = raw[R["role"]].astype(str).str.lower()
for key, needle in [("is_student", "student"), ("is_school_staff", "school staff"), ("is_caregiver_parent", "caregiver"),
                    ("is_other_community", "other (e.g")]:
    check(f"{key} matches raw role text", (rl.str.contains(needle, regex=False) == clean[key].astype(bool)).all())
KNOWN_ROLES = {"student", "school staff member", "caregiver/parent", "other (e.g., community member)"}
parts = raw[R["role"]].map(lambda v: [] if pd.isna(v) else [" ".join(p.split()).lower() for p in str(v).split(";") if p.strip()])
has_unknown = parts.map(lambda ps: any(p not in KNOWN_ROLES for p in ps))
check("role_group = 'Multiple roles' exactly when >1 role and all recognized",
      (((parts.map(len) > 1) & ~has_unknown) == (clean["role_group"] == "Multiple roles")).all())
check("any unrecognized role selection -> 'Includes unrecognized role' and flagged",
      ((clean["role_group"] == "Includes unrecognized role") == has_unknown).all() and clean.loc[has_unknown.values, "flag_unmapped_value"].astype(bool).all())
check("role_group = 'Student' only for a single recognized 'Student' selection",
      ((clean["role_group"] == "Student") == parts.map(lambda ps: ps == ["student"])).all())

ck = raw[R["county"]].astype(str).str.replace("\xa0", " ").str.replace("’", "'").str.strip().str.lower()
first_word = clean["county"].astype("string").str.replace("UNRECOGNIZED:", "", regex=False).str.lower().str.split().str[0]
check("county: cleaned name is the start of the raw text",
      all((pd.isna(f) and r.strip() in ("", "nan", "none")) or (pd.notna(f) and r.startswith(f)) for r, f in zip(ck, first_word)))
check("county: blank only when the raw cell is blank or whitespace", (clean["county"].isna() == (ck.str.strip().isin(["", "nan"]))).all())
unrec = clean["county"].astype(str).str.startswith("UNRECOGNIZED")
check("county: every unrecognized value is flagged, none forced into a category", (clean.loc[unrec, "flag_unmapped_value"].astype(bool)).all())
check("language: Spanish exactly when raw starts with 'Español'",
      ((raw[R["lang"]].astype(str).str.startswith("Español")) == (clean["language"] == "Spanish")).all())

# 5. Text: wording unchanged ---------------------------------------------------------
for col, rc in [("text_most_valuable", "valuable"), ("text_improve", "improve")]:
    exp = raw[R[rc]].map(lambda v: None if pd.isna(v) or not " ".join(str(v).replace("\xa0", " ").split()) else " ".join(str(v).replace("\xa0", " ").split()))
    got = clean[col].where(clean[col].notna(), None)
    mismatch = [(i, e, g) for i, e, g in zip(raw.index, exp, got) if not (pd.isna(e) and pd.isna(g)) and e != g]
    check(f"{col}: wording identical to raw (whitespace only)", not mismatch, f"{len(mismatch)} mismatches {mismatch[:2]}")
    orig = clean[f"{col}_original"]
    check(f"{col}_original: identical to raw export", all((pd.isna(o) and pd.isna(x)) or str(o) == str(x) for o, x in zip(orig, raw[R[rc]])))
    typ = clean[f"{col}_type"]
    check(f"{col}_type: 'blank' exactly when there is no text", ((typ == "blank") == got.isna().values).all())
    red = clean[f"{col}_redacted"].dropna().astype(str)
    if NAMES:
        check(f"{col}_redacted: listed names removed (any capitalization)",
              not red.str.contains("|".join(re.escape(n) for n in NAMES), case=False).any())
    diff = clean[col].notna() & (clean[col] != clean[f"{col}_redacted"])
    check(f"{col}_redacted: differs from original only where a token was inserted",
          clean.loc[diff, f"{col}_redacted"].astype(str).str.contains(r"\[(?:NAME|PHONE|EMAIL)\]").all(), f"{int(diff.sum())} redacted")

# 6. Derived values ------------------------------------------------------------------
for k in ["identify", "describe", "when"]:
    diff = clean[f"post_{k}"] - clean[f"pre_{k}"]
    check(f"change_{k} = post - pre (blank if either is blank)", ((diff.isna() & clean[f"change_{k}"].isna()) | (diff == clean[f"change_{k}"])).all())
check("no combined knowledge score in output", not any(c.startswith("knowledge_") for c in clean.columns))
dur = (pd.to_datetime(raw[R["end"]], errors="coerce") - pd.to_datetime(raw[R["start"]], errors="coerce")).dt.total_seconds()
check("duration_sec matches raw timestamps", ((dur.isna().values & clean["duration_sec"].isna().values) | (dur.values == clean["duration_sec"].values)).all())
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
pos_of_id = {rid: pos for pos, rid in clean["response_id"].items() if pd.notna(rid)}   # duplicate_of_id holds a response ID
same = all(int(o) in pos_of_id and clean.loc[[i, pos_of_id[int(o)]], cols].astype(str).nunique().max() == 1
           for i, o in dups["duplicate_of_id"].items())
check("every flagged duplicate is truly identical to its original", same, f"{len(dups)} flagged")

# 7. Change log is complete --------------------------------------------------------------
for col in ["post_identify", "sat_engaged", "county", "role_group", "recommend_0_10"]:
    check(f"change_log counts for {col} sum to row count", log.loc[log["column"] == col, "count"].sum() == len(raw))

# 8. Headline numbers in the report, recomputed from raw ---------------------------------
blank_lc = int(clean["likely_call_0_10"].isna().sum())   # values themselves were checked against raw above
check("report: likely-to-call blank count", f"{blank_lc:,} of {len(raw):,} blank" in report)
rec = pd.to_numeric(raw[R["recommend"]], errors="coerce")
rec = rec.where(rec.between(0, 10) & (rec % 1 == 0)).dropna()
nps_txt = "n/a" if len(rec) == 0 else f"{round(100 * ((rec >= 9).sum() - (rec <= 6).sum()) / len(rec)):+d}"
check("report: overall NPS", f"| Likely to recommend: NPS | {nps_txt} | {len(rec)} |" in report, f"expected {nps_txt} ({len(rec)})")


def pct_txt(num, n):
    return "n/a" if n == 0 else f"{100 * num / n:.1f}%"


for item, label in [("identify", "Identify mental health challenges and crises"), ("describe", "Describe 988 and how it works"),
                    ("when", "Know when to use 988")]:
    pre = raw[R[f"pre_{item}"]].map(score).astype(float)
    post = raw[R[f"post_{item}"]].map(score).astype(float)
    ok = pre.notna() & post.notna()
    pre, post = pre[ok], post[ok]
    if ok.sum() == 0:
        want = f"| {label} | 0 | {len(raw)} |"
    else:
        b, a2 = 100 * (pre >= 4).mean(), 100 * (post >= 4).mean()
        want = f"| {label} | {int(ok.sum())} | {int((~ok).sum())} | {b:.1f}% | {a2:.1f}% | {a2 - b:+.1f} pts | {pre.mean():.2f} | {post.mean():.2f} | {(post - pre).mean():+.2f}"
    check(f"report: all-responses knowledge row ({item})", want in report, want)
for col, label in [("sat_engaged", "Presenter engaged participants"), ("sat_knowledge", "Presenter knew the subject matter"),
                   ("sat_organized", "Training was well organized")]:
    s = raw[R[col]].map(score).dropna()
    want = f"| {label}: % agree (4-5) | {pct_txt((s >= 4).sum(), len(s))} | {len(s)} | {len(raw) - len(s)} |"
    check(f"report: satisfaction row ({col})", want in report, want)
lcv = pd.to_numeric(raw[R["likely_call"]], errors="coerce")
lcv = lcv.where(lcv.between(0, 10) & (lcv % 1 == 0)).dropna()
want = f"| Likely to call 988: % scoring 9-10 | {pct_txt((lcv >= 9).sum(), len(lcv))} | {len(lcv)} | {len(raw) - len(lcv)} |"
check("report: likely-to-call 9-10 row", want in report, want)
nsd = int((raw[[R["post_identify"], R["post_describe"], R["post_when"]]].map(score) == 1).all(axis=1).sum())
check("report: all-post-strongly-disagree count", f"{nsd} responses rate all three after-training" in report)
cn = raw[R["county"]].astype(str).str.replace("\xa0", " ").str.replace("’", "'").str.strip().str.lower()
only_student = raw[R["role"]].astype(str).map(lambda s: [p.strip().lower() for p in s.split(";") if p.strip()] == ["student"])
stats = [f"Anne Arundel | {int(cn.str.startswith('anne arundel').sum())}", f"Prince George's | {int(cn.str.startswith('prince george').sum())}",
         f"Student | {int(only_student.sum())}"]
for stat in [s for s in stats if not s.endswith("| 0")]:      # groups absent from this export have no row
    check(f"report: composition '{stat}'", f"| {stat} |" in report)


# 9. Group tables: small-group rules and independent recomputation ------------------------
MIN_N = 10
gt = pd.read_csv(Path(a.out) / "group_tables.csv", keep_default_na=False, na_values=[""])
by_col = {"Role": "role_group", "County (training affiliation)": "county", "Form language": "language", "County, students only": "county"}
# A table whose groups total < 10 can't be protected by pooling; then everything in it must be suppressed.
tot = gt.drop_duplicates(["table", "group"]).groupby("table")["group_n"].sum()
tiny = set(tot[tot < MIN_N].index)
check("group tables: tables with < 10 responses in total show nothing", (gt.loc[gt["table"].isin(tiny), "status"] != "shown").all())
gt = gt[~gt["table"].isin(tiny)]
check("group tables: every displayed group has n >= 10", (gt["group_n"] >= MIN_N).all(), str(gt.loc[gt["group_n"] < MIN_N, ["table", "group"]].drop_duplicates().values.tolist()))
check("group tables: no value shown for a cell with n < 10", not ((gt["status"] == "shown") & (gt["metric_n"] < MIN_N)).any())
check("group tables: suppressed cells carry no value", gt.loc[gt["status"] != "shown", "value"].isna().all())
hidden = gt[gt["status"].isin(["suppressed", "complementary"])].groupby(["table", "metric"])["metric_n"].sum()
check("group tables: hidden cells per measure total 0 or >= 10 (no recovery by subtraction)", (hidden >= MIN_N).all(), str(hidden[hidden < MIN_N].to_dict()))
rec_raw = pd.to_numeric(raw[R["recommend"]], errors="coerce")
rec_raw = rec_raw.where(rec_raw.between(0, 10) & (rec_raw % 1 == 0))
lc_raw = pd.to_numeric(raw[R["likely_call"]], errors="coerce")
lc_raw = lc_raw.where(lc_raw.between(0, 10) & (lc_raw % 1 == 0))
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
