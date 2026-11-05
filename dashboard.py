#!/usr/bin/env python3
"""One-page visual report of the cleaned survey and comment themes.

Usage
-----
    python3 dashboard.py                    # after clean.py, validate_clean.py and themes.py
    python3 dashboard.py --out output --config cleaning_config.json

Writes --out/dashboard.html: a single self-contained file (fonts embedded, no internet, no libraries) to open
in any browser. Every headline and sentence is written from the numbers in this run, so a new export produces
an up-to-date page with no editing.

It shows only aggregate figures. No comment text, IDs or row-level data go into the page. Group figures come
from group_tables.csv and theme_summary.csv as published, so their small-cell suppression carries over; the two
figures computed here (whole-sample results, students vs everyone else) apply the same minimum group size.

It refuses to run unless every file it reads matches run_manifest.json / themes_manifest.json, and the themes
were built from this exact cleaned bundle, so the page always describes one complete, checked run.
"""
from __future__ import annotations

import argparse
import interventions
import base64
import html
import itertools
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from clean import pool_small_groups
from themes import sha256, strip_comments

HERE = Path(__file__).resolve().parent
FONTS = HERE / "assets" / "fonts"
ITEMS = [("identify", "Identify mental health challenges and crises"),
         ("describe", "Describe 988 and how it works"),
         ("when", "Know when to use 988")]
ROLE_ORDER = ["Student", "School staff", "Caregiver/parent", "Other/community member", "Multiple roles"]
INTENTS = [("question", "Asked a question", "c-gold"), ("suggestion", "Suggested a change", "c-blue"),
           ("no_change", "No change needed", "c-sand"), ("unclear", "Unclear", "c-pale")]
STUDENTS, OTHERS = "Students", "All other respondents"
SKIP_THEMES = {"mv_possible_misconception", "mv_unspecific"}   # a staff-review list, and comments with no theme
LIT = dict(keep_default_na=False, na_values=[""])
HISTORY, HISTORY_KEEP = "dashboard_history.json", 20   # aggregate figures of past runs, for 'since the last run'
e = html.escape


# ---------------------------------------------------------------- loading


def load(out: Path) -> dict:
    """Read the bundle, refusing anything that doesn't match its manifests."""
    themes = out / "themes"
    need = [out / "run_manifest.json", out / "clean_responses.csv", out / "group_tables.csv",
            themes / "themes_manifest.json", themes / "theme_summary.csv"]
    missing = [str(p) for p in need if not p.exists()]
    if missing:
        sys.exit(f"ERROR: missing {', '.join(missing)}. Run clean.py, validate_clean.py and themes.py first (or run_all.py).")
    man = json.loads((out / "run_manifest.json").read_text())
    tman = json.loads((themes / "themes_manifest.json").read_text())
    for f in ["clean_responses.csv", "group_tables.csv"]:
        if man.get("files", {}).get(f) != sha256(out / f):
            sys.exit(f"ERROR: {f} doesn't match run_manifest.json. Re-run clean.py and validate_clean.py.")
    if (tman.get("clean_responses_sha256") != sha256(out / "clean_responses.csv")
            or tman.get("run_manifest_sha256") != sha256(out / "run_manifest.json")):
        sys.exit("ERROR: the themes were built from a different cleaned bundle. Re-run themes.py.")
    if tman.get("files", {}).get("theme_summary.csv", {}).get("sha256") != sha256(themes / "theme_summary.csv"):
        sys.exit("ERROR: theme_summary.csv doesn't match themes_manifest.json. Re-run themes.py.")
    return {"manifest": man, "themes_manifest": tman,
            "clean": pd.read_csv(out / "clean_responses.csv", dtype={"county": str, "role_group": str}, **LIT),
            "groups": pd.read_csv(out / "group_tables.csv", dtype=str, **LIT),
            "themes": pd.read_csv(themes / "theme_summary.csv", dtype=str, **LIT)}


# ---------------------------------------------------------------- figures


def num(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


NAN = float("nan")
Z = 1.96   # 95% margins of error


def nps(scores: pd.Series) -> float:
    return ((scores >= 9).mean() - (scores <= 6).mean()) * 100 if len(scores) else NAN


def nps_moe(scores: pd.Series) -> float:
    """95% margin of error of an NPS: each answer counts +1 (9–10), 0 (7–8) or −1 (0–6).

    Adjusted-Wald: ¾ of a promoter and ¾ of a detractor are added (n + 3), so a small session where everyone
    gave 10 gets a sensible margin instead of ±0.
    """
    n = len(scores)
    if n < 2:
        return NAN
    pp, pd_ = ((scores >= 9).sum() + 0.75) / (n + 3), ((scores <= 6).sum() + 0.75) / (n + 3)
    return Z * ((pp + pd_ - (pp - pd_) ** 2) / (n + 3)) ** 0.5 * 100


def item_stats(d: pd.DataFrame, key: str) -> dict:
    """% agree (4–5) before and after on valid pairs; the margin on the gain uses each person's own change."""
    pairs = d[[f"pre_{key}", f"post_{key}"]].dropna()
    n = len(pairs)
    if not n:
        return {"n": 0, "before": NAN, "after": NAN, "gain": NAN, "moe": NAN}
    b, a = (pairs[f"pre_{key}"] >= 4).astype(int), (pairs[f"post_{key}"] >= 4).astype(int)
    return {"n": n, "before": b.mean() * 100, "after": a.mean() * 100, "gain": (a.mean() - b.mean()) * 100,
            "moe": Z * (a - b).std() / n ** 0.5 * 100 if n >= 2 else NAN}


def figures(d: pd.DataFrame) -> dict:
    """The core figures for any set of rows (whole sample, without flagged rows, a filtered view)."""
    rec = d["recommend_0_10"].dropna()
    return {"n": len(d), "items": [{"key": k, "label": lab, **item_stats(d, k)} for k, lab in ITEMS],
            "nps": nps(rec), "nps_moe": nps_moe(rec), "nps_n": len(rec)}


def is_kept(d: pd.DataFrame) -> pd.Series:
    return d["in_sensitivity_sample"].astype(str).str.lower().eq("true")


def headline(d: pd.DataFrame, min_n: int) -> dict:
    """Whole-sample figures, computed the same way as §6 of data_quality_report.md."""
    student = d["role_group"].eq("Student")
    s_rec, o_rec = d.loc[student, "recommend_0_10"].dropna(), d.loc[~student, "recommend_0_10"].dropna()
    fives = d[d["recommend_0_10"].eq(5)]
    dates = pd.to_datetime(d["survey_date"], errors="coerce").dropna()
    months = d["survey_month"].replace("", pd.NA).dropna().value_counts().sort_index()
    return {**figures(d), "sessions": int(d["session_id"].replace("", pd.NA).nunique()),
            "first": dates.min(), "last": dates.max(), "months": months,
            # students vs everyone else is shown only when both groups reach the minimum group size
            "nps_split": ({"s": nps(s_rec), "sn": len(s_rec), "sm": nps_moe(s_rec),
                           "o": nps(o_rec), "on": len(o_rec), "om": nps_moe(o_rec)}
                          if min(len(s_rec), len(o_rec)) >= min_n else None),
            "fives": len(fives), "fives_student_pct": fives["role_group"].eq("Student").mean() * 100 if len(fives) else 0,
            "student_pct": student.mean() * 100,
            "flagged": int((~is_kept(d)).sum()),
            "without_flagged": figures(d[is_kept(d)]),
            "ambiguous_rows": int(d["flag_ambiguous_label"].astype(str).str.lower().eq("true").sum()),
            "likely_n": int(d["likely_call_0_10"].notna().sum())}


def role_moe(d: pd.DataFrame, group: str, metric: str, n) -> float:
    """Margin of error for one role cell of group_tables.csv, only when the rows here reproduce its n exactly."""
    sub = d[d["role_group"] == group]
    n = num(n)
    if metric == "recommend_nps":
        s = sub["recommend_0_10"].dropna()
        return nps_moe(s) if len(s) == n else NAN
    st = item_stats(sub, metric.replace("agree_change_", ""))
    return st["moe"] if st["n"] == n else NAN


def session_label(sid: str) -> str:
    date, _, county = sid.partition(" / ")
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        date = f"{dt.day} {dt:%b %Y}"
    except ValueError:
        pass
    return f"{date} · {county}" if county else date


def sessions(d: pd.DataFrame, min_n: int, split) -> dict:
    """One row per training session (training date × county) with at least min_n responses and ratings.

    Smaller sessions are pooled. If the pool would hold 1–(min_n−1) responses, the smallest shown session joins it,
    to reduce simple subtraction disclosure; cross-table combinations can still narrow groups.
    Descriptive benchmark NPS = the session's student share × the students' NPS + the rest × everyone else's NPS.
    """
    shown, small = [], []
    for sid, g in d.groupby("session_id"):
        rec = g["recommend_0_10"].dropna()
        (shown if len(g) >= min_n and len(rec) >= min_n else small).append((sid, g))
    while shown and 0 < sum(len(g) for _, g in small) < min_n:
        small.append(shown.pop(min(range(len(shown)), key=lambda i: len(shown[i][1]))))

    def row(label, g):
        rec = g.dropna(subset=["recommend_0_10"])
        stud = rec["role_group"].eq("Student").mean() if len(rec) else NAN
        v, m = nps(rec["recommend_0_10"]), nps_moe(rec["recommend_0_10"])
        exp = stud * split["s"] + (1 - stud) * split["o"] if split and len(rec) else NAN
        status = "below" if v + m < exp else "above" if v - m > exp else ""
        return {"label": label, "n": len(g), "nps_n": len(rec), "student": stud * 100, "nps": v, "moe": m, "exp": exp,
                "status": status, "when": item_stats(g, "when")}
    rows = [row(session_label(sid), g) for sid, g in shown]
    rows.sort(key=lambda r: r["nps"] - r["exp"] if r["exp"] == r["exp"] else r["nps"])
    pool = pd.concat([g for _, g in small]) if small else None
    pooled = (row(f"{len(small)} smaller date/county groups, pooled", pool)
              if pool is not None and pool["recommend_0_10"].notna().sum() >= min_n else None)
    if pooled:
        pooled["status"] = ""
    return {"rows": rows, "pooled": pooled, "n_small": len(small), "small_n": 0 if pool is None else len(pool)}


def explore_cube(d: pd.DataFrame, min_n: int) -> dict:
    """Figures for every combination of county, role and 'leave out flagged rows', for the in-page filters.

    Counties under min_n are pooled into one 'Other (…)' option, as in group_tables.csv. A view is published only if it has min_n responses, and within every filter, the views left unpublished
    (small ones, blank values, the flagged rows themselves) add up to 0 or at least min_n responses, so no
    group under min_n can be found by subtracting one published view from another one level up. Combining
    several views could still narrow a group down, as with the other tables: an internal working file.
    """
    blank = "(blank)"
    county, _ = pool_small_groups(d["county"], min_n)     # same pooling as group_tables.csv
    cols = {"county": county, "role": d["role_group"],
            "flag": is_kept(d).map({True: "kept", False: "flagged"})}
    cols = {k: c.fillna(blank).astype(str) for k, c in cols.items()}
    hidden = {"flag": {"flagged"}}
    keys = list(cols)
    values = {k: sorted(c.unique()) for k, c in cols.items()}
    eq = {(k, x): cols[k].eq(x).to_numpy() for k in keys for x in values[k]}
    combos = list(itertools.product(*[["*"] + values[k] for k in keys]))

    def mask(v):
        m = pd.Series(True, index=d.index).to_numpy()
        for k, x in zip(keys, v):
            if x != "*":
                m = m & eq[(k, x)]
        return m
    masks = {v: mask(v) for v in combos}
    n = {v: int(masks[v].sum()) for v in combos}
    offered = {v: all(x == "*" or (x != blank and x not in hidden.get(k, ())) for k, x in zip(keys, v)) for v in combos}
    shown = {v: offered[v] and n[v] >= min_n for v in combos}
    changed = True
    while changed:
        changed = False
        for i, k in enumerate(keys):
            for p in combos:
                if p[i] != "*":
                    continue
                sibs = [p[:i] + (x,) + p[i + 1:] for x in values[k]]
                if 0 < sum(n[s] for s in sibs if not shown[s]) < min_n:
                    cand = [s for s in sibs if shown[s]]
                    if cand:
                        shown[min(cand, key=n.get)] = False
                        changed = True

    def r1(x):
        return None if x != x else round(float(x), 1)
    views = {}
    for v in combos:
        if not shown[v]:
            continue
        f = figures(d[masks[v]])
        views["|".join(v)] = {
            "n": f["n"], "s": int(d.loc[masks[v], "session_id"].nunique()),
            "items": [[r1(i["before"]), r1(i["after"]), r1(i["gain"]), r1(i["moe"]), i["n"]] if i["n"] >= min_n else None
                      for i in f["items"]],
            "nps": [r1(f["nps"]), r1(f["nps_moe"]), f["nps_n"]] if f["nps_n"] >= min_n else None}
    used = [set(k.split("|")[i] for k in views) for i in range(len(keys))]
    options = {k: [x for x in values[k] if x in used[i] and x != blank and x not in hidden.get(k, ())]
               for i, k in enumerate(keys)}
    options["county"].sort(key=lambda c: (c.startswith("Other ("), -int((county == c).sum())))
    options["role"].sort(key=lambda r: ROLE_ORDER.index(r) if r in ROLE_ORDER else len(ROLE_ORDER))
    return {"views": views, "options": options, "items": [lab for _, lab in ITEMS]}


def role_rows(groups: pd.DataFrame, metric: str) -> list:
    g = groups[(groups["table"] == "Role") & (groups["metric"] == metric)].copy()
    g["order"] = g["group"].map({r: i for i, r in enumerate(ROLE_ORDER)}).fillna(len(ROLE_ORDER))
    return [r for _, r in g.sort_values("order").iterrows()]


def theme_rows(themes: pd.DataFrame, field: str, prefix: str) -> list:
    t = themes[(themes["field"] == field) & (themes["kind"] == "theme") & themes["id"].str.startswith(prefix)
               & ~themes["id"].isin(SKIP_THEMES)]
    rows = []
    for tid, sub in t.groupby("id", sort=False):
        by = {r["group"]: r for _, r in sub.iterrows()}
        rows.append({"id": tid, "label": sub["label"].iloc[0], "by": by})
    return sorted(rows, key=lambda r: -num(r["by"]["All"]["pct"]))


def usable(cell) -> bool:
    """A theme cell solid enough to compare groups on: shown, not suppressed and not under 10."""
    return cell is not None and cell["status"] == "shown"


def short(label: str) -> str:
    """Theme label up to its first parenthesis or colon, for use inside a sentence."""
    return re.split(r" \(|: ", label)[0]


def pct(x: float) -> str:
    return f"{x:.0f}%"


def signed(x: float) -> str:
    return f"{x:+.0f}".replace("-", "−")


# ---------------------------------------------------------------- HTML pieces


def tip(text: str) -> str:
    return f'tabindex="0" data-tip="{e(text)}" aria-label="{e(text)}"'


def badge(status: str, reliability: str = "") -> str:
    if reliability == "thin":
        return ' <span class="flag" title="Valid n is 10–29: read with care">thin</span>'
    if status.startswith("few"):
        return ' <span class="flag" title="Fewer than 10 comments: too few to compare groups on">under 10</span>'
    return ""


def table_view(head: list, rows: list) -> str:
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<details class="tv"><summary>Show the numbers</summary><div class="tw"><table><thead><tr>{th}</tr></thead>'
            f'<tbody>{tr}</tbody></table></div></details>')


def gridlines(ticks: list, lo: float, hi: float) -> str:
    return "".join(f'<i class="gl" style="left:{(t - lo) / (hi - lo) * 100:.2f}%"></i>' for t, _ in ticks)


def axis(ticks: list, lo: float, hi: float) -> str:
    marks = "".join(f'<span style="left:{(t - lo) / (hi - lo) * 100:.2f}%">{e(lab)}</span>' for t, lab in ticks)
    return f'<div class="row axis-row"><div></div><div class="axis">{marks}</div><div></div></div>'


def legend(items: list) -> str:
    return '<div class="legend">' + "".join(f'<span><i class="key {cls}"></i>{e(lab)}</span>' for lab, cls in items) + "</div>"


def pct_ticks(hi: float) -> list:
    step = 25 if hi > 50 else 10 if hi > 30 else 5
    return [(t, f"{t}%") for t in range(0, int(hi) + 1, step)]


def nice_max(top: float) -> float:
    return min(100, max(10, -(-top // 5) * 5))


PCT100 = [(0, "0%"), (25, "25%"), (50, "50%"), (75, "75%"), (100, "100%")]


def pm(m: float) -> str:
    """'±4' for a margin of error, or nothing when there isn't one."""
    return f"±{m:.0f}" if m == m else ""


def margin_text(m: float) -> str:
    return f", margin ±{m:.0f}" if m == m else ""


NPS_TICKS = [(-100, "−100"), (-50, "−50"), (0, "0"), (50, "+50"), (100, "+100")]


def nps_pos(v: float) -> float:
    return (max(-100.0, min(100.0, v)) + 100) / 2


def ci_bar(v: float, m: float) -> str:
    if m != m:
        return ""
    lo, hi = nps_pos(v - m), nps_pos(v + m)
    return f'<i class="ci" style="left:{lo:.2f}%;width:{hi - lo:.2f}%"></i>'


def dumbbells(groups: pd.DataFrame, h: dict, d: pd.DataFrame) -> str:
    """Before → after % agree, one panel per item, one row per role."""
    panels, trows = [], []
    for it in sorted(h["items"], key=lambda i: -i["gain"]):
        rows = [("All respondents", it["before"], it["after"], it["gain"], it["n"], "shown", "ok", True, it["moe"])]
        for r in role_rows(groups, f"agree_change_{it['key']}"):
            m = re.match(r"(\d+)% → (\d+)%", r["display"])
            ok = r["status"] == "shown" and m
            rows.append((r["group"], float(m[1]) if ok else None, float(m[2]) if ok else None, num(r["value"]),
                         r["metric_n"], r["status"], r["cell_reliability"], False,
                         role_moe(d, r["group"], r["metric"], r["metric_n"]) if ok else NAN))
        body = []
        for label, b, a, ch, n, status, rel, total, moe in rows:
            cls = "row total" if total else "row"
            if b is None:
                body.append(f'<div class="{cls}"><div class="lab">{e(label)}</div><div class="track none">suppressed (under 10)</div><div class="val"></div></div>')
                continue
            lo, hi = min(b, a), max(b, a)
            t = f"{label}: {b:.0f}% agreed before → {a:.0f}% after ({signed(ch)}%{margin_text(moe)}), n = {int(n):,}"
            body.append(
                f'<div class="{cls}"><div class="lab">{e(label)}{badge(status, rel)}</div>'
                f'<div class="track" {tip(t)}>{gridlines(PCT100, 0, 100)}'
                f'<i class="link" style="left:{lo:.2f}%;width:{hi - lo:.2f}%"></i>'
                f'<i class="dot ring" style="left:{b:.2f}%"></i><i class="dot c-gold" style="left:{a:.2f}%"></i></div>'
                f'<div class="val"><b>{signed(ch)}</b> <span class="moe">{pm(moe)}</span></div></div>')
            trows.append([e(it["label"]), e(label), pct(b), pct(a), f"{signed(ch)}%", pm(moe), f"{int(n):,}"])
        panels.append(f'<div class="panel"><h4>{e(it["label"])}</h4>{"".join(body)}{axis(PCT100, 0, 100)}</div>')
    return (legend([("Before (recalled)", "ring"), ("After the training", "c-gold")])
            + "".join(panels)
            + table_view(["Item", "Group", "Agree before", "Agree after", "Change", "Margin (95%)", "Valid pairs"], trows))


def nps_bars(groups: pd.DataFrame, h: dict, d: pd.DataFrame) -> str:
    rows = [("All respondents", h["nps"], h["nps_n"], "shown", "ok", True, h["nps_moe"])]
    rows += [(r["group"], num(r["value"]) if r["status"] == "shown" else None, r["metric_n"], r["status"], r["cell_reliability"], False,
              role_moe(d, r["group"], "recommend_nps", r["metric_n"]) if r["status"] == "shown" else NAN)
             for r in role_rows(groups, "recommend_nps")]
    body, trows = [], []
    for label, v, n, status, rel, total, moe in rows:
        cls = "row total" if total else "row"
        if v is None:
            body.append(f'<div class="{cls}"><div class="lab">{e(label)}</div><div class="track none">suppressed (under 10)</div><div class="val"></div></div>')
            continue
        left, width = (50, v / 2) if v >= 0 else (50 + v / 2, -v / 2)
        body.append(f'<div class="{cls}"><div class="lab">{e(label)}{badge(status, rel)}</div>'
                    f'<div class="track" {tip(f"{label}: NPS {signed(v)}{margin_text(moe)}, n = {int(n):,}")}>{gridlines(NPS_TICKS, -100, 100)}<i class="zero"></i>'
                    f'<i class="bar {"pos" if v >= 0 else "neg"}" style="left:{left:.2f}%;width:{width:.2f}%"></i>{ci_bar(v, moe)}</div>'
                    f'<div class="val"><b>{signed(v)}</b> <span class="moe">{pm(moe)}</span></div></div>')
        trows.append([e(label), signed(v), pm(moe), f"{int(n):,}"])
    return (legend([("NPS", "c-stone"), ("95% margin of error", "whisk")])
            + f'<div class="panel nps">{"".join(body)}{axis(NPS_TICKS, -100, 100)}</div>'
            + table_view(["Group", "NPS", "Margin (95%)", "Valid n"], trows))


def session_chart(s: dict, split) -> str:
    """Each session's NPS with its margin, against the NPS its mix of students and others predicts."""
    if not s["rows"]:
        return '<p class="muted">No session has enough responses to show on its own.</p>'
    body, trows = [], []
    for r in s["rows"] + ([s["pooled"]] if s["pooled"] else []):
        v, m, x = r["nps"], r["moe"], r["exp"]
        pill = {"below": ' <span class="flag warn">below benchmark</span>',
                "above": ' <span class="flag good">above benchmark</span>'}.get(r["status"], "")
        w = r["when"]
        t = (f"{r['label']}: NPS {signed(v)}{margin_text(m)} from {r['nps_n']:,} ratings; {r['student']:.0f}% students"
             + (f"; descriptive audience benchmark {signed(x)}" if x == x else "")
             + (f"; {w['after']:.0f}% know when to use 988 after" if w["n"] else ""))
        pooled = r is s["pooled"]
        body.append(f'<div class="row{" total" if pooled else ""}"><div class="lab">{e(r["label"])}{pill}</div>'
                    f'<div class="track sess" {tip(t)}>{gridlines(NPS_TICKS, -100, 100)}<i class="zero"></i>{ci_bar(v, m)}'
                    + (f'<i class="exp" style="left:{nps_pos(x):.2f}%"></i>' if x == x and not pooled else "")
                    + f'<i class="dot {"c-clay" if r["status"] == "below" else "c-stone"}" style="left:{nps_pos(v):.2f}%"></i></div>'
                    f'<div class="val"><b>{signed(v)}</b> <span class="moe">{pm(m)}</span></div></div>')
        trows.append([e(r["label"]), f"{r['n']:,}", pct(r["student"]), signed(v), pm(m), signed(x) if x == x else "—",
                      pct(w["after"]) if w["n"] else "—", {"below": "below benchmark", "above": "above benchmark"}.get(r["status"], "")])
    keys = [("Date/county group NPS", "c-stone"), ("Below benchmark", "c-clay"), ("95% margin of error", "whisk")]
    if split:
        keys.append(("Descriptive audience benchmark", "tick"))
    return (legend(keys) + f'<div class="panel">{"".join(body)}{axis(NPS_TICKS, -100, 100)}</div>'
            + table_view(["Date/county group", "Responses", "Students", "NPS", "Margin (95%)", "Benchmark", "Know when to use 988 (after)", ""], trows))


def since_html(prev: dict | None, cur: dict) -> str:
    """'Since the last run' strip: the previous run recorded in dashboard_history.json with a different export or settings."""
    if not prev:
        return ""
    same_input = prev["input_sha"] == cur["input_sha"]
    when = prev["built"][:10]
    chips = [("Responses", f"{prev['n']:,} → {cur['n']:,}", f"{cur['n'] - prev['n']:+,}".replace("-", "−"))]
    for k, lab in [("nps", "NPS"), ("nps_students", "Students' NPS"), ("nps_others", "Everyone else's NPS")]:
        if prev.get(k) is not None and cur.get(k) is not None:
            chips.append((lab, f"{signed(prev[k])} → {signed(cur[k])}", signed(cur[k] - prev[k])))
    for key, lab in ITEMS:
        a, b = prev["items"].get(key), cur["items"].get(key)
        if a and b:
            chips.append((f"{lab} (after)", f"{pct(a['after'])} → {pct(b['after'])}", f"{signed(b['after'] - a['after'])}%"))
    cells = "".join(f'<div><span>{e(lab)}</span><b>{e(val)}</b><i>{e(dlt)}</i></div>' for lab, val, dlt in chips)
    src = ("the same export with different settings" if same_input else e(prev["input"]))
    return (f'<div class="since"><p class="eyebrow">Since the last run · {e(when)} · {src}</p><div class="chips">{cells}</div>'
            '<p class="muted">Most responses appear in both exports, so small changes are expected; '
            'read them against the margins of error below.</p></div>')


def theme_dots(rows: list) -> str:
    """Students vs everyone else, one row per theme, sorted from 'non-students more' to 'students more'."""
    def gap(r):
        s, o = r["by"].get(STUDENTS), r["by"].get(OTHERS)
        return num(o["pct"]) - num(s["pct"]) if s is not None and o is not None else float("nan")
    rows = sorted(rows, key=lambda r: (gap(r) != gap(r), -gap(r) if gap(r) == gap(r) else 0))
    top = max([num(c["pct"]) for r in rows for g, c in r["by"].items() if g != "All" and num(c["pct"]) == num(c["pct"])] + [1])
    hi = nice_max(top)
    ticks = pct_ticks(hi)
    body, trows = [], []
    for r in rows:
        s, o = r["by"].get(STUDENTS), r["by"].get(OTHERS)
        sv, ov = (num(s["pct"]) if s is not None else float("nan")), (num(o["pct"]) if o is not None else float("nan"))
        if sv != sv or ov != ov:
            body.append(f'<div class="row"><div class="lab">{e(r["label"])}</div><div class="track none">suppressed (under 10)</div><div class="val"></div></div>')
            trows.append([e(r["label"]), "suppressed", "suppressed"])
            continue
        few = s["status"].startswith("few") or o["status"].startswith("few")
        lo, top_v = min(sv, ov), max(sv, ov)
        t = (f"{r['label']}: students {sv:.1f}% ({s['n']} of {s['denominator']}), everyone else {ov:.1f}% "
             f"({o['n']} of {o['denominator']})" + (" — a group has under 10 comments" if few else ""))
        body.append(f'<div class="row dots"><div class="lab">{e(r["label"])}{badge("few" if few else "")}</div>'
                    f'<div class="track" {tip(t)}>{gridlines(ticks, 0, hi)}'
                    f'<i class="link grey" style="left:{lo / hi * 100:.2f}%;width:{(top_v - lo) / hi * 100:.2f}%"></i>'
                    + (f'<i class="dot split" style="left:{sv / hi * 100:.2f}%"></i></div>' if abs(sv - ov) < 0.5 else
                       f'<i class="dot c-gold" style="left:{sv / hi * 100:.2f}%"></i><i class="dot c-blue" style="left:{ov / hi * 100:.2f}%"></i></div>')
                    + f'<div class="val pair"><span><i class="key c-gold"></i>{sv:.0f}</span><span><i class="key c-blue"></i>{ov:.0f}</span></div></div>')
        trows.append([e(r["label"]), f"{sv:.1f}% ({s['n']}/{s['denominator']})", f"{ov:.1f}% ({o['n']}/{o['denominator']})"])
    return (legend([("Students", "c-gold"), ("Everyone else", "c-blue")])
            + f'<div class="panel">{"".join(body)}{axis(ticks, 0, hi)}</div>'
            + table_view(["Theme", "Students", "Everyone else"], trows))


def ranked_bars(rows: list, highlight_first: bool = True) -> str:
    """One series (all respondents), ranked; the top bar carries the accent."""
    hi = nice_max(max([num(r["by"]["All"]["pct"]) for r in rows] + [1]))
    ticks = pct_ticks(hi)
    body, trows = [], []
    for i, r in enumerate(rows):
        c = r["by"]["All"]
        v, few = num(c["pct"]), c["status"].startswith("few")
        t = f"{r['label']}: {v:.1f}% ({c['n']} of {c['denominator']})"
        accent = "c-gold" if highlight_first and i == 0 else "c-olive"
        body.append(f'<div class="row"><div class="lab">{e(r["label"])}{badge(c["status"])}</div>'
                    f'<div class="track" {tip(t)}>{gridlines(ticks, 0, hi)}<i class="bar {accent}" style="left:0;width:{v / hi * 100:.2f}%"></i></div>'
                    f'<div class="val"><b>{pct(v) if v >= 1 else "<1%"}</b></div></div>')
        trows.append([e(r["label"]), f"{v:.1f}%", f"{c['n']} of {c['denominator']}" + (" (under 10)" if few else "")])
    return f'<div class="panel">{"".join(body)}{axis(ticks, 0, hi)}</div>' + table_view(["Theme", "Share", "Comments"], trows)


def intent_bars(themes: pd.DataFrame) -> str:
    t = themes[(themes["field"] == "improve") & (themes["kind"] == "intent")]
    body, trows = [], []
    for g, name in [(STUDENTS, "Students"), (OTHERS, "Everyone else")]:
        sub = {r["id"]: r for _, r in t[t["group"] == g].iterrows()}
        if not sub:
            continue
        den = next(iter(sub.values()))["denominator"]
        segs, cells = [], [f"{e(name)} ({den})"]
        for k, lab, cls in INTENTS:
            r = sub.get(k)
            v = num(r["pct"]) if r is not None else float("nan")
            if v != v or v <= 0:
                cells.append("—")
                continue
            few = r["status"].startswith("few")
            inner = f"{v:.0f}%" if v >= 9 else ""
            label = f"{name}: {lab.lower()}, {v:.1f}% ({r['n']} of {den})"
            segs.append(f'<i class="part {cls}" style="flex-grow:{v:.3f}" {tip(label)}>{inner}</i>')
            cells.append(f"{v:.1f}% ({r['n']})" + (" (under 10)" if few else ""))
        body.append(f'<div class="srow"><div class="lab">{e(name)} <span class="muted">· {den} comments</span></div>'
                    f'<div class="segbar">{"".join(segs)}</div></div>')
        trows.append(cells)
    return (legend([(lab, cls) for _, lab, cls in INTENTS]) + f'<div class="panel">{"".join(body)}</div>'
            + table_view(["Group"] + [lab for _, lab, _ in INTENTS], trows))


def month_columns(months: pd.Series) -> str:
    if months.empty:
        return '<p class="muted">No readable survey dates.</p>'
    hi = max(months.max(), 1)
    peak, last = months.idxmax(), months.index[-1]
    cols, trows = [], []
    for m, n in months.items():
        dt = datetime.strptime(m, "%Y-%m")
        lab = dt.strftime("%b") + ("<br>" + dt.strftime("’%y") if dt.month == 1 or m == months.index[0] else "<br>&nbsp;")
        show = f'<span class="cv">{n:,}</span>' if m in (peak, last) else ""
        cols.append(f'<div class="col" {tip(f"{dt:%B %Y}: {n:,} responses")}><div class="cbar-wrap">{show}'
                    f'<i class="cbar {"c-gold" if m == peak else "c-olive"}" style="height:{n / hi * 100:.2f}%"></i></div>'
                    f'<div class="cl">{lab}</div></div>')
        trows.append([f"{dt:%B %Y}", f"{n:,}"])
    return f'<div class="panel"><div class="cols">{"".join(cols)}</div></div>' + table_view(["Month", "Responses"], trows)


def group_tables_html(groups: pd.DataFrame) -> str:
    out = []
    for name, sub in groups.groupby("table", sort=False):
        metrics = list(dict.fromkeys(sub["metric_label"]))
        rows = []
        for g, gs in sub.groupby("group", sort=False):
            by = dict(zip(gs["metric_label"], gs["display"]))
            rows.append([f"{e(g)} <span class='muted'>(n {e(gs['group_n'].iloc[0])})</span>"] + [e(by.get(m, "")) for m in metrics])
        th = "".join(f"<th>{e(h)}</th>" for h in ["Group"] + metrics)
        tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
        out.append(f'<h4>{e(name)} <span class="muted">· {e(sub["filter"].iloc[0])}</span></h4>'
                   f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>')
    return "".join(out)


# ---------------------------------------------------------------- page


def font_faces() -> str:
    """Embed the open-licensed fonts in assets/fonts (SIL OFL); without them the page falls back to system fonts."""
    faces = [("Newsreader", "Newsreader-Light-latin.woff2", "normal", "300"),
             ("Newsreader", "Newsreader-LightItalic-latin.woff2", "italic", "300"),
             ("Hanken Grotesk", "HankenGrotesk-latin.woff2", "normal", "100 900")]
    css = []
    for fam, f, style, weight in faces:
        p = FONTS / f
        if p.exists():
            b64 = base64.b64encode(p.read_bytes()).decode()
            css.append(f"@font-face{{font-family:'{fam}';font-style:{style};font-weight:{weight};font-display:swap;"
                       f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    return "".join(css)


CSS = """
:root{color-scheme:light;--canvas:#f7f1e8;--surface:#e8e6e1;--ink:#1c1b1a;--stone:#4a4741;--ash:#65625d;--mute:#86837d;
--hair:#cfcac1;--grid:#d3cfc7;--gold:#a66c12;--blue:#3d6eb4;--olive:#4f6450;--clay:#a24f3c;--sand:#b3a68f;--pale:#cdc5b6;
--espresso:#39221e;--band:#19191c;--serif:'Newsreader',Georgia,'Times New Roman',serif;
--sans:'Hanken Grotesk',system-ui,-apple-system,'Segoe UI',sans-serif}
*{box-sizing:border-box}
html{background:var(--canvas)}
section{scroll-margin-top:20px}
.section-nav{display:flex;flex-wrap:wrap;gap:12px 24px;padding-top:12px;padding-bottom:12px;border-block:1px solid var(--hair);font-size:15px}
.section-nav a{color:var(--ink);text-underline-offset:4px}
a:focus-visible,summary:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--blue);outline-offset:4px}
body{margin:0;background:var(--canvas);color:var(--ink);font:400 17px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:16px;padding-top:22px;padding-bottom:22px;
font-size:14px;color:var(--stone)}
.mark{font-weight:600;letter-spacing:.14em;text-transform:uppercase;font-size:13px}
.eyebrow{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--stone);margin:0 0 18px}
h1,h2,h3{font-family:var(--serif);font-weight:300;letter-spacing:-.01em;margin:0}
h1{font-size:clamp(42px,7.2vw,84px);line-height:1.04;max-width:15ch}
h1 em,h2 em{font-style:italic}
h2{font-size:clamp(30px,4.4vw,46px);line-height:1.1;max-width:22ch}
h3{font-size:28px;line-height:1.15;margin:36px 0 6px}
h4{font:600 15px/1.3 var(--sans);margin:26px 0 10px;color:var(--ink)}
.lead{font-family:var(--serif);font-weight:300;font-size:clamp(20px,2.3vw,25px);line-height:1.35;color:var(--stone);
max-width:40ch;margin:22px 0 0}
.hero{padding-top:48px;padding-bottom:40px}
.pill{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--stone);color:var(--stone);border-radius:999px;
padding:7px 14px;font-size:13px;margin-top:28px}
.pill i{width:7px;height:7px;border-radius:50%;background:var(--gold)}
.badges{display:flex;flex-wrap:wrap;gap:28px;margin-top:44px}
.badge-c{width:168px;text-align:center}
.circle{width:148px;height:148px;margin:0 auto;border-radius:50%;border:1px solid var(--hair);background:var(--surface);
display:flex;flex-direction:column;align-items:center;justify-content:center}
.circle b{font:300 46px/1 var(--serif);color:var(--ink)}.circle span{font-size:13px;color:var(--ash);margin-top:4px}
.badge-c p{font-size:14px;color:var(--stone);margin:12px 0 0;line-height:1.35}
.facts{display:flex;flex-wrap:wrap;gap:10px 36px;margin-top:40px;padding-top:22px;border-top:1px solid var(--hair);font-size:15px;color:var(--stone)}
.facts b{font-weight:600;color:var(--ink)}
section{padding:72px 0}
.sec-head{margin-bottom:28px}
.sec-head .sub{color:var(--stone);max-width:62ch;margin:16px 0 0}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:24px;padding:28px}
.findings{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}
.finding{background:var(--surface);border:1px solid var(--hair);border-radius:24px;padding:26px 26px 24px;display:flex;flex-direction:column}
.finding .n{font:300 15px/1 var(--serif);font-style:italic;color:var(--gold);margin-bottom:16px}
.finding h3{font-size:25px;margin:0 0 12px;line-height:1.18}
.finding p{font-size:15px;color:var(--stone);margin:0}
.band{background:var(--espresso);color:#f7f1e8}
.band .eyebrow,.band .sub{color:#ddbe78}.band h2{color:#f7f1e8}.band .sub{color:#e7dccb}
.band .card{border-color:transparent}
.note{font-size:14px;color:var(--stone);margin:18px 0 0;max-width:72ch}
.band .note{color:#e7dccb}
.legend{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:14px;color:var(--stone);margin:0 0 14px}
.legend span{display:inline-flex;align-items:center;gap:8px}
.key{display:inline-block;width:11px;height:11px;border-radius:50%;flex:none}
.key.ring{background:var(--surface);box-shadow:inset 0 0 0 2px var(--mute)}
.c-gold{background:var(--gold)}.c-blue{background:var(--blue)}.c-olive{background:var(--olive)}
.c-sand{background:var(--sand)}.c-pale{background:var(--pale)}
.row{display:grid;grid-template-columns:minmax(150px,32%) 1fr 92px;align-items:center;gap:14px;min-height:34px}
.row.total .lab{font-weight:600}
.lab{font-size:14px;line-height:1.3;color:var(--ink)}
.val{font-size:14px;color:var(--stone);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.val b{font-weight:600;color:var(--ink)}
.val.pair{display:flex;flex-direction:column;align-items:flex-end;gap:1px;font-size:13px}
.val.pair span{display:inline-flex;align-items:center;gap:5px}.val.pair .key{width:8px;height:8px}
.track{position:relative;height:26px;outline:none;border-radius:6px}
.track:focus-visible,.col:focus-visible,.part:focus-visible{box-shadow:0 0 0 2px var(--stone)}
.track.none{font-size:13px;color:var(--ash);display:flex;align-items:center}
.gl{position:absolute;top:0;bottom:0;width:1px;background:var(--grid)}
.zero{position:absolute;top:0;bottom:0;left:50%;width:1px;background:var(--mute)}
.link{position:absolute;top:12px;height:2px;background:var(--gold);opacity:.6}.link.grey{background:var(--mute)}
.dot{position:absolute;top:7px;width:12px;height:12px;margin-left:-6px;border-radius:50%;box-shadow:0 0 0 2px var(--surface)}
.dot.split{background:linear-gradient(90deg,var(--gold) 50%,var(--blue) 50%)}
.row.dots{min-height:46px}
.dot.ring{background:var(--surface);box-shadow:inset 0 0 0 2px var(--mute),0 0 0 2px var(--surface)}
.bar{position:absolute;top:5px;height:16px;border-radius:0 4px 4px 0}
.bar.pos{background:var(--stone)}.bar.neg{background:var(--clay);border-radius:4px 0 0 4px}
.row.total .bar.pos{background:var(--gold)}
.axis-row{min-height:20px}.axis{position:relative;height:18px;font-size:12px;color:var(--ash);font-variant-numeric:tabular-nums}
.axis span{position:absolute;transform:translateX(-50%);white-space:nowrap}
.axis span:first-child{transform:none}.axis span:last-child{transform:translateX(-100%)}
.panel h4:first-child{margin-top:0}
.srow{display:grid;grid-template-columns:minmax(150px,24%) 1fr;gap:14px;align-items:center;margin:10px 0}
.segbar{display:flex;gap:2px;height:30px}
.part{display:flex;align-items:center;min-width:3px;border-radius:4px;font-style:normal;font-size:13px;font-weight:600;
padding:0 8px;color:#fff;outline:none}
.part.c-sand,.part.c-pale{color:var(--ink)}
.cols{display:flex;align-items:flex-end;gap:6px;height:230px;padding-top:22px}
.col{flex:1;display:flex;flex-direction:column;align-items:stretch;height:100%;outline:none;border-radius:6px}
.cbar-wrap{flex:1;position:relative;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}
.cbar{display:block;width:min(26px,80%);border-radius:4px 4px 0 0;min-height:2px}
.cv{font-size:13px;font-weight:600;margin-bottom:4px;font-variant-numeric:tabular-nums}
.cl{font-size:11.5px;color:var(--ash);text-align:center;line-height:1.25;margin-top:6px;border-top:1px solid var(--mute);padding-top:6px}
.cav{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0 40px}
.cav div{border-top:1px solid var(--hair);padding:16px 0}
.cav b{display:block;font-weight:600;margin-bottom:4px}.cav p{margin:0;font-size:15px;color:var(--stone)}
.flag{font-size:11px;font-weight:600;letter-spacing:.04em;color:var(--stone);border:1px solid var(--hair);border-radius:999px;
padding:1px 7px;white-space:nowrap;vertical-align:1px}
.muted{color:var(--ash);font-weight:400}
.tv{margin-top:18px;font-size:14px}.tv summary{cursor:pointer;color:var(--stone);display:inline-block;border:1px solid var(--hair);
border-radius:999px;padding:5px 14px;list-style:none}.tv summary::-webkit-details-marker{display:none}
.tv[open] summary{background:var(--canvas)}
.tw{overflow-x:auto}
table{border-collapse:collapse;font-size:13px;margin-top:12px;min-width:100%}
th,td{text-align:left;padding:7px 12px 7px 0;border-bottom:1px solid var(--hair);vertical-align:top}
th{color:var(--stone);font-weight:600}td{font-variant-numeric:tabular-nums}
footer{background:var(--band);color:#a8a5a0;font-size:13px;padding:44px 0 56px;margin-top:40px}
footer .mark{color:#f7f1e8;display:block;margin-bottom:14px}footer p{max-width:80ch;margin:0 0 8px}
#tip{position:fixed;pointer-events:none;background:var(--band);color:#f7f1e8;font-size:13px;padding:8px 12px;border-radius:12px;
max-width:320px;opacity:0;transition:opacity .1s;z-index:10;line-height:1.4}
.moe{color:var(--ash);font-weight:400;font-size:12.5px}
.c-stone{background:var(--stone)}.c-clay{background:var(--clay)}
.nps .bar{top:2px;height:15px}
.ci{position:absolute;top:19px;height:7px;border:solid var(--ink);border-width:0 1.5px;opacity:.7}
.ci::after{content:"";position:absolute;left:0;right:0;top:2.5px;height:1.5px;background:var(--ink)}
.sess .ci{top:9.5px}
.exp{position:absolute;top:2px;height:22px;width:2px;margin-left:-1px;background:var(--ink);border-radius:1px}
.key.whisk{width:16px;height:8px;border-radius:0;border:solid var(--ink);border-width:0 1.5px;
background:linear-gradient(var(--ink),var(--ink)) center/100% 1.5px no-repeat}
.key.tick{width:2px;height:13px;border-radius:1px;background:var(--ink)}
.flag.warn{border-color:var(--clay);box-shadow:inset 0 0 0 1px var(--clay)}.flag.good{border-color:var(--olive)}
.next{margin-top:auto;padding-top:16px;border-top:1px solid var(--hair);margin-top:18px;display:flex;flex-direction:column;gap:6px}
.next span{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--ash)}
.next a{font-size:14px;color:var(--ink);text-decoration:none;border-bottom:1px solid var(--hair);align-self:flex-start;line-height:1.35}
.next a:hover{border-color:var(--gold)}
.plan{padding-top:24px}
.acts{display:flex;flex-direction:column;gap:14px}
.act{display:grid;grid-template-columns:52px 1fr;gap:18px;background:var(--surface);border:1px solid var(--hair);border-radius:24px;padding:24px 26px;
scroll-margin-top:24px}
.act:target{box-shadow:0 0 0 2px var(--gold)}
.al{width:44px;height:44px;border-radius:50%;background:var(--ink);color:var(--canvas);display:flex;align-items:center;justify-content:center;
font:300 22px/1 var(--serif)}
.act h3{font-size:24px;margin:4px 0 14px;max-width:46ch}
.act dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px 24px;margin:0}
.act dt{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--ash);margin-bottom:3px}
.act dd{margin:0;font-size:14px;color:var(--ink);line-height:1.4}
.since{margin-top:30px;background:var(--surface);border:1px solid var(--hair);border-radius:24px;padding:22px 24px}
.since .eyebrow{margin-bottom:14px}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px 22px}
.chips div{display:flex;flex-direction:column;gap:2px}.chips span{font-size:13px;color:var(--ash)}
.chips b{font-weight:600;font-variant-numeric:tabular-nums}.chips i{font-style:normal;font-size:13px;color:var(--stone)}
.since .muted{font-size:13px;margin:14px 0 0}
.dash-head{display:flex;justify-content:space-between;align-items:center;gap:24px;min-height:72px;border-bottom:1px solid var(--hair);font-size:14px}
.dash-head>div{display:flex;align-items:baseline;gap:18px}.dash-head .mark{letter-spacing:.08em}.dash-meta{color:var(--ash);font-variant-numeric:tabular-nums}.dash-head nav{display:flex;gap:18px}.dash-head a{color:var(--stone);text-decoration:none;font-size:13px;font-weight:600}.dash-head a:hover{text-decoration:underline;text-underline-offset:4px}
.overview{padding:36px 0 28px;background:var(--canvas)}.overview-copy{display:flex;align-items:end;justify-content:space-between;gap:32px;margin-bottom:22px}.overview .eyebrow{margin:0 0 8px}.overview h1{font-size:42px;line-height:1.05;max-width:16ch}.overview-copy>p{max-width:32ch;margin:0 0 4px;color:var(--stone);font-size:16px;line-height:1.4}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--hair);border:1px solid var(--hair);border-radius:14px;overflow:hidden}.kpi{background:var(--surface);padding:16px 18px;min-height:104px;display:flex;flex-direction:column;justify-content:space-between}.kpi span{font-size:12px;color:var(--ash);font-weight:700;letter-spacing:.06em;text-transform:uppercase}.kpi b{font:300 31px/1 var(--serif);font-variant-numeric:tabular-nums;color:var(--ink)}.kpi small{color:var(--stone);font-size:12px}.overview .pill{margin-top:14px;padding:5px 11px;font-size:12px}.overview .since{margin-top:16px;padding:16px 18px;border-radius:14px}
.decision-section{padding:24px 0 40px}.decision-grid{display:grid;grid-template-columns:minmax(0,6fr) minmax(360px,6fr);gap:18px}.decision-panel{background:var(--surface);border:1px solid var(--hair);border-radius:16px;overflow:hidden}.decision-panel>header{padding:20px 22px 16px;border-bottom:1px solid var(--hair)}.decision-panel .eyebrow{margin:0 0 8px}.decision-panel h2{font-family:var(--sans);font-size:21px;font-weight:650;letter-spacing:-.02em;max-width:none}.signal{display:grid;grid-template-columns:28px minmax(0,1fr);gap:14px;padding:19px 22px;border-bottom:1px solid var(--hair);align-items:start}.signal:last-child,.action:last-child{border-bottom:0}.signal-num{font:600 12px/1 var(--sans);color:var(--gold);padding-top:3px}.signal h3,.action h3{font-family:var(--sans);font-size:16px;font-weight:650;letter-spacing:-.01em;line-height:1.3;margin:0 0 5px}.signal p,.action>p{font-size:13.5px;line-height:1.45;color:var(--stone);margin:0}.signal-links{display:flex;flex-wrap:wrap;gap:5px 12px;margin-top:9px}.signal-links a{font-size:12px;line-height:1.3;color:var(--olive);font-weight:650;text-decoration:none}.signal-links a:hover{text-decoration:underline}
.action{padding:19px 22px;border-bottom:1px solid var(--hair);scroll-margin-top:20px}.action h3{margin-bottom:8px}.action-evidence{margin-top:11px!important;padding-top:10px;border-top:1px solid var(--hair);font-size:12px!important}.action-evidence b{color:var(--ink)}.decision-note{font-size:12px;color:var(--ash);margin:12px 2px}.measurement{margin:18px 0 0;border-top:1px solid var(--hair);padding-top:14px;font-size:13px;color:var(--stone)}.measurement summary{cursor:pointer;font-weight:650;color:var(--ink)}.measurement p{max-width:80ch;margin:9px 0}
main>section:not(.overview):not(.decision-section):not(.methods){padding:36px 0}main>section:not(.overview):not(.decision-section):not(.methods) .wrap{max-width:1280px}main>section:not(.overview):not(.decision-section):not(.methods) .sec-head{display:flex;gap:24px;align-items:baseline;margin-bottom:14px}main>section:not(.overview):not(.decision-section):not(.methods) h2{font-family:var(--sans);font-size:21px;font-weight:650;max-width:30ch}main>section:not(.overview):not(.decision-section):not(.methods) .sec-head .sub{font-size:13px;margin:0;max-width:58ch}.card{border-radius:16px;padding:22px}.methods{padding:32px 0 56px}.methods details{border-top:1px solid var(--hair);border-bottom:1px solid var(--hair);padding:17px 0}.methods summary{font:650 18px/1.3 var(--sans);cursor:pointer}.methods-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 32px;margin-top:12px}.methods-grid p{font-size:13px;line-height:1.45;color:var(--stone);margin:10px 0}.methods-grid b{color:var(--ink)}
@media (max-width:640px){.wrap{padding:0 16px}.act{grid-template-columns:1fr;padding:20px}.al{width:36px;height:36px;font-size:18px}
 .dash-head{min-height:60px}.dash-head nav{display:none}.dash-head>div{display:block}.dash-meta{display:block;font-size:12px;margin-top:3px}.overview{padding:26px 0 22px}.overview-copy{display:block;margin-bottom:18px}.overview h1{font-size:32px}.overview-copy>p{margin-top:10px}.kpis{grid-template-columns:repeat(2,1fr)}.kpi{min-height:92px;padding:13px}.kpi b{font-size:27px}.decision-grid{grid-template-columns:1fr;gap:14px}.signal{grid-template-columns:24px 1fr}.methods-grid{grid-template-columns:1fr}
section{padding:52px 0}.card{padding:20px 16px;border-radius:18px}
.row{grid-template-columns:1fr 64px;gap:2px 10px}.row .lab{grid-column:1/-1}.row .track{grid-column:1}.row .val{grid-column:2}
.axis-row>div:first-child{display:none}.srow{grid-template-columns:1fr}.badges{gap:14px;justify-content:space-between}
.badge-c{width:calc(33% - 10px)}.circle{width:96px;height:96px}.circle b{font-size:30px}.cols{gap:3px}.topbar .meta{display:none}}
@media print{.tv{display:none}section{padding:28px 0}.band{background:none;color:var(--ink)}.band h2{color:var(--ink)}}
"""

JS = """
const tip=document.getElementById('tip');
function show(el,x,y){tip.textContent=el.dataset.tip;tip.style.opacity=1;
 const r=tip.getBoundingClientRect();tip.style.left=Math.max(8,Math.min(x+14,innerWidth-r.width-8))+'px';tip.style.top=Math.max(8,y-r.height-12)+'px'}
document.addEventListener('pointermove',ev=>{const el=ev.target.closest('[data-tip]');if(el)show(el,ev.clientX,ev.clientY);else tip.style.opacity=0});
document.addEventListener('focusin',ev=>{const el=ev.target.closest('[data-tip]');if(!el)return;const r=el.getBoundingClientRect();show(el,r.left+r.width/2,r.top)});
document.addEventListener('focusout',()=>tip.style.opacity=0);
"""


def section(eyebrow: str, title: str, sub: str, content: str, note: str = "", band: bool = False) -> str:
    section_id = f' id="{e(eyebrow.lower().replace(" ", "-"))}"' if eyebrow else ""
    eyebrow_html = f'<p class="eyebrow">{e(eyebrow)}</p>' if eyebrow else ""
    return (f'<section{section_id}{" class=band" if band else ""}><div class="wrap"><div class="sec-head">{eyebrow_html}'
            f'<h2>{title}</h2>{f"<p class=sub>{e(sub)}</p>" if sub else ""}</div>'
            f'<div class="card">{content}</div>{f"<p class=note>{e(note)}</p>" if note else ""}</div></section>')


def findings(h: dict, themes: pd.DataFrame, sess: dict) -> tuple:
    """Up to three supported findings and three proposed pilots for staff review."""
    out, acts = [], {}

    def act(aid, short_do, do, owner, effort, measure):
        acts.setdefault(aid, {"short": short_do, "do": do, "owner": owner, "effort": effort, "measure": measure, "why": []})
        return aid

    best = max(h["items"], key=lambda i: i["gain"])
    activity = act("activity", "Test a short interactive activity",
                   "Pilot a five-minute scenario and a staff-approved call demonstration, replacing lecture time where feasible",
                   "Curriculum lead", "8–12 preparation hours plus 1 hour to brief facilitators",
                   "Use the same knowledge check in comparable sessions with and without the activity. Report correct answers, valid counts and missing answers by item. Track delivery time; small nonrandom differences remain exploratory.")
    out.append((f"{best['label']}: <em>{signed(best['gain'])}%</em>",
                f"{pct(best['before'])} agreed before the training (recalled) and {pct(best['after'])} after "
                f"(margin ±{best['moe']:.0f}). These are self-rated changes, not independently measured learning or behavior.", [activity]))
    split = h["nps_split"]
    if split and abs(split["s"] - split["o"]) >= 20:
        out.append((f"Recommendation gap: students <em>{signed(split['s'])}</em>, others {signed(split['o'])} NPS",
                    f"From {split['sn']:,} student ratings and {split['on']:,} other ratings. This gap does not establish weaker learning or explain its cause. "
                    "Test how participants understand the recommendation scale; midpoint confusion remains a hypothesis.", [activity]))
    q = themes[(themes["field"] == "improve") & (themes["id"] == "question")].set_index("group")
    if "All" in q.index and num(q.loc["All", "n"]) > 0:
        qr = q.loc["All"]
        survey = act("survey", "Separate questions from improvement feedback",
                     "Use separate question and improvement fields, record the form version, and standardize facilitator instructions",
                     "Evaluation lead", "4–6 setup hours",
                     "Test whether participants can explain what each field and the recommendation scale asks. Track form-version coverage, completion and missing answers. Preserve compatible definitions when comparing runs; fewer questions alone is not success.")
        faq = act("faq", "Provide a staff-verified FAQ",
                  "Have service staff verify answers to common questions, then pilot a short FAQ in student sessions",
                  "Training lead and service reviewer", "8–12 preparation and review hours",
                  "Use three staff-approved comprehension items before and after the FAQ. Report correct-answer counts and missing responses for each item. Keep inviting questions; their frequency is not a learning measure.")
        out.append((f"<em>{pct(num(qr['pct']))}</em> of improvement comments are questions",
                    f"{qr['n']} of {qr['denominator']} substantive comments ask questions. Possible prompt or form differences remain unconfirmed. "
                    "Separate feedback from questions so both can guide staff decisions.", [survey, faq]))
    for i, (_, _, ids) in enumerate(out, 1):
        for aid in ids:
            acts[aid]["why"].append(i)
    # Survey clarity comes first in the pilot sequence, when question comments support it.
    acts = {k: acts[k] for k in ("survey", "faq", "activity") if k in acts}
    mapping = {'survey':'delivery','faq':'delivery','activity':'adaptation'}
    out = [(title, body, list(dict.fromkeys(mapping[x] for x in ids))) for title, body, ids in out]
    acts = {
      'delivery': dict(short='Immediate delivery tweaks', do='Immediate delivery tweaks', owner='Training lead, facilitator and service reviewer',
        effort='Correct before next delivery; restore essential access immediately',
        measure='Check comprehension, delivery completeness and understanding of the two survey prompts.',
        evidence='Call experience: 65/263 questions; privacy/police: 24/263. These are broad topics, not tracing-specific counts.',
        change='Add a 60–90 second staff-verified call walkthrough and explanation of privacy/location handling. Separate questions from feedback. Check AV and keep an accessible backup.',
        trigger='Any confirmed factual error or missing core explanation: correct it. Any inaccessible essential content: restore access or provide an equivalent.', why=[]),
      'adaptation': dict(short='Targeted content adaptations', do='Targeted content adaptations', owner='Curriculum lead with student input and service review',
        effort='Version scenarios and compare the same learning objective',
        measure='Compare correct-answer shares, omissions and delivery time. Proposed retention criterion: +10 percentage points with no material increase in missingness or planned time; exploratory, not proof.',
        evidence='Interaction: 38/568 improvement comments; examples: 33/568. The recommendation gap does not establish the cause.',
        change='Rewrite generic prompts around fictional exam pressure, falling behind, or concern for a friend at school. Keep factual guidance and scoring difficulty consistent.',
        trigger='Proposed: below 80% correct on the same item in at least 2 of the latest 3 comparable sessions, each with at least 10 answered checks. Review omissions before deciding.', why=[]),
      'structural': dict(short='Structural / logistical interventions', do='Structural / logistical interventions', owner='Program coordinator and venue contact',
        effort='Match the response to the documented cause; costs depend on the remedy',
        measure='Track lost-time percentage, recurring problems, essential content delivered and access.',
        evidence='Broad logistics: 16/568 comments; pacing: 18/568. Historical data does not measure venue interruptions.',
        change='Use a quieter room, microphone, protected teaching slot or registration before teaching when documented incidents support the change. Meet access requirements without waiting for recurrence.',
        trigger='Proposed: at least 10% of planned teaching time lost → debrief before next booking. Same problem in 2 of the latest 3 sessions at the same venue/configuration → document a structural change.', why=[])}
    def count(theme_id):
        rows = themes[(themes['field']=='improve') & (themes['id']==theme_id) & (themes['group']=='All')]
        if rows.empty: return 'unavailable'
        r = rows.iloc[0]
        if r['status'] != 'shown': return 'suppressed / unavailable'
        return f"{r['n']}/{r['denominator']}"
    acts['delivery']['evidence'] = f"Call experience: {count('q_call_experience')} questions; privacy/police: {count('q_privacy_police')}. These broad topics are not tracing-specific counts."
    acts['adaptation']['evidence'] = f"Interaction: {count('im_more_interactive')} improvement comments; examples: {count('im_examples_stories')}. The recommendation gap does not establish the cause."
    acts['structural']['evidence'] = f"Broad logistics: {count('im_logistics')} comments; pacing: {count('im_time_pacing')}. Historical data does not measure venue interruptions."
    # Bucket titles and "because" bodies, grounded in the survey figures above.
    def theme(field, theme_id, group='All'):
        rows = themes[(themes['field']==field) & (themes['id']==theme_id) & (themes['group']==group)]
        return rows.iloc[0] if not rows.empty and rows.iloc[0]['status'] == 'shown' else None
    a_title = 'Staff demonstration of a 60–90 second phone call with 988'
    acts['delivery'].update(short=a_title, do=a_title)
    qa, qc, qp = theme('improve', 'question'), theme('improve', 'q_call_experience'), theme('improve', 'q_privacy_police')
    if qa is not None and qc is not None:
        qs = theme('improve', 'question', STUDENTS)
        who = f", {int(qs['n'])} of them from students" if qs is not None else ""
        priv = f", and another {int(qp['n'])} asked about privacy or police involvement" if qp is not None else ""
        acts['delivery']['change'] = (
            f"Because many students still didn't understand what actually happens on a call: {int(qa['n'])} of "
            f"{int(qa['denominator'])} improvement comments were questions{who}. The most common question topic was what a call "
            f"is like and who answers ({int(qc['n'])} of {int(qc['denominator'])}){priv}. A short, staff-verified "
            "demonstration answers these directly, including how privacy and location are handled.")
    b_title = 'Student-specific training'
    acts['adaptation'].update(short=b_title, do=b_title)
    if split:
        acts['adaptation']['change'] = (
            f"Because students gave the training an NPS of {signed(split['s'])}, compared with {signed(split['o'])} from other "
            f"respondents ({split['sn']:,} student and {split['on']:,} other ratings). Rebuild examples and activities around "
            "student situations (exam pressure, falling behind, worrying about a friend at school) while keeping the factual guidance the same.")
    c_title = 'Environmental changes: quieter room, better microphone'
    acts['structural'].update(short=c_title, do=c_title)
    lg = theme('improve', 'im_logistics')
    if lg is not None:
        acts['structural']['change'] = (
            f"Because participants raised the setting itself: {int(lg['n'])} of {int(lg['denominator'])} improvement comments were about "
            "logistics such as tech, format and setting, including a request for a microphone, a request for a private space to ask "
            "questions, and audio/video problems that got in the way of the presentation. Use a quieter room and a working microphone.")
    for i, (_, _, ids) in enumerate(out, 1):
        for aid in ids: acts[aid]['why'].append(i)
    return out, acts


def snapshot(data: dict, h: dict) -> dict:
    """The figures recorded in dashboard_history.json, for the next run's 'since the last run' strip."""
    man, split = data["manifest"], h["nps_split"]

    def r1(x):
        return None if x != x else round(float(x), 1)
    return {"input_sha": man.get("input", {}).get("sha256", ""), "config_sha": man.get("config", {}).get("sha256", ""),
            "input": man.get("input", {}).get("name", "?"), "run_id": man.get("run_id", "?"),
            "built": datetime.now().isoformat(timespec="seconds"), "n": h["n"], "nps": r1(h["nps"]),
            "nps_students": r1(split["s"]) if split else None, "nps_others": r1(split["o"]) if split else None,
            "items": {i["key"]: {"after": r1(i["after"]), "gain": r1(i["gain"])} for i in h["items"]}}


def build(data: dict, min_n: int, previous: dict | None = None) -> str:
    d, groups, themes = data["clean"], data["groups"], data["themes"]
    man, tman = data["manifest"], data["themes_manifest"]
    h = headline(d, min_n)
    sess = sessions(d, min_n, h["nps_split"])
    when = next(i for i in h["items"] if i["key"] == "when")
    checked = int(tman.get("validation", {}).get("rows_checked", 0))
    span = (f"{h['first']:%b %Y} – {h['last']:%b %Y}" if pd.notna(h["first"]) else "dates unknown")

    status = ("Comment themes are provisional: hand check pending" if not checked
              else f"Comment themes remain provisional: {checked} of "
                   f"{tman.get('validation', {}).get('rows_total', checked)} review entries scored; "
                   "calibration sample, not held-out accuracy")
    fs, acts = findings(h, themes, sess)
    num_of = {aid: i for i, aid in enumerate(acts, 1)}
    kpis = "".join([
        f'<div class="kpi"><span>Describe 988</span><b>{signed(next(i for i in h["items"] if i["key"] == "describe")["gain"])}%</b><small>self-rated change</small></div>',
        f'<div class="kpi"><span>Know when to use it</span><b>{signed(when["gain"])}%</b><small>self-rated change</small></div>',
        f'<div class="kpi"><span>Would recommend</span><b>{signed(h["nps"])}%</b><small>{pm(h["nps_moe"])} · {h["nps_n"]:,} ratings</small></div>',
        f'<div class="kpi"><span>Priority actions</span><b>{len(acts)}</b><small>proposed for staff review</small></div>',
    ])
    hero = f"""
<header class="wrap dash-head"><div><span class="mark">EveryMind 988 survey</span><span class="dash-meta">{h["n"]:,} responses · {e(span)}</span></div>
<nav aria-label="Dashboard sections"><a href="#decisions">Decisions</a><a href="#evidence">Evidence</a><a href="#methods">Methods</a></nav></header>
<main>
<section class="overview" id="overview"><div class="wrap"><div class="overview-copy"><h1>Three findings. Three next actions.</h1></div>
<div class="kpis">{kpis}</div><div class="pill"><i></i>{e(status)}</div>{since_html(previous, snapshot(data, h))}</div></section>"""
    finding_rows = "".join(
        f'<article class="signal"><span class="signal-num">{i:02d}</span><div><h3>{t}</h3><p>{e(p)}</p></div>'
        f'<div class="signal-links">' + "".join(f'<a href="#act-{aid}">{e(acts[aid]["short"])}</a>' for aid in ids) + "</div></article>"
        for i, (t, p, ids) in enumerate(fs, 1))
    logs, policy = data.get('interventions', ({k: [] for k in interventions.FIELDS}, interventions.DEFAULT))
    states = interventions.evaluate(logs, policy, min_n)
    acts['adaptation']['trigger'] = (f"Proposed: below {policy['correct_threshold']:.0%} correct on the same item in at least {policy['repeats']} of the latest {policy['window']} comparable sessions, each with at least {max(min_n, policy['minimum_answered'])} answered checks. Review omissions before deciding.")
    acts['structural']['trigger'] = (f"Proposed: at least {policy['lost_fraction']:.0%} of planned teaching time lost → debrief before next booking. Same problem in {policy['repeats']} of the latest {policy['window']} sessions at the same venue/configuration → document a structural change.")

    act_rows = ""
    for aid, a in acts.items():
        state = states[aid]
        details = ''.join('<li>'+e(v)+'</li>' for v in state['details'])
        human = [r for r in logs['actions'] if r['bucket']==aid]
        reviews = ''.join('<li>'+e(r['action_id']+': '+(r['decision'] or 'proposed action')+' — '+r['owner_role']+'; '+r['action_taken'])+'</li>' for r in human)
        act_rows += (f'<article class="action" id="act-{aid}"><h3>{e(a["do"])}</h3><p>{e(a["change"])}</p>'
          f'<p class="action-evidence"><b>Why it belongs here:</b> {e(a["evidence"])}</p></article>')
    versions = sorted(set('/'.join(r[k] for k in ('form_version','curriculum_version','faq_version','scenario_version')) for r in logs['sessions']))
    decisions = (f'<section class="decision-section" id="decisions"><div class="wrap"><div class="decision-grid">'
                 f'<section class="decision-panel"><header><p class="eyebrow">What the data says</p><h2>Three supported findings</h2></header>{finding_rows}</section>'
                 f'<section class="decision-panel actions-panel"><header><p class="eyebrow">Recommended next actions</p><h2>Test, measure, decide</h2></header>{act_rows}</section>'
                 '</div><p class="decision-note">Actions and thresholds are proposals for staff review, not conclusions proven by the historical survey.</p>'
                 f'<details class="measurement"><summary>Measurement and implementation context</summary><p>Measurement coverage: {len(logs["sessions"])} logged events; {len(logs["assessments"])} assessment summaries. Policy: {e(policy["version"])}. Form/curriculum/FAQ/scenario versions: {e(", ".join(versions) or "awaiting measurement")}.</p><p>Missingness tolerance must be agreed before retention decisions. Small learner groups are suppressed. Calculated flags do not close actions; staff record their review separately.</p></details></div></section>')

    all_up = all(num(r["value"]) > 0 for it in ITEMS for r in role_rows(groups, f"agree_change_{it[0]}") if r["status"] == "shown")
    q = themes[(themes["field"] == "improve") & (themes["id"] == "question")].set_index("group")
    s_q = num(q.loc[STUDENTS, "pct"]) if STUDENTS in q.index else float("nan")
    months = h["months"]
    split = h["nps_split"]
    nps_title = ("Would they recommend it?" if not split or abs(split["s"] - split["o"]) < 20 else
                 "Students give <em>lower recommendation scores</em>" if split["s"] < split["o"] else "Other respondents give <em>lower recommendation scores</em>")
    below = sum(r["status"] == "below" for r in sess["rows"])
    sess_title = "Exploratory session comparisons"
    sess_sub = (f"Training date × county is a proxy for sessions, not a verified event identifier. Groups with at least {min_n} ratings show NPS and a 95% margin of error. "
                + ("The tick is a descriptive benchmark: the group's share of student ratings times the whole-sample student NPS, plus the remaining share times the other respondents' NPS. "
                   "It uses this same dataset and is not a validated prediction. " if split else "")
                + (f"{sess['n_small']} smaller groups ({sess['small_n']:,} responses) are pooled." if sess["n_small"] else ""))
    wf = h["without_flagged"]

    body = "".join([
        section("", "Every group reports more confidence after the training" if all_up else "Self-rated confidence, before and after",
                "Share answering agree or strongly agree (4–5). Both ratings were collected after the session; the ‘before’ is recalled, so this is self-report, not measured learning. ± is the 95% margin of error on the change.", dumbbells(groups, h, d)),
        section("", nps_title, "Net Promoter Score: the share scoring 9–10 minus the share scoring 0–6, from −100 to +100. The thin line on each bar is its 95% margin of error.", nps_bars(groups, h, d), f"{h['fives']} respondents chose exactly 5 and {pct(h['fives_student_pct'])} of them were students (students are {pct(h['student_pct'])} of respondents). Some may have used the midpoint as ‘no opinion’; the data can't confirm this. NPS measures recommendation, not learning.", band=True),
        section("", "What people found <em>most valuable</em> depends on who they are", "Themes in answers to ‘the most valuable thing you learned’. Sorted from themes non-students mention more (top) to themes students mention more (bottom). A comment can have several themes.", theme_dots(theme_rows(themes, "most_valuable", "mv_"))),
        section("", "For students, the feedback box <em>became a question box</em>" if s_q == s_q and s_q >= 40 else "What the ‘how to improve’ box was used for", "Every substantive answer to ‘how could the training be improved’, by what kind of answer it was.", intent_bars(themes) + '<h3>What they still wanted to know</h3><p class="muted" style="margin:0 0 18px">' + f'Topics of the comments that asked a question (all respondents).</p>' + ranked_bars(theme_rows(themes, "improve", "q_"))),
        section("", "What participants would change", "Themes in suggestion and question comments, as a share of all substantive ‘improve’ comments. Outreach requests support a small demand pilot before expansion. Track registrations, attendance and staff time rather than fewer requests.", ranked_bars(theme_rows(themes, "improve", "im_"))),
        section("", sess_title, sess_sub, session_chart(sess, split), "Flags compare each group’s margin with the benchmark. They omit benchmark uncertainty, dependence within sessions and adjustment for repeated comparisons. Use them to generate questions, never to rank trainers or count successful sessions."),
        section("", (f"{datetime.strptime(months.idxmax(), '%Y-%m'):%B %Y} was the busiest month, with <em>{months.max():,}</em> responses") if not months.empty else "Responses by month", "Survey responses by the month they were submitted.", month_columns(months)),
    ])
    evidence = '<span id="evidence" aria-hidden="true"></span>'
    methods = (f'<section class="methods" id="methods"><div class="wrap"><details><summary>Methods and limits</summary><div class="methods-grid">'
               '<p><b>Self-report, not measured learning.</b> Before and after ratings were both given after training, with “before” recalled. There is no knowledge test or comparison group.</p>'
               f'<p><b>Nothing is dropped.</b> {h["flagged"]} responses carry a review flag and remain in the primary figures. The sensitivity sample has NPS {signed(wf["nps"])}.</p>'
               f'<p><b>Small groups stay protected.</b> Groups and cells under {min_n} are pooled or suppressed; “thin” marks a figure with 10–29 valid answers.</p>'
               '<p><b>Comment themes are rules, not a model.</b> They are provisional during calibration and require human review before use outside this internal analysis.</p>'
               '</div></details></div></section>')

    inp = man.get("input", {})
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Survey dashboard</title><style>{font_faces()}{CSS}</style></head>
<body>
{hero}{decisions}{evidence}{body}{methods}</main>
<div id="tip" role="tooltip"></div><script>{JS}</script></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "output"), help="folder holding the checked bundle (default ./output)")
    ap.add_argument("--config", default=str(HERE / "cleaning_config.json"), help="for the small-group threshold")
    a = ap.parse_args(argv)
    out = Path(a.out)
    min_n = int(strip_comments(json.loads(Path(a.config).read_text()))["thresholds"]["small_group_min_n"])
    data = load(out)
    data['interventions'] = interventions.read_logs(out / 'measurement')
    cur = snapshot(data, headline(data["clean"], min_n))
    hist_path = out / HISTORY
    try:
        history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    except (ValueError, OSError):
        print(f"WARNING: {hist_path} is unreadable; starting a new history.", file=sys.stderr)
        history = []
    same = lambda s: (s.get("input_sha"), s.get("config_sha")) == (cur["input_sha"], cur["config_sha"])  # noqa: E731
    previous = next((s for s in reversed(history) if not same(s)), None)
    page = build(data, min_n, previous)
    dest = out / "dashboard.html"
    tmp = out / ".dashboard.html.tmp"
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(dest)                                        # never leave a half-written page
    # one entry per export + settings, newest last; a re-run of the same one replaces its entry
    history = ([s for s in history if not same(s)] + [cur])[-HISTORY_KEEP:]
    tmp = out / f".{HISTORY}.tmp"
    tmp.write_text(json.dumps(history, indent=1), encoding="utf-8")
    tmp.replace(hist_path)
    print(f"Dashboard: {dest}" + (f" (compared with the run of {previous['built'][:10]})" if previous else ""))


if __name__ == "__main__":
    main()
