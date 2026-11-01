#!/usr/bin/env python3
"""EveryMind "Understanding 988" post-training survey: cleaning pipeline and data-quality report.

Usage
-----
    python3 clean.py                                   # uses the default export in this folder
    python3 clean.py path/to/new_export.xlsx           # a newer Microsoft Forms export (.xlsx or .csv)
    python3 clean.py export.xlsx --config cleaning_config.json --out output

Outputs (in --out, default ./output)
    clean_responses.csv        one row per survey response, cleaned + flag_* columns
    clean_responses.xlsx       same data plus data dictionary, change log, flag summary and the untouched raw export
    change_log.csv             every raw value -> cleaned value mapping, with counts and the rule applied
    group_tables.csv           every group comparison in the report, long format, with suppression status
    data_quality_report.md     what was found, what was changed, and baseline figures with sample sizes

Principles
    * The source file is never modified. No rows are dropped and no values are imputed. Row count in == row count out.
    * Judgment calls (speeders, possible duplicates, suspected scale reversal) are flags, never exclusions.
    * Values no rule covers (unknown labels, unreadable dates, decimal ratings, ...) are left blank and flagged.
    * Ambiguous rating labels stay unscored until a score is confirmed in the config.
    * Every mapping lives in cleaning_config.json so new exports can be processed without code edits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "BuildFest_EveryMind_Survey_Data_FULL.xlsx"

KNOWLEDGE_ITEMS = {
    "identify": "Identify mental health challenges and crises",
    "describe": "Describe 988 and how it works",
    "when": "Know when to use 988",
}
SAT_ITEMS = {
    "sat_engaged": "Presenter engaged participants",
    "sat_knowledge": "Presenter knew the subject matter",
    "sat_organized": "Training was well organized",
}
LIKERT_COLS = [f"post_{k}" for k in KNOWLEDGE_ITEMS] + [f"pre_{k}" for k in KNOWLEDGE_ITEMS] + list(SAT_ITEMS)
TEXT_COLS = ["text_most_valuable", "text_improve"]
REVIEW_FLAGS = ["flag_possible_scale_reversal", "flag_possible_duplicate", "flag_repeated_id", "flag_fast_completion", "flag_long_completion"]


# --------------------------------------------------------------------------- helpers
def norm(value) -> str:
    """Lower-case, strip invisible characters and collapse whitespace."""
    s = str(value).replace("\xa0", " ").replace("‎", "").replace("​", "").replace("﻿", "")
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def clean_text(value):
    if pd.isna(value):
        return pd.NA
    s = str(value).replace("\xa0", " ").replace("‎", "").replace("​", "").replace("﻿", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s if s else pd.NA


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_comments(obj):
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    return obj


def load_local_names(red: dict) -> list:
    """Names of real people to redact, kept in a gitignored local file so they are never committed."""
    f = HERE / red.get("names_file", "redaction_names.local.json")
    if not f.exists():
        print(f"NOTE: {f.name} not found; only the honorific pattern (Mr./Ms./Dr. ...) is used to redact names.")
        return []
    return json.loads(f.read_text()).get("names", [])


class ChangeLog:
    def __init__(self):
        self.rows = []

    def add_pairs(self, column: str, raw: pd.Series, cleaned: pd.Series, rule: str, same=None, display=None):
        """Record every distinct raw -> cleaned pair with its count.

        kind = 'unchanged' (same value), 'recode' (expected label -> score, e.g. 'Agree' -> 4),
        or 'correction' (a variant, typo or formatting issue was fixed). `same(raw, cleaned)` decides
        whether a pair is a pure recode; by default values are compared after whitespace/case normalization.
        """
        same = same or (lambda r, c: norm(r) == norm(c))
        display = display or (lambda c: str(c))
        blank = object()
        pairs = pd.DataFrame({"raw": raw.map(lambda v: blank if pd.isna(v) else v),
                              "cleaned": cleaned.map(lambda v: blank if pd.isna(v) else v)})
        counts = pairs.groupby(["raw", "cleaned"], sort=False, dropna=False).size()
        for (r, c), n in counts.items():
            if r is blank and c is blank:
                kind = "unchanged"
            elif r is blank or c is blank:
                kind = "correction"
            elif str(r) == display(c):
                kind = "unchanged"
            else:
                dirty = isinstance(r, str) and clean_text(r) != r
                if isinstance(r, str) and clean_text(r) == display(c):
                    kind = "formatting"
                elif same(r, c):
                    kind = "recode + formatting" if dirty else "recode"
                else:
                    kind = "correction"
            self.rows.append({"column": column,
                              "raw_value": "<blank>" if r is blank else repr(r) if isinstance(r, str) else str(r),
                              "cleaned_value": "<blank>" if c is blank else display(c),
                              "count": int(n), "kind": kind, "rule": rule})

    def frame(self) -> pd.DataFrame:
        f = pd.DataFrame(self.rows, columns=["column", "raw_value", "cleaned_value", "count", "kind", "rule"])
        return f.sort_values(["column", "kind", "raw_value"]).reset_index(drop=True)


# --------------------------------------------------------------------------- loading
def load_raw(path: Path) -> pd.DataFrame:
    """Only truly empty cells become missing. pandas' default would also turn literal answers such as
    'None', 'NA' or 'null' into blanks, and those are real comments."""
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=object, keep_default_na=False, na_values=[""])
    return pd.read_excel(path, dtype=object, keep_default_na=False, na_values=[""])


def map_columns(raw: pd.DataFrame, spec: dict) -> tuple[dict, list]:
    """Return {clean_name: raw_header} and the list of raw headers that matched nothing."""
    found, problems = {}, []
    normed = {h: norm(h) for h in raw.columns}
    for key, rule in spec.items():
        if "exact" in rule:
            hits = [h for h, n in normed.items() if n == rule["exact"].lower()]
        else:
            hits = [h for h, n in normed.items() if all(s in n for s in rule["all"])]
        if len(hits) != 1:
            problems.append(f"{key}: expected 1 matching column, found {len(hits)} {hits}")
        else:
            found[key] = hits[0]
    if problems:
        sys.exit("Column mapping failed. Update 'columns' in the config:\n  " + "\n  ".join(problems))
    extra = [h for h in raw.columns if h not in found.values()]
    return found, extra


# --------------------------------------------------------------------------- cleaning
def clean(raw: pd.DataFrame, cfg: dict, log: ChangeLog) -> tuple[pd.DataFrame, dict]:
    colmap, extra = map_columns(raw, cfg["columns"])
    r = raw.rename(columns={v: k for k, v in colmap.items()})
    df = pd.DataFrame(index=r.index)
    meta = {"column_map": colmap, "extra_columns": extra}
    unmapped = {}   # cleaned column -> boolean mask of rows whose raw value matched no rule in the config
    th = cfg["thresholds"]

    # IDs ------------------------------------------------------------------
    def present(col):
        """True where the raw cell has something in it (not blank or whitespace-only)."""
        return r[col].map(lambda v: pd.notna(v) and str(v).strip() != "")

    df["response_id"] = pd.to_numeric(r["response_id"], errors="coerce").astype("Int64")
    unmapped["response_id"] = present("response_id") & df["response_id"].isna()

    # Timestamps & dates ---------------------------------------------------
    df["start_time"] = pd.to_datetime(r["start_time"], errors="coerce")
    df["end_time"] = pd.to_datetime(r["end_time"], errors="coerce")
    df["duration_sec"] = (df["end_time"] - df["start_time"]).dt.total_seconds()
    df["survey_date"] = df["start_time"].dt.normalize()
    df["survey_month"] = df["start_time"].dt.strftime("%Y-%m")          # blank (not "NaT") if start time is missing
    df["training_date"] = pd.to_datetime(r["training_date"], errors="coerce")
    df["days_survey_after_training"] = (df["survey_date"] - df["training_date"]).dt.days.astype("Int64")
    for c in ["start_time", "end_time", "training_date"]:           # entered but not a date: blank + flagged, never guessed
        unmapped[c] = present(c) & df[c].isna()

    # Language -------------------------------------------------------------
    def lang(v):
        if pd.isna(v) or norm(v) == "":
            return pd.NA
        n = norm(v)
        for prefix, label in cfg["language_map"].items():
            if n.startswith(prefix):
                return label
        return "UNMAPPED:" + n

    df["language"] = r["language_raw"].map(lang)
    unmapped["language"] = df["language"].astype(str).str.startswith("UNMAPPED:")
    log.add_pairs("language", r["language_raw"], df["language"], "match label prefix (language_map)", same=lambda raw_v, v: True)

    # Role (multi-select) ----------------------------------------------------
    role_map, role_disp = cfg["role_map"], cfg["role_display"]

    def parse_roles(v):
        if pd.isna(v):
            return []
        keys = []
        for part in str(v).split(";"):
            p = norm(part)
            if not p:
                continue
            if p in role_map:
                keys.append(role_map[p])
        return sorted(set(keys))

    roles = r["role_raw"].map(parse_roles)
    for key in role_map.values():
        df[f"is_{key}"] = roles.map(lambda ks, k=key: k in ks)
    df["role_count"] = roles.map(len)
    df["role_group"] = roles.map(lambda ks: pd.NA if not ks else role_disp[ks[0]] if len(ks) == 1 else "Multiple roles")
    df["roles_selected"] = roles.map(lambda ks: "; ".join(role_disp[k] for k in ks) if ks else pd.NA)
    log.add_pairs("role_group", r["role_raw"], df["role_group"], "split on ';', drop empty parts, map labels (role_map)",
                  same=lambda raw_v, v: norm(raw_v) in role_map and role_disp[role_map[norm(raw_v)]] == v)
    unmapped["role_group"] = r["role_raw"].map(lambda v: pd.notna(v) and any(norm(p) and norm(p) not in role_map for p in str(v).split(";")))

    # County ---------------------------------------------------------------
    aliases = cfg["county_aliases"]

    def county(v):
        if pd.isna(v):
            return pd.NA
        k = re.sub(r"\bcounty\b", " ", norm(v).replace("’", "'").replace("‘", "'"))   # curly apostrophes, e.g. Prince George’s
        k = re.sub(r"\s+", " ", k).strip()
        if not norm(v):
            return pd.NA                                              # whitespace-only = missing
        return aliases.get(k, "UNRECOGNIZED:" + str(v).strip())

    df["county"] = r["county_raw"].map(county)
    df["county_note"] = r["county_raw"].map(lambda v: "Entered as 'with NAMI MC' (partner)" if pd.notna(v) and "nami" in norm(v) else pd.NA)
    md = set(cfg["maryland_counties"])
    unmapped["county"] = df["county"].astype(str).str.startswith("UNRECOGNIZED:")
    df["flag_county_outside_md"] = df["county"].notna() & ~df["county"].isin(md) & ~unmapped["county"]
    log.add_pairs("county", r["county_raw"], df["county"], "strip spaces/NBSP, drop word 'County', apply county_aliases")

    # Likert items ---------------------------------------------------------
    lmap = {k: v for k, v in cfg["likert_map"].items()}
    # A listed label counts as ambiguous only while it is unresolved (null in likert_map). Once EveryMind
    # confirms a meaning and a score is entered, it is scored, logged and reported like any other label.
    ambiguous = {l for l in cfg["likert_ambiguous_labels"]["labels"] if lmap.get(l) is None}
    meta["ambiguous_labels"] = sorted(ambiguous)
    meta["resolved_labels"] = sorted(set(cfg["likert_ambiguous_labels"]["labels"]) - ambiguous)
    canon = {int(k): v for k, v in cfg["likert_canonical_labels"].items()}
    df["flag_ambiguous_label"] = False
    meta["ambiguous_cells"] = {}
    unscored = "unscored (ambiguous label)"
    for c in LIKERT_COLS:
        n = r[c].map(lambda v: pd.NA if pd.isna(v) or norm(v) == "" else norm(v))   # whitespace-only = missing
        unmapped[c] = n.notna() & ~n.isin(list(lmap))
        df[c] = n.map(lambda x: pd.NA if pd.isna(x) or lmap.get(x) is None else lmap[x]).astype("Int64")
        amb = n.isin(ambiguous).fillna(False).astype(bool)
        df["flag_ambiguous_label"] |= amb
        meta["ambiguous_cells"][c] = int(amb.sum())
        log.add_pairs(c, r[c], df[c].astype(object).where(~amb, unscored),
                      "normalize whitespace/NBSP/case, map to 1-5 (likert_map); ambiguous labels left unscored",
                      same=lambda raw_v, v: not isinstance(v, str) and norm(raw_v) == canon[int(v)].lower(),
                      display=lambda v: v if isinstance(v, str) else f"{int(v)} = {canon[int(v)]}")

    # 0-10 scales ----------------------------------------------------------
    for c in ["recommend_0_10", "likely_call_0_10"]:
        has_value = present(c)
        num = pd.to_numeric(r[c].where(has_value), errors="coerce")
        valid = num.notna() & num.between(0, 10) & (num % 1 == 0)          # whole numbers 0-10 only
        unmapped[c] = has_value & ~valid                                   # non-numeric, decimal or out of range
        df[f"{c}_original"] = r[c]
        df[c] = num.where(valid).round().astype("Int64")
        log.add_pairs(c, r[c], df[c], "whole number 0-10; anything else -> blank and flagged (flag_unmapped_value)",
                      same=lambda raw_v, v: float(raw_v) == float(v), display=lambda v: str(int(v)))
    rec = df["recommend_0_10"]
    df["recommend_category"] = pd.Series(pd.NA, index=df.index, dtype="object")
    df.loc[rec.notna() & (rec >= 9), "recommend_category"] = "Promoter (9-10)"
    df.loc[rec.notna() & rec.between(7, 8), "recommend_category"] = "Passive (7-8)"
    df.loc[rec.notna() & (rec <= 6), "recommend_category"] = "Detractor (0-6)"

    # Self-rated change per item (retrospective: "before" was recalled after training). The three items
    # are kept separate on purpose; no combined score is created.
    for k in KNOWLEDGE_ITEMS:
        df[f"change_{k}"] = (df[f"post_{k}"] - df[f"pre_{k}"]).astype("Int64")
    post = [f"post_{k}" for k in KNOWLEDGE_ITEMS]
    pre = [f"pre_{k}" for k in KNOWLEDGE_ITEMS]
    changes = [f"change_{k}" for k in KNOWLEDGE_ITEMS]
    complete_k = df[post + pre].notna().all(axis=1)
    sat = list(SAT_ITEMS)
    # "answered" = the respondent gave any answer (including an ambiguous label); "scored" = it has a 1-5 score
    df["sat_items_answered"] = r[sat].apply(lambda col: col.map(lambda v: pd.notna(v) and norm(v) != "")).sum(axis=1)
    df["sat_items_scored"] = df[sat].notna().sum(axis=1)
    df["satisfaction_mean"] = df[sat].astype(float).mean(axis=1).where(df["sat_items_scored"] == 3).round(3)

    # Text ------------------------------------------------------------------
    def text_key(s):
        s = str(s).lower().replace("’", "").replace("'", "")
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9ñáéíóú ]", " ", s)).strip()

    rtypes = {t: {text_key(p) for p in phrases} for t, phrases in cfg["comment_response_types"].items()}

    def response_type(s):
        if pd.isna(s):
            return "blank"
        k = text_key(s)
        if not k:
            return "no_content"          # e.g. only punctuation
        for t, phrases in rtypes.items():
            if k in phrases:
                return t
        return "substantive"

    red = cfg["redaction"]
    names = sorted(set(red.get("names", [])) | set(load_local_names(red)), key=len, reverse=True)
    meta["redaction_names_loaded"] = len(names)
    honor = re.compile(red["honorific_pattern"])
    allow = set(red["public_phone_allowlist"])
    phone = re.compile(r"(?<!\d)(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})(?!\d)")
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

    def redact(s):
        if pd.isna(s):
            return s
        out = honor.sub("[NAME]", s)
        for nm in names:
            out = re.sub(rf"\b{re.escape(nm)}\b", "[NAME]", out, flags=re.IGNORECASE)
        out = email.sub("[EMAIL]", out)
        out = phone.sub(lambda m: m.group(0) if re.sub(r"\D", "", m.group(0)) in allow else "[PHONE]", out)
        return out

    df["flag_text_redacted"] = False
    for c in TEXT_COLS:
        df[f"{c}_original"] = r[c]                     # exactly as exported
        df[c] = r[c].map(clean_text)                   # whitespace/invisible characters only
        df[f"{c}_type"] = df[c].map(response_type)
        df[f"{c}_redacted"] = df[c].map(redact)
        changed = df[c].notna() & (df[c] != df[f"{c}_redacted"])
        df["flag_text_redacted"] |= changed.fillna(False).astype(bool)
        meta[f"{c}_whitespace_changed"] = int((r[c].notna() & (r[c].astype(str) != df[c].astype(str))).sum())

    # Session ---------------------------------------------------------------
    df["session_id"] = df["training_date"].dt.strftime("%Y-%m-%d").fillna("unknown-date") + " / " + df["county"].fillna("unknown")
    df["session_n"] = df.groupby("session_id")["response_id"].transform("size")

    # Which optional blocks did this response contain? Missingness turns out to be session-level
    # (possibly form versions), so coverage is reported per session in the data-quality report.
    has_sat = df["sat_items_answered"] > 0
    has_lc = df["likely_call_0_10"].notna()
    df["answered_blocks"] = (has_sat.map({True: "satisfaction", False: ""}) + has_lc.map({True: "+likely_call", False: ""})).str.strip("+").replace("", "neither")
    df["session_likely_call_coverage"] = has_lc.groupby(df["session_id"]).transform("mean").round(3)
    df["session_satisfaction_coverage"] = has_sat.groupby(df["session_id"]).transform("mean").round(3)

    # Quality / pattern flags ----------------------------------------------
    df["flag_training_date_missing"] = ~present("training_date")      # truly blank; unreadable dates are flag_unmapped_value
    d = df["days_survey_after_training"]
    df["flag_survey_before_training"] = d.notna() & (d < 0)
    df["flag_survey_late"] = d.notna() & (d > th["late_survey_days"])
    df["flag_fast_completion"] = df["duration_sec"] < th["fast_completion_seconds"]
    df["flag_long_completion"] = df["duration_sec"] > th["long_completion_seconds"]
    df["flag_repeated_id"] = df["response_id"].notna() & df["response_id"].duplicated(keep=False)
    allk = LIKERT_COLS
    df["flag_straightline_all9"] = df[allk].notna().all(axis=1) & (df[allk].nunique(axis=1) == 1)
    all_post_sd = df[post].notna().all(axis=1) & (df[post] == 1).fillna(False).all(axis=1)   # all three answered and = 1
    positive_elsewhere = (df["recommend_0_10"] >= 7).fillna(False) | (df["satisfaction_mean"] >= 4).fillna(False)
    df["flag_possible_scale_reversal"] = all_post_sd & positive_elsewhere
    df["pattern_all_post_strongly_disagree"] = all_post_sd
    df["pattern_any_decline"] = (df[changes] < 0).any(axis=1).fillna(False)
    df["pattern_no_change"] = complete_k & (df[changes] == 0).all(axis=1).fillna(False)

    # Possible duplicate submissions: identical on every answer, same session, within the time window.
    answer_cols = ["role_group", "county", "language", "training_date"] + LIKERT_COLS + ["recommend_0_10", "likely_call_0_10"] + TEXT_COLS
    key = df[answer_cols].astype(str).agg("|".join, axis=1)
    df["duplicate_of_id"] = pd.Series(pd.NA, index=df.index, dtype="Int64")
    window = pd.Timedelta(minutes=th["duplicate_window_minutes"])
    for _, grp in df.assign(_k=key).sort_values("start_time").groupby("_k"):
        if len(grp) < 2:
            continue
        first = grp.iloc[0]
        for idx, row in grp.iloc[1:].iterrows():
            if row["start_time"] - first["start_time"] <= window:
                df.at[idx, "duplicate_of_id"] = first["response_id"]
            else:
                first = row
    df["flag_possible_duplicate"] = df["duplicate_of_id"].notna()

    um = pd.DataFrame(unmapped).fillna(False).astype(bool)
    df["unmapped_fields"] = um.apply(lambda row: ", ".join(c for c in um.columns if row[c]) or pd.NA, axis=1)
    df["flag_unmapped_value"] = um.any(axis=1)
    src = {"language": "language_raw", "role_group": "role_raw", "county": "county_raw"}
    meta["unmapped"] = [{"column": c, "raw_value": repr(v), "rows": int(k)}
                        for c in um.columns if um[c].any()
                        for v, k in r.loc[um[c], src.get(c, c)].value_counts().items()]

    # Primary results use every valid response. Review flags are never auto-excluded; the report repeats
    # key results without them (sensitivity check) so readers can see whether they matter.
    df["review_flag_count"] = df[REVIEW_FLAGS].sum(axis=1)
    df["in_sensitivity_sample"] = df["review_flag_count"] == 0

    meta["fast_counts"] = {s: int((df["duration_sec"] < s).sum()) for s in (15, 20, 25, 30)}
    return df, meta


# --------------------------------------------------------------------------- reporting
def md_table(frame: pd.DataFrame, index: bool = False) -> str:
    f = frame.reset_index() if index else frame
    cols = [str(c) for c in f.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, row in f.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in row.values) + " |")
    return "\n".join(lines)


def pct(num, den) -> str:
    return "n/a" if den == 0 else f"{100 * num / den:.1f}%"


def mean_ci(s: pd.Series):
    s = s.dropna().astype(float)
    n = len(s)
    if n == 0:
        return n, math.nan, math.nan, math.nan
    m = s.mean()
    se = s.std(ddof=1) / math.sqrt(n) if n > 1 else math.nan
    return n, m, m - 1.96 * se, m + 1.96 * se


def fmt_ci(s: pd.Series, dp=2) -> str:
    n, m, lo, hi = mean_ci(s)
    return "n/a" if n == 0 else f"{m:+.{dp}f} [{lo:+.{dp}f}, {hi:+.{dp}f}]"


def item_pairs(d: pd.DataFrame, k: str) -> pd.DataFrame:
    """Valid before/after pairs for one knowledge item (same respondent, both answered)."""
    return d[[f"pre_{k}", f"post_{k}"]].dropna().astype(float)


def knowledge_table(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for k, label in KNOWLEDGE_ITEMS.items():
        both = item_pairs(d, k)
        n = len(both)
        if n == 0:
            rows.append({"Item": label, "Valid pairs": 0, "Missing": len(d)})
            continue
        g = both[f"post_{k}"] - both[f"pre_{k}"]
        b, a = 100 * (both[f"pre_{k}"] >= 4).mean(), 100 * (both[f"post_{k}"] >= 4).mean()
        rows.append({
            "Item": label, "Valid pairs": n, "Missing": len(d) - n,
            "Before: % agree (4-5)": f"{b:.1f}%", "After: % agree (4-5)": f"{a:.1f}%", "Change in % agree": f"{a - b:+.1f} pts",
            "Mean before": f"{both[f'pre_{k}'].mean():.2f}", "Mean after": f"{both[f'post_{k}'].mean():.2f}",
            "Mean self-rated change [95% CI]": fmt_ci(g),
            "% rated higher": pct((g > 0).sum(), n), "% same": pct((g == 0).sum(), n), "% rated lower": pct((g < 0).sum(), n),
        })
    return pd.DataFrame(rows)


def nps_summary(s: pd.Series) -> dict:
    s = s.dropna()
    n = len(s)
    if n == 0:
        return {"n": 0, "nps": "n/a", "promoters": "n/a", "passives": "n/a", "detractors": "n/a"}
    p, d = (s >= 9).sum(), (s <= 6).sum()
    return {"n": n, "promoters": pct(p, n), "passives": pct(n - p - d, n), "detractors": pct(d, n),
            "nps_value": 100 * (p - d) / n, "nps": f"{100 * (p - d) / n:+.0f}"}


def reliability(n: int, min_n: int, thin_n: int) -> str:
    if n < min_n:
        return f"Too small (n<{min_n})"
    return f"Thin: descriptive only (n<{thin_n})" if n < thin_n else "OK"


def pool_small_groups(labels: pd.Series, min_n: int):
    """Pool groups with n < min_n into one 'Other (...)' group. If the pool is still under min_n, the
    smallest reportable group joins it. Every displayed group then has n >= min_n, so a small group's
    result can't be recovered by subtracting displayed groups from a total."""
    labels = labels.fillna("(blank)").astype(str)
    sizes = labels.value_counts()
    small = list(sizes[sizes < min_n].index)
    if not small:
        return labels, {g: [g] for g in sizes.index}
    big = sizes[sizes >= min_n].sort_values()
    if sizes[small].sum() < min_n and len(big):
        small.append(big.index[0])
    pooled = "Other (" + ", ".join(f"{g} {sizes[g]}" for g in sorted(small, key=lambda g: -sizes[g])) + ")"
    mapping = {g: (pooled if g in small else g) for g in sizes.index}
    members = {}
    for g, lab in mapping.items():
        members.setdefault(lab, []).append(g)
    return labels.map(mapping), members


def suppress(cells: dict, min_n: int) -> dict:
    """Primary + complementary suppression for one metric across the groups of one table.
    A cell with 0 < n < min_n is suppressed; if the suppressed cells together hold fewer than min_n
    responses, the next-smallest shown cell is suppressed too, until they hold at least min_n."""
    status = {g: ("no data" if n == 0 else "suppressed" if n < min_n else "shown") for g, n in cells.items()}

    def hidden():
        return sum(cells[g] for g, s in status.items() if s in ("suppressed", "complementary"))

    while 0 < hidden() < min_n:
        shown = [g for g, s in status.items() if s == "shown"]
        if not shown:
            break
        status[min(shown, key=lambda g: cells[g])] = "complementary"
    return status


def group_metrics():
    """(metric_id, column label, function(sub) -> (n, numeric value, display text))."""
    out = []
    for k, label in KNOWLEDGE_ITEMS.items():
        def f(sub, k=k):
            both = item_pairs(sub, k)
            n = len(both)
            if n == 0:
                return 0, None, ""
            b, a = 100 * (both[f"pre_{k}"] >= 4).mean(), 100 * (both[f"post_{k}"] >= 4).mean()
            return n, round(a - b, 1), f"{b:.0f}% → {a:.0f}% ({a - b:+.0f} pts)"
        out.append((f"agree_change_{k}", f"{label}: % agree before → after", f))
    for c, label in SAT_ITEMS.items():
        def f(sub, c=c):
            s = sub[c].dropna()
            return len(s), (round(100 * (s >= 4).mean(), 1) if len(s) else None), (f"{100 * (s >= 4).mean():.0f}% ({len(s)})" if len(s) else "")
        out.append((f"agree_{c}", f"{label}: % agree (n)", f))

    def nps(sub):
        ns = nps_summary(sub["recommend_0_10"])
        return ns["n"], (round(ns["nps_value"], 1) if ns["n"] else None), (f"{ns['nps']} ({ns['n']})" if ns["n"] else "")
    out.append(("recommend_nps", "Recommend: NPS (n)", nps))

    def lc(sub):
        s = sub["likely_call_0_10"].dropna()
        return len(s), (round(100 * (s >= 9).mean(), 1) if len(s) else None), (f"{100 * (s >= 9).mean():.0f}% ({len(s)})" if len(s) else "")
    out.append(("likely_call_9_10", "Likely to call 988: % 9-10 (n)", lc))
    return out


def group_section(d: pd.DataFrame, by: str, title: str, min_n: int, thin_n: int, filter_label: str):
    """Two display tables (self-rated change; training experience) plus long-format records for group_tables.csv."""
    labels, members = pool_small_groups(d[by], min_n)
    groups = sorted(members, key=lambda g: (g.startswith("Other ("), -int((labels == g).sum())))
    metrics = group_metrics()
    values = {}
    status = {}
    for mid, mlabel, fn in metrics:
        res = {g: fn(d[labels == g]) for g in groups}
        values[mid] = res
        status[mid] = suppress({g: r[0] for g, r in res.items()}, min_n)
    records, rows_k, rows_x = [], [], []
    for g in groups:
        sub = d[labels == g]
        n = len(sub)
        base = {title: g, "n": n, "Reliability": reliability(n, min_n, thin_n)}
        months = sub["survey_month"].dropna().sort_values()
        timing = {"Sessions": sub["session_id"].nunique(),
                  "Survey months (median)": f"{months.iloc[0]} – {months.iloc[-1]} ({months.iloc[len(months) // 2]})" if len(months) else ""}
        rk, rx = dict(base, **timing), dict(base)
        for mid, mlabel, fn in metrics:
            mn, val, disp = values[mid][g]
            st = status[mid][g]
            shown = disp if st == "shown" else "no data" if st == "no data" else \
                f"suppressed (n={mn})" if st == "suppressed" else "suppressed (complementary)"
            (rk if mid.startswith("agree_change_") else rx)[mlabel] = shown
            records.append({"table": title, "filter": filter_label, "group": g, "members": "; ".join(members[g]),
                            "group_n": n, "metric": mid, "metric_label": mlabel, "metric_n": mn,
                            "value": val if st == "shown" else None, "display": shown, "status": st})
        rows_k.append(rk)
        rows_x.append(rx)
    md = (f"**Self-rated change, item by item** ({filter_label})\n\n" + md_table(pd.DataFrame(rows_k)) + "\n\n"
          f"**Training experience** ({filter_label})\n\n" + md_table(pd.DataFrame(rows_x)) + "\n")
    return md, records


def build_report(raw: pd.DataFrame, df: pd.DataFrame, meta: dict, cfg: dict, log: pd.DataFrame, in_path: Path):
    th = cfg["thresholds"]
    min_n = th["small_group_min_n"]
    thin_n = th.get("thin_group_n", 30)
    N = len(df)
    sens_df = df[df["in_sensitivity_sample"]]
    group_records = []
    L = []
    w = L.append

    # ---------------------------------------------------------------- precomputed facts
    lc_answered = int(df["likely_call_0_10"].notna().sum())
    sat_none = int((df["sat_items_answered"] == 0).sum())
    sat_part = int(df["sat_items_answered"].between(1, 2).sum())
    amb_cells = meta["ambiguous_cells"]
    amb_total = sum(amb_cells.values())
    amb_rows = int(df["flag_ambiguous_label"].sum())
    amb_scores = {k: v for k, v in cfg["likert_ambiguous_labels"].get("sensitivity_scores", {}).items() if k in meta["ambiguous_labels"]}
    amb_names = " / ".join(f"'{l.capitalize()}'" for l in meta["ambiguous_labels"]) or "none"
    resolved_txt = (" (" + ", ".join(f"'{l.capitalize()}' is now scored {cfg['likert_map'][l]} per config" for l in meta["resolved_labels"]) + ")") if meta["resolved_labels"] else ""
    amb_scores_txt = ", ".join(f"'{k.capitalize()}' = {v}" for k, v in amb_scores.items())
    outside_list = ", ".join(f"{c} ({n})" for c, n in df.loc[df["flag_county_outside_md"], "county"].value_counts().items()) or "none"
    amb_sessions = ", ".join(sorted(df.loc[df["flag_ambiguous_label"], "session_id"].unique())) or "none"
    fives_student = int(((df["recommend_0_10"] == 5) & (df["role_group"] == "Student")).sum())
    sess_cov = df.groupby("session_id").agg(n=("response_id", "size"), lc=("session_likely_call_coverage", "first"),
                                            sat=("session_satisfaction_coverage", "first"))
    n_lc_full = int((sess_cov.lc >= 0.8).sum())
    n_lc_none = int((sess_cov.lc <= 0.2).sum())
    n_lc_mixed = len(sess_cov) - n_lc_full - n_lc_none
    n_lc_any = int(df.loc[df["likely_call_0_10"].notna(), "session_id"].nunique())
    lc_in_high = int(df.loc[df["session_likely_call_coverage"] >= 0.8, "likely_call_0_10"].notna().sum())
    nosat_sessions = sorted(sess_cov.index[(sess_cov.sat == 0)])
    sat_none_in_nosat = int(((df["sat_items_answered"] == 0) & df["session_id"].isin(nosat_sessions)).sum())
    county_raw_variants = raw[meta["column_map"]["county_raw"]].nunique()
    role_raw_variants = raw[meta["column_map"]["role_raw"]].nunique()

    def scored_alt(c):
        """Satisfaction item with ambiguous labels scored per sensitivity_scores (for the sensitivity check only)."""
        raw_c = raw[meta["column_map"][c]].map(lambda v: pd.NA if pd.isna(v) else norm(v))
        return df[c].astype("Float64").fillna(raw_c.map(lambda x: amb_scores.get(x) if pd.notna(x) else None).astype("Float64"))

    amb_shift = max((abs((scored_alt(c) >= 4).mean() - (df[c].dropna() >= 4).mean()) * 100 for c in SAT_ITEMS), default=0.0)
    rtype = {c: df[f"{c}_type"].value_counts() for c in TEXT_COLS}

    # ---------------------------------------------------------------- header
    w("# Data Quality Report: EveryMind 988 Training Survey\n")
    w(f"*Generated {datetime.now():%Y-%m-%d %H:%M} by `clean.py`. Input `{in_path.name}` (SHA-256 `{sha256(in_path)[:16]}…`).*\n")
    w("This report covers how the raw Microsoft Forms export was cleaned, what quality issues exist, and baseline figures. "
      "**The source file is not modified. No rows were deleted and no answers were filled in or invented.** Every cleaning change is listed with its rule "
      "in `change_log.csv`. Doubtful responses are *flagged for review*, never removed. Primary figures use every valid answer, and §6 repeats them "
      "without flagged responses.\n")

    # ---------------------------------------------------------------- at a glance
    w("## 1. At a glance\n")
    dates = df["start_time"]
    glance = pd.DataFrame([
        ("Responses (rows in = rows out)", f"{len(raw):,} in → {N:,} out"),
        ("Survey submission dates", f"{dates.min():%b %d, %Y} to {dates.max():%b %d, %Y}"),
        ("Training sessions (training date × county)", f"{df['session_id'].nunique()} ({int((df.groupby('session_id').size() < min_n).sum())} with fewer than {min_n} responses)"),
        ("Responses with a review flag (kept; see §5)", f"{int((~df['in_sensitivity_sample']).sum())} ({pct((~df['in_sensitivity_sample']).sum(), N)})"),
        ("Rating cells left unscored (ambiguous label)", f"{amb_total} cells in {amb_rows} responses"),
        ("Values that matched no cleaning rule", "none" if not meta["unmapped"] else
         f"**{int(df['flag_unmapped_value'].sum())} responses**: see §2a and update `cleaning_config.json`"),
    ], columns=["Item", "Value"])
    w(md_table(glance) + "\n")
    if N < min_n:
        w(f"> ⚠ **This export has only {N} responses, fewer than the small-group minimum of {min_n}.** Overall figures below describe very few people "
          "and could identify individuals. Don't share them. Group results are fully suppressed.\n")

    # ---------------------------------------------------------------- issues
    w("## 2. Issues found and what was done\n")
    rt_txt = {c: ", ".join(f"{t.replace('_', ' ')} {rtype[c].get(t, 0)}" for t in ["substantive", "nothing", "dont_know", "no_questions", "no_content"])
              for c in TEXT_COLS}
    issues = [
        ("1", "\"How likely are you to call 988?\" is mostly blank",
         f"{N - lc_answered:,} of {N:,} blank ({pct(N - lc_answered, N)}). Coverage clusters **by session** (date × county group): {n_lc_full} have high coverage (≥80% answered), {n_lc_none} low (≤20%), and {n_lc_mixed} in between (§4b). One possible explanation is that different versions of the form were used. That isn't confirmed.",
         f"Left blank, never filled in. Every figure for this question uses its {lc_answered} valid answers as the denominator. Those answers come from {n_lc_any} date × county groups, {lc_in_high} of them from the {n_lc_full} high-coverage groups, so they are **not representative** of all participants."),
        ("2", "Satisfaction items use labels that aren't on the agree/disagree scale",
         f"{amb_rows} responses use an unresolved label ({amb_names}) ({amb_total} cells: " + ", ".join(f"{c} {n}" for c, n in amb_cells.items() if n) + f"). Sessions: {amb_sessions}. "
         "The wording \"Neither agree **or** disagree\" (satisfaction) also differs from \"**nor**\" (knowledge).",
         f"\"Neither agree or disagree\" → neutral (3). Cells with an unresolved label ({amb_names}) are **left unscored** until EveryMind confirms their meaning{resolved_txt}; "
         f"the respondent's other answers are kept. Scoring them instead as {amb_scores_txt} changes any item's % agree by at most {amb_shift:.1f} points (§6)."),
        ("3", "Satisfaction block not answered",
         f"{sat_none} responses answered none of the 3 satisfaction items and {sat_part} answered only some. {sat_none_in_nosat} of the {sat_none} come from sessions where nobody answered the block ({', '.join(nosat_sessions) or 'none'}). A different form version is one possible explanation.",
         "Left blank. Each item is reported with its own valid n."),
        ("4", "County typed free-form",
         f"{county_raw_variants} distinct spellings (e.g. 'Montgomery\\xa0', 'Montgomery County ', 'Fairfax county ').",
         f"Trimmed, dropped the word 'County', mapped to {df['county'].nunique()} counties (every mapping is in Appendix B). County is the county the **training was affiliated with**, not where the respondent lives. "
         f"{int(df['flag_county_outside_md'].sum())} responses come from trainings affiliated with counties outside Maryland ({outside_list}), flagged `flag_county_outside_md`."),
        ("5", "Role is multi-select with stray semicolons",
         f"{role_raw_variants} distinct raw strings (e.g. 'School staff member;' vs 'School staff member').",
         f"Split into `is_student`, `is_school_staff`, `is_caregiver_parent`, `is_other_community`, so all selections are kept. `role_group` = the single role, or 'Multiple roles' ({int((df['role_count'] > 1).sum())} responses)."),
        ("6", "Possible scale reversal",
         f"{int(df['pattern_all_post_strongly_disagree'].sum())} responses rate all three after-training knowledge items 'Strongly disagree'. {int(df['flag_possible_scale_reversal'].sum())} of them also gave a recommend score of 7 or higher, or a satisfaction mean of 4 or higher. That combination may mean the person read the scale backwards.",
         "Flagged `flag_possible_scale_reversal` for review. **Kept** in primary figures and **not** recoded, because intent can't be confirmed. §6 shows results without them."),
        ("7", "Repeated IDs and possible duplicate submissions",
         f"{int(df['flag_repeated_id'].sum())} rows share a response ID. {int(df['flag_possible_duplicate'].sum())} responses match an earlier response exactly (same session, every answer and comment identical, within {th['duplicate_window_minutes']} min).",
         "Flagged (`flag_repeated_id`, `flag_possible_duplicate`; `duplicate_of_id` points to the first) for review. **Kept**, because identical answers can come from different people."),
        ("8", "Very short or very long completion times",
         f"{int(df['flag_fast_completion'].sum())} responses took under {th['fast_completion_seconds']} s and {int(df['flag_long_completion'].sum())} took over {th['long_completion_seconds'] // 60} min (median {df['duration_sec'].median():.0f} s). For comparison, {meta['fast_counts'][25]} took under 25 s and {meta['fast_counts'][30]} under 30 s.",
         "Flagged `flag_fast_completion` / `flag_long_completion` for review. **Kept** in primary figures; §6 shows results without them. Thresholds are in config."),
        ("9", "Straight-lining",
         f"{int(df['flag_straightline_all9'].sum())} responses gave the identical answer to all 9 agree/disagree items.",
         "Flagged `flag_straightline_all9` as descriptive only. Answering 'Strongly agree' to everything, before and after, is plausible for someone who already knew the material."),
        ("10", "Training date issues",
         f"{int(df['flag_training_date_missing'].sum())} missing, {int(df['flag_survey_before_training'].sum())} where the training date is after the survey was submitted, {int(df['flag_survey_late'].sum())} submitted more than {th['late_survey_days']} days after the training (median gap {df['days_survey_after_training'].median():.0f} days).",
         "Flagged. Not corrected, because the true date can't be known. Training date and submission date are kept as separate fields."),
        ("11", "Comments: response types, other languages, and personal info",
         f"\"Most valuable\": {int(df['text_most_valuable'].notna().sum())} answered ({rt_txt['text_most_valuable']}). "
         f"\"Improve\": {int(df['text_improve'].notna().sum())} answered ({rt_txt['text_improve']}). "
         f"{int((df['language'] == 'Spanish').sum())} responses used the Spanish form. {int(df['flag_text_redacted'].sum())} responses had something redacted (a listed name, 'Mr./Ms./Dr. + name', an email, or a phone number not on the allowlist). Unlisted names aren't detected.",
         "Whitespace tidied only, wording untouched. The exact export text is kept in `*_original`. Response types are in `*_type` (blank / nothing / dont_know / no_questions / no_content / substantive). "
         "`*_redacted` copies replace listed names (any capitalization), 'Mr./Ms./Dr. + name', emails and phone numbers. "
         "**This is a preliminary, automated pass. It can miss names that aren't listed, so read every excerpt before quoting or sharing it.**"),
        ("12", "Invisible characters",
         "Language labels end in a hidden left-to-right mark (U+200E). Several labels contain non-breaking spaces.",
         "Removed during normalization (logged as `formatting` in the change log)."),
    ]
    w(md_table(pd.DataFrame(issues, columns=["#", "Issue", "What we found", "Action"])) + "\n")

    if meta["unmapped"]:
        w("### 2a. ⚠ Values that matched no cleaning rule\n")
        w("These raw values aren't covered by `cleaning_config.json`. They were **not forced into an existing category**. Ratings, roles and language are left blank. "
          "County keeps the typed text with an `UNRECOGNIZED:` prefix so it stays visible. Rows are flagged `flag_unmapped_value` (columns listed in `unmapped_fields`). "
          "Add the values to the config and re-run.\n")
        w(md_table(pd.DataFrame(meta["unmapped"])) + "\n")

    # ---------------------------------------------------------------- completeness
    w("## 3. Completeness by question\n")
    labels = {"training_date": "Training date", "role_group": "Role", "county": "County (training affiliation)", "language": "Form language",
              **{f"post_{k}": f"After: {v}" for k, v in KNOWLEDGE_ITEMS.items()},
              **{f"pre_{k}": f"Before (recalled): {v}" for k, v in KNOWLEDGE_ITEMS.items()},
              **SAT_ITEMS, "recommend_0_10": "Likely to recommend (0-10)", "likely_call_0_10": "Likely to call 988 (0-10)",
              "text_most_valuable": "Most valuable thing learned (text)", "text_improve": "How to improve (text)"}
    comp = pd.DataFrame([{"Question": lab, "Valid answers": int(df[c].notna().sum()), "Missing": int(df[c].isna().sum()),
                          "of which unscored ambiguous label": amb_cells.get(c, 0) if c in amb_cells else "",
                          "% valid": pct(df[c].notna().sum(), N)} for c, lab in labels.items()])
    w(md_table(comp) + "\n")

    # ---------------------------------------------------------------- composition
    w("## 4. Who is in the data\n")
    w(f"Counts are shown in full. Outcome figures use the small-group rules in §7.\n")
    for col, title in [("role_group", "Role"), ("county", "County the training was affiliated with"), ("language", "Form language")]:
        vc = df[col].fillna("(blank)").value_counts()
        t = pd.DataFrame({title: vc.index, "n": vc.values, "%": [pct(v, N) for v in vc.values]})
        w(f"**{title}**\n\n" + md_table(t) + "\n")
    w("*Form language* is the language version of the form the respondent used. It is not necessarily their native language or the language the training was delivered in, and some comments may be in a different language.\n")
    anyrole = pd.DataFrame([{"Role": cfg["role_display"][k], "n selecting": int(df[f"is_{k}"].sum())} for k in cfg["role_map"].values()])
    w(f"**Any-role counts.** People can select more than one role, so these add up to {int(anyrole['n selecting'].sum()):,}, which is more than the {N:,} respondents.\n")
    w(md_table(anyrole) + "\n")
    w("**Role × county**. Role and county overlap heavily, so county differences may really be role differences.\n")
    ct = pd.crosstab(df["county"], df["role_group"], margins=True, margins_name="Total")
    w(md_table(ct, index=True) + "\n")
    w("**Responses and question coverage by survey month**\n")
    bym = df.groupby("survey_month").agg(
        responses=("response_id", "size"), sessions=("session_id", "nunique"),
        likely_call_answered=("likely_call_0_10", lambda s: int(s.notna().sum())),
        satisfaction_all3=("sat_items_answered", lambda s: int((s == 3).sum())),
    )
    bym["likely_call % answered"] = [pct(a, b) for a, b in zip(bym.likely_call_answered, bym.responses)]
    w(md_table(bym, index=True) + "\n")
    sess = df.groupby("session_id").size()
    w(f"**Sessions**: {len(sess)} sessions. Responses per session: median {sess.median():.0f}, range {sess.min()}–{sess.max()}. "
      f"{int((sess < min_n).sum())} sessions have fewer than {min_n} responses. A session is approximated as training date × county, "
      "and the dataset has no session or presenter ID.\n")
    w("### 4b. Response coverage of the optional questions, by session\n")
    w("Blank answers cluster by session (date × county group) rather than by person. Sessions are grouped by the share of their responses "
      "that answered each block (≥80% = high coverage, ≤20% = low coverage). **One possible explanation is that different versions of the form were used, "
      "but the data can't confirm this.** EveryMind would need to.\n")

    def band(x):
        return "high coverage (≥80%)" if x >= 0.8 else "low coverage (≤20%)" if x <= 0.2 else "mixed"

    sv = sess_cov.assign(likely_call=sess_cov.lc.map(band), satisfaction=sess_cov.sat.map(band))
    ver = sv.groupby(["satisfaction", "likely_call"]).agg(sessions=("n", "size"), responses=("n", "sum")).reset_index()
    ver.columns = ["Satisfaction block", "Likely-to-call question", "Sessions", "Responses"]
    w(md_table(ver) + "\n")
    lc_sess = sv[sv.likely_call != "low coverage (≤20%)"].sort_values("n", ascending=False)
    w(f"**Where the {lc_answered} 'likely to call 988' answers come from.** They span {n_lc_any} date × county groups. "
      f"{lc_in_high} come from the {n_lc_full} high-coverage groups. Groups with more than 20% coverage:\n")
    t = pd.DataFrame({"Session": lc_sess.index, "Responses": lc_sess.n.values,
                      "% answered likely-to-call": [pct(round(x * n), n) for x, n in zip(lc_sess.lc, lc_sess.n)]})
    w(md_table(t) + "\n")
    w(f"Answers in low-coverage sessions ({int(df.loc[df['session_likely_call_coverage'] <= 0.2, 'likely_call_0_10'].notna().sum())} responses) "
      "could be people who entered a different training date, or a question that was optional in those sessions. The data can't tell which.\n")

    # ---------------------------------------------------------------- flags
    w("## 5. Flags\n")
    w("**Review flags** mark responses a person should look at. They are **not** excluded from primary figures; §6 repeats the key figures without them. "
      "**Descriptive flags** just describe a pattern.\n")
    flag_defs = [
        ("flag_possible_scale_reversal", "All 3 after-training knowledge items = Strongly disagree, AND recommend ≥7 or satisfaction mean ≥4", "Review"),
        ("flag_possible_duplicate", f"Identical answers and comments to an earlier response in the same session within {th['duplicate_window_minutes']} min", "Review"),
        ("flag_repeated_id", "Response ID appears more than once", "Review"),
        ("flag_fast_completion", f"Completed in under {th['fast_completion_seconds']} seconds", "Review"),
        ("flag_long_completion", f"Took over {th['long_completion_seconds'] // 60} minutes", "Review"),
        ("flag_ambiguous_label", f"At least one rating used an unresolved label ({amb_names}); those cells are unscored", "Descriptive"),
        ("flag_unmapped_value", "A raw value matched no cleaning rule (see `unmapped_fields`)", "Descriptive"),
        ("flag_straightline_all9", "Same answer to all 9 agree/disagree items", "Descriptive"),
        ("flag_county_outside_md", "Training affiliated with a known non-Maryland county", "Descriptive"),
        ("flag_training_date_missing", "No training date entered", "Descriptive"),
        ("flag_survey_before_training", "Training date is after the submission date", "Descriptive"),
        ("flag_survey_late", f"Submitted more than {th['late_survey_days']} days after the training", "Descriptive"),
        ("flag_text_redacted", "Automated redaction changed the comment (listed name, Mr./Ms./Dr. + name, email, or non-allowlisted phone). Unlisted names aren't detected", "Descriptive"),
        ("pattern_any_decline", "At least one knowledge item rated lower after than before", "Descriptive"),
        ("pattern_no_change", "All 3 knowledge items identical before and after", "Descriptive"),
    ]
    ft = pd.DataFrame([{"Flag": f"`{f}`", "Type": x, "Definition": d, "Responses": int(df[f].sum()), "%": pct(df[f].sum(), N)}
                       for f, d, x in flag_defs])
    w(md_table(ft) + "\n")
    w(f"Responses with at least one review flag: **{int((~df['in_sensitivity_sample']).sum())}** "
      f"(some have more than one, so this is less than the sum, {sum(int(df[f].sum()) for f in REVIEW_FLAGS)}).\n")

    # ---------------------------------------------------------------- baseline
    w("## 6. Baseline figures\n")
    w("> **Read before using these numbers.** These are **retrospective, self-reported** ratings. The \"before\" ratings were collected *after* the training, "
      "as recall (\"think back to before…\"), which tends to make change look bigger than it is (response-shift and social-desirability bias). "
      "There is no comparison group, no test of knowledge, and no record of actual 988 calls. These figures show that participants *rated themselves* more confident after the training. "
      "They are not objective learning gains, causal effects, hotline use, or mental-health outcomes. Before/after comparisons use only respondents who answered both. "
      "95% CIs are normal-approximation intervals.\n")
    w(f"### Self-rated knowledge and confidence, item by item (all valid responses, n = {N:,})\n")
    w(md_table(knowledge_table(df)) + "\n")
    w("The three items are kept separate on purpose. They move by very different amounts, and a combined score would hide that.\n")
    w("### Training experience\n")
    rows = []
    for c, lab in SAT_ITEMS.items():
        s = df[c].dropna()
        rows.append({"Measure": f"{lab}: % agree (4-5)", "Value": pct((s >= 4).sum(), len(s)), "Valid n": len(s), "Missing": N - len(s),
                     "Note": f"{amb_cells.get(c, 0)} ambiguous cells unscored"})
    ns = nps_summary(df["recommend_0_10"])
    rows.append({"Measure": "Likely to recommend: NPS", "Value": ns["nps"], "Valid n": ns["n"], "Missing": N - ns["n"],
                 "Note": f"Promoters {ns['promoters']} / Passives {ns['passives']} / Detractors {ns['detractors']}"})
    lc = df["likely_call_0_10"].dropna()
    rows.append({"Measure": "Likely to call 988: % scoring 9-10", "Value": pct((lc >= 9).sum(), len(lc)), "Valid n": len(lc), "Missing": N - len(lc),
                 "Note": f"Mean {lc.astype(float).mean():.2f}. Only asked in some sessions (§4b). Not representative."})
    w(md_table(pd.DataFrame(rows)) + "\n")
    rc = df["recommend_0_10"].value_counts().sort_index()
    w("**Recommend score distribution:** " + ", ".join(f"{int(k)}: {v}" for k, v in rc.items()) +
      f". There is a spike at exactly 5 ({rc.get(5, 0)} responses vs {rc.get(4, 0)} at 4 and {rc.get(6, 0)} at 6). "
      f"{pct(fives_student, rc.get(5, 0))} of the 5s come from students, who are {pct((df['role_group'] == 'Student').sum(), N)} of respondents. "
      "One possible reading is that some students used the midpoint as 'no opinion'. The data can't confirm this, so the NPS is reported as is.\n")

    w("### Sensitivity check 1: without review-flagged responses\n")
    w(f"Same figures, leaving out the {int((~df['in_sensitivity_sample']).sum())} responses with a review flag (n = {len(sens_df):,}). "
      "If a conclusion changes here, it depends on the flagged responses.\n")
    srows = []
    for k, label in KNOWLEDGE_ITEMS.items():
        for name, d in [("All valid", df), ("Without flagged", sens_df)]:
            both = item_pairs(d, k)
            b, a = 100 * (both[f"pre_{k}"] >= 4).mean(), 100 * (both[f"post_{k}"] >= 4).mean()
            srows.append({"Item": label, "Sample": name, "Valid pairs": len(both), "Change in % agree": f"{a - b:+.1f} pts",
                          "Mean self-rated change [95% CI]": fmt_ci(both[f"post_{k}"] - both[f"pre_{k}"])})
    for name, d in [("All valid", df), ("Without flagged", sens_df)]:
        ns2 = nps_summary(d["recommend_0_10"])
        srows.append({"Item": "Likely to recommend: NPS", "Sample": name, "Valid pairs": ns2["n"], "Change in % agree": "", "Mean self-rated change [95% CI]": ns2["nps"]})
    w(md_table(pd.DataFrame(srows)) + "\n")
    w("### Sensitivity check 2: scoring the ambiguous labels\n")
    w(f"Primary figures leave unresolved labels ({amb_names}) unscored. This shows what changes if they are scored as {amb_scores_txt or '(no unresolved labels)'}.\n")
    arows = []
    for c, lab in SAT_ITEMS.items():
        s, alt = df[c].dropna(), scored_alt(c).dropna()
        arows.append({"Item": lab, "% agree, unscored (primary)": f"{pct((s >= 4).sum(), len(s))} (n={len(s)})",
                      "% agree, scored": f"{pct((alt >= 4).sum(), len(alt))} (n={len(alt)})",
                      "Mean, unscored": f"{s.astype(float).mean():.3f}", "Mean, scored": f"{alt.astype(float).mean():.3f}"})
    w(md_table(pd.DataFrame(arows)) + "\n")

    # ---------------------------------------------------------------- groups
    w("## 7. Differences across groups (exploratory)\n")
    w(f"**Small-group rules (set before analysis):** groups with fewer than {min_n} responses are pooled into an *Other (…)* group. "
      f"If that pool is still under {min_n}, the next-smallest group joins it. Within each measure, any cell with fewer than {min_n} valid answers is suppressed, "
      f"and if suppressed cells together hold fewer than {min_n} answers, the next-smallest cell is also suppressed (*complementary*). That way no hidden result can be worked out by subtraction. "
      f"Groups of {min_n}–{thin_n - 1} are labeled *thin* and are descriptive only. These are project guardrails, not a guarantee that larger groups are representative.\n")
    w("**How to read these tables.** Groups are ordered by size, not by score. This is **not a ranking**. Differences are exploratory and not adjusted for other factors. "
      "Before attributing a gap to training quality, check (a) the *before* % agree, because groups that start high have less room to rise, "
      "(b) the survey months and number of sessions, because question coverage differs across sessions and months (§4b), and (c) composition, e.g. role mix by county (§4). "
      "*Likely to call 988* comes only from the sessions in §4b, so don't compare groups on it.\n")
    for col, title, d, flt in [("role_group", "Role", df, "all valid responses"),
                               ("county", "County (training affiliation)", df, "all valid responses"),
                               ("language", "Form language", df, "all valid responses"),
                               ("county", "County, students only", df[df["role_group"] == "Student"], "students only")]:
        w(f"### By {title.lower()}\n")
        if title == "County, students only":
            aa = df["county"] == "Anne Arundel"
            w(f"Anne Arundel's respondents are {pct((df.loc[aa, 'role_group'] == 'Student').sum(), aa.sum())} students, while other counties have far more adults. "
              "Comparing students with students takes role mix out of the county comparison. Other differences, such as school, grade and session, remain.\n")
        md, recs = group_section(d, col, title, min_n, thin_n, flt)
        group_records += recs
        w(md)
    w("*% agree before → after*: share answering Agree or Strongly agree, among respondents who answered both, with the change in percentage points. "
      "*NPS* = % promoters (9–10) − % detractors (0–6).\n")

    # ---------------------------------------------------------------- limitations
    w("## 8. Limitations and open questions for EveryMind\n")
    for item in [
        "**Retrospective \"before\" ratings.** Change scores are likely inflated relative to a true pre-test. A real pre-survey would fix this, even for a sample of sessions.",
        "**Self-report only.** No measure of tested knowledge, actual 988 use, or mental-health outcomes.",
        f"**\"Likely to call 988\" coverage.** Answered by only {pct(lc_answered, N)} of respondents, across {n_lc_any} date × county groups (mostly the {n_lc_full} high-coverage ones).",
        f"**Ambiguous labels.** {amb_total} satisfaction cells are unscored until their meaning is confirmed. The sensitivity check shows the effect is small.",
        "**No session or presenter ID.** Sessions are approximated by training date × county. A self-entered training date can be wrong, and several groups can share a date.",
        "**County and language.** County is the training's affiliation, not residence. Form language isn't necessarily native or training language.",
        "**Unbalanced groups.** Most responses are students in Anne Arundel or Prince George's. Smaller counties and the Spanish form allow only descriptive comparisons.",
        "**Selection.** Attendance per session is unknown, so the response rate and non-response bias can't be estimated.",
        "**Judgment-call flags.** Scale-reversal, duplicate and duration flags are heuristics. Thresholds are in `cleaning_config.json`, and nothing is excluded by default.",
        "**Suppression scope.** Pooling and complementary suppression are applied and checked within each table. A determined reader comparing figures *across* different tables (e.g. role vs. county) might still narrow down a small group's result. Keep that in mind before publishing new cross-tabulations.",
        f"**Questions for EveryMind:** (a) Did the form change during the year (likely-to-call and satisfaction blocks)? (b) What did {amb_names} mean on the forms used in {amb_sessions}? (c) Should trainings affiliated with {outside_list} be in scope? (d) Is 410-768-5522 the crisis line taught in training?",
    ]:
        w(f"- {item}")
    w("")

    # ---------------------------------------------------------------- appendix
    w("## Appendix A: Column mapping\n")
    w(md_table(pd.DataFrame([{"Cleaned name": k, "Raw header": v.replace("|", "/").replace("\xa0", " ")} for k, v in meta["column_map"].items()])) + "\n")
    if meta["extra_columns"]:
        w(f"Unmapped raw columns (ignored): {meta['extra_columns']}\n")
    w("## Appendix B: Corrections and formatting fixes (from change_log.csv)\n")
    w("`kind` = **correction** (a variant label, stray separator or ambiguous answer was handled) or **formatting** "
      "(only invisible characters or whitespace removed). Routine recodes (e.g. 'Agree' → 4) and unchanged values are in `change_log.csv`.\n")
    ch = log[log["kind"].isin(["correction", "formatting"])][["column", "kind", "raw_value", "cleaned_value", "count", "rule"]]
    w(md_table(ch) + "\n")
    rec = log[log["kind"].str.startswith("recode")].groupby("column")["count"].sum()
    w("Routine recodes per column (label → score/category): " + ", ".join(f"{c} {n:,}" for c, n in rec.items()) + ". "
      "Those with `kind` = `recode + formatting` also had an invisible character removed.\n")
    w(f"Text columns: whitespace tidied in {meta['text_most_valuable_whitespace_changed']} 'most valuable' and "
      f"{meta['text_improve_whitespace_changed']} 'improve' comments. No wording was changed.\n")
    w("## Appendix C: Comment response types\n")
    w("Counted per respondent. Themes (praise, suggestions, questions, …) belong to the analysis stage, and their percentages should use the *substantive* count as the denominator.\n")
    rt = pd.DataFrame({c: df[f"{c}_type"].value_counts() for c in TEXT_COLS}).fillna(0).astype(int)
    rt = rt.reindex([t for t in ["substantive", "nothing", "dont_know", "no_questions", "no_content", "blank"] if t in rt.index])
    rt.loc["Total"] = rt.sum()
    rt.index.name = "Response type"
    w(md_table(rt, index=True) + "\n")
    return "\n".join(L), pd.DataFrame(group_records)


# --------------------------------------------------------------------------- data dictionary
def data_dictionary(df: pd.DataFrame) -> pd.DataFrame:
    desc = {
        "response_id": "Forms response ID (unchanged)", "start_time": "Survey start timestamp", "end_time": "Survey completion timestamp",
        "duration_sec": "end_time − start_time in seconds", "survey_date": "Date survey started", "survey_month": "YYYY-MM of survey start",
        "training_date": "Training date as entered by respondent", "days_survey_after_training": "survey_date − training_date (days)",
        "language": "Language version of the form used (not necessarily native or training language)", "role_group": "Single role, or 'Multiple roles'", "roles_selected": "All roles selected",
        "role_count": "Number of roles selected", "county": "Standardized county the training was affiliated with (not residence)", "county_note": "Extra detail typed in county field",
        "recommend_0_10": "Likelihood to recommend (0-10)", "recommend_category": "NPS category from recommend_0_10",
        "likely_call_0_10": "Likelihood to call 988 (0-10); mostly blank, see report",
        "sat_items_answered": "Satisfaction items with any answer, incl. ambiguous labels (0-3)", "sat_items_scored": "Satisfaction items with a 1-5 score (0-3)", "satisfaction_mean": "Mean of 3 satisfaction items (only if all 3 scored); used only for the scale-reversal flag",
        "session_id": "Approximate session: training date / county", "session_n": "Responses in this session_id",
        "duplicate_of_id": "response_id of the earlier identical response, if flagged",
        "recommend_0_10_original": "Recommend answer exactly as exported", "likely_call_0_10_original": "Likely-to-call answer exactly as exported",
        "session_likely_call_coverage": "Share of this session's responses that answered the likely-to-call question (0-1)",
        "session_satisfaction_coverage": "Share of this session's responses that answered any satisfaction item (0-1)", "review_flag_count": "Number of review flags set", "in_sensitivity_sample": "True if no review flag is set (used for the sensitivity check only)",
        "unmapped_fields": "Columns whose raw value matched no cleaning rule", "answered_blocks": "Optional question blocks this response answered",
    }
    for k, v in KNOWLEDGE_ITEMS.items():
        desc[f"post_{k}"] = f"After training: {v} (1=Strongly disagree … 5=Strongly agree)"
        desc[f"pre_{k}"] = f"Recalled before training: {v} (1-5)"
        desc[f"change_{k}"] = f"post_{k} − pre_{k} (retrospective self-rated change)"
    for k, v in SAT_ITEMS.items():
        desc[k] = f"{v} (1-5)"
    for c in TEXT_COLS:
        desc[c] = "Comment, whitespace tidied, wording unchanged. INTERNAL USE (may contain names)"
        desc[f"{c}_redacted"] = "Comment with listed names, Mr./Ms./Dr. + name, emails and phone numbers replaced. PRELIMINARY: automated redaction can miss names, so review manually before quoting or sharing"
        desc[f"{c}_original"] = "Comment exactly as exported"
        desc[f"{c}_type"] = "blank / nothing / dont_know / no_questions / no_content / substantive"
    rows = []
    for c in df.columns:
        d = desc.get(c)
        if d is None and c.startswith("is_"):
            d = f"Selected role: {c[3:]}"
        if d is None and c.startswith(("flag_", "pattern_")):
            d = "See data_quality_report.md §5"
        rows.append({"column": c, "description": d or "", "non_blank": int(df[c].notna().sum()), "dtype": str(df[c].dtype)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", default=str(DEFAULT_INPUT))
    ap.add_argument("--config", default=str(HERE / "cleaning_config.json"))
    ap.add_argument("--out", default=str(HERE / "output"))
    a = ap.parse_args(argv)

    in_path, out = Path(a.input), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = strip_comments(json.loads(Path(a.config).read_text()))

    raw = load_raw(in_path)
    log = ChangeLog()
    df, meta = clean(raw, cfg, log)
    assert len(df) == len(raw), "row count changed"
    logf = log.frame()

    df.to_csv(out / "clean_responses.csv", index=False)
    logf.to_csv(out / "change_log.csv", index=False)
    report, groups = build_report(raw, df, meta, cfg, logf, in_path)
    (out / "data_quality_report.md").write_text(report)
    groups.to_csv(out / "group_tables.csv", index=False)

    flags = [c for c in df.columns if c.startswith(("flag_", "pattern_"))]
    flag_summary = pd.DataFrame({"flag": flags, "responses": [int(df[f].sum()) for f in flags]})
    xl = df.copy()
    for c in xl.select_dtypes(include=["datetimetz"]).columns:
        xl[c] = xl[c].dt.tz_localize(None)
    with pd.ExcelWriter(out / "clean_responses.xlsx") as w:
        xl.to_excel(w, sheet_name="clean_responses", index=False)
        data_dictionary(df).to_excel(w, sheet_name="data_dictionary", index=False)
        logf.to_excel(w, sheet_name="change_log", index=False)
        flag_summary.to_excel(w, sheet_name="flag_summary", index=False)
        groups.to_excel(w, sheet_name="group_tables", index=False)
        raw.to_excel(w, sheet_name="raw_original", index=False)

    if meta["unmapped"]:
        print(f"WARNING: {int(df['flag_unmapped_value'].sum())} responses contain values no rule in cleaning_config.json covers "
              "(left blank, flagged flag_unmapped_value):")
        for u in meta["unmapped"]:
            print(f"    {u['column']}: {u['raw_value']} x{u['rows']}")
    print(f"Cleaned {len(df):,} responses -> {out}/")
    print(f"  responses with a review flag (kept): {int((~df['in_sensitivity_sample']).sum())}  |  ambiguous rating cells unscored: {sum(meta['ambiguous_cells'].values())}")
    for f in ["clean_responses.csv", "clean_responses.xlsx", "change_log.csv", "group_tables.csv", "data_quality_report.md"]:
        print(f"  {out / f}")


if __name__ == "__main__":
    main()
