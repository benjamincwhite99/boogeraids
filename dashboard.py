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
import base64
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

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


def nps(scores: pd.Series) -> float:
    return ((scores >= 9).mean() - (scores <= 6).mean()) * 100


def headline(d: pd.DataFrame, min_n: int) -> dict:
    """Whole-sample figures, computed the same way as §6 of data_quality_report.md."""
    items = []
    for key, label in ITEMS:
        pairs = d[[f"pre_{key}", f"post_{key}"]].dropna()
        before, after = (pairs[f"pre_{key}"] >= 4).mean() * 100, (pairs[f"post_{key}"] >= 4).mean() * 100
        items.append({"key": key, "label": label, "n": len(pairs), "before": before, "after": after, "gain": after - before})
    rec = d["recommend_0_10"].dropna()
    student = d["role_group"].eq("Student")
    s_rec, o_rec = d.loc[student, "recommend_0_10"].dropna(), d.loc[~student, "recommend_0_10"].dropna()
    fives = d[d["recommend_0_10"].eq(5)]
    dates = pd.to_datetime(d["survey_date"], errors="coerce").dropna()
    months = d["survey_month"].replace("", pd.NA).dropna().value_counts().sort_index()
    return {"n": len(d), "sessions": int(d["session_id"].replace("", pd.NA).nunique()),
            "first": dates.min(), "last": dates.max(), "items": items, "months": months,
            "nps": nps(rec), "nps_n": len(rec),
            # students vs everyone else is shown only when both groups reach the minimum group size
            "nps_split": (nps(s_rec), len(s_rec), nps(o_rec), len(o_rec)) if min(len(s_rec), len(o_rec)) >= min_n else None,
            "fives": len(fives), "fives_student_pct": fives["role_group"].eq("Student").mean() * 100 if len(fives) else 0,
            "student_pct": student.mean() * 100,
            "flagged": int((~d["in_sensitivity_sample"].astype(str).str.lower().eq("true")).sum()),
            "ambiguous_rows": int(d["flag_ambiguous_label"].astype(str).str.lower().eq("true").sum()),
            "likely_n": int(d["likely_call_0_10"].notna().sum())}


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


def dumbbells(groups: pd.DataFrame, h: dict) -> str:
    """Before → after % agree, one panel per item, one row per role."""
    panels, trows = [], []
    for it in sorted(h["items"], key=lambda i: -i["gain"]):
        rows = [("All respondents", it["before"], it["after"], it["gain"], it["n"], "shown", "ok", True)]
        for r in role_rows(groups, f"agree_change_{it['key']}"):
            m = re.match(r"(\d+)% → (\d+)%", r["display"])
            ok = r["status"] == "shown" and m
            rows.append((r["group"], float(m[1]) if ok else None, float(m[2]) if ok else None, num(r["value"]),
                         r["metric_n"], r["status"], r["cell_reliability"], False))
        body = []
        for label, b, a, ch, n, status, rel, total in rows:
            cls = "row total" if total else "row"
            if b is None:
                body.append(f'<div class="{cls}"><div class="lab">{e(label)}</div><div class="track none">suppressed (under 10)</div><div class="val"></div></div>')
                continue
            lo, hi = min(b, a), max(b, a)
            t = f"{label}: {b:.0f}% agreed before → {a:.0f}% after ({signed(ch)} pts), n = {int(n):,}"
            body.append(
                f'<div class="{cls}"><div class="lab">{e(label)}{badge(status, rel)}</div>'
                f'<div class="track" {tip(t)}>{gridlines(PCT100, 0, 100)}'
                f'<i class="link" style="left:{lo:.2f}%;width:{hi - lo:.2f}%"></i>'
                f'<i class="dot ring" style="left:{b:.2f}%"></i><i class="dot c-gold" style="left:{a:.2f}%"></i></div>'
                f'<div class="val"><b>{signed(ch)}</b> pts</div></div>')
            trows.append([e(it["label"]), e(label), pct(b), pct(a), f"{signed(ch)} pts", f"{int(n):,}"])
        panels.append(f'<div class="panel"><h4>{e(it["label"])}</h4>{"".join(body)}{axis(PCT100, 0, 100)}</div>')
    return (legend([("Before (recalled)", "ring"), ("After the training", "c-gold")])
            + "".join(panels)
            + table_view(["Item", "Group", "Agree before", "Agree after", "Change", "Valid pairs"], trows))


def nps_bars(groups: pd.DataFrame, h: dict) -> str:
    ticks = [(-100, "−100"), (-50, "−50"), (0, "0"), (50, "+50"), (100, "+100")]
    rows = [("All respondents", h["nps"], h["nps_n"], "shown", "ok", True)]
    rows += [(r["group"], num(r["value"]) if r["status"] == "shown" else None, r["metric_n"], r["status"], r["cell_reliability"], False)
             for r in role_rows(groups, "recommend_nps")]
    body, trows = [], []
    for label, v, n, status, rel, total in rows:
        cls = "row total" if total else "row"
        if v is None:
            body.append(f'<div class="{cls}"><div class="lab">{e(label)}</div><div class="track none">suppressed (under 10)</div><div class="val"></div></div>')
            continue
        left, width = (50, v / 2) if v >= 0 else (50 + v / 2, -v / 2)
        body.append(f'<div class="{cls}"><div class="lab">{e(label)}{badge(status, rel)}</div>'
                    f'<div class="track" {tip(f"{label}: NPS {signed(v)}, n = {int(n):,}")}>{gridlines(ticks, -100, 100)}<i class="zero"></i>'
                    f'<i class="bar {"pos" if v >= 0 else "neg"}" style="left:{left:.2f}%;width:{width:.2f}%"></i></div>'
                    f'<div class="val"><b>{signed(v)}</b></div></div>')
        trows.append([e(label), signed(v), f"{int(n):,}"])
    return f'<div class="panel">{"".join(body)}{axis(ticks, -100, 100)}</div>' + table_view(["Group", "NPS", "Valid n"], trows)


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
.row{display:grid;grid-template-columns:minmax(150px,32%) 1fr 74px;align-items:center;gap:14px;min-height:34px}
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
@media (max-width:640px){.wrap{padding:0 16px}section{padding:52px 0}.card{padding:20px 16px;border-radius:18px}
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
    return (f'<section{" class=band" if band else ""}><div class="wrap"><div class="sec-head"><p class="eyebrow">{e(eyebrow)}</p>'
            f'<h2>{title}</h2>{f"<p class=sub>{e(sub)}</p>" if sub else ""}</div>'
            f'<div class="card">{content}</div>{f"<p class=note>{e(note)}</p>" if note else ""}</div></section>')


def findings(h: dict, themes: pd.DataFrame) -> list:
    """Short, data-written findings. Each one is included only when the data supports it."""
    out = []
    best = max(h["items"], key=lambda i: i["gain"])
    out.append((f"{best['label']}: <em>{signed(best['gain'])} points</em>",
                f"{pct(best['before'])} agreed before the training (recalled) and {pct(best['after'])} after. "
                "The largest self-rated gain of the three questions."))
    if h["nps_split"]:
        s, sn, o, on = h["nps_split"]
        if abs(s - o) >= 20:
            who = "Students" if s < o else "Everyone else"
            out.append((f"{who} rate the training far lower: NPS <em>{signed(min(s, o))}</em> vs {signed(max(s, o))}",
                        f"Would-recommend scores from {sn:,} students and {on:,} other respondents. "
                        f"{h['fives']} people chose exactly 5, {pct(h['fives_student_pct'])} of them students."))
    rows = theme_rows(themes, "most_valuable", "mv_")
    gaps = [(num(r["by"][OTHERS]["pct"]) - num(r["by"][STUDENTS]["pct"]), r) for r in rows
            if usable(r["by"].get(STUDENTS)) and usable(r["by"].get(OTHERS))]
    if gaps:
        g, r = max(gaps, key=lambda x: x[0])
        if g >= 5:
            out.append((f"Non-students valued learning “{e(short(r['label']))}” most",
                        f"{pct(num(r['by'][OTHERS]['pct']))} of other respondents' “most valuable” comments, "
                        f"against {pct(num(r['by'][STUDENTS]['pct']))} of students'."))
        g, r = min(gaps, key=lambda x: x[0])
        if g <= -5:
            out.append((f"Students took away “{e(short(r['label']))}”",
                        f"{pct(num(r['by'][STUDENTS]['pct']))} of students' “most valuable” comments, "
                        f"against {pct(num(r['by'][OTHERS]['pct']))} of everyone else's."))
    q = themes[(themes["field"] == "improve") & (themes["id"] == "question")].set_index("group")
    topics = theme_rows(themes, "improve", "q_")
    if STUDENTS in q.index and num(q.loc[STUDENTS, "pct"]) >= 25:
        topic = f" The most common topic: {short(topics[0]['label']).lower()}." if topics else ""
        out.append((f"<em>{pct(num(q.loc[STUDENTS, 'pct']))}</em> of students' “improve” comments were questions",
                    f"{q.loc[STUDENTS, 'n']} student questions, a ready-made list of what students still want to know.{topic}"))
    sugg = theme_rows(themes, "improve", "im_")
    if sugg:
        c = sugg[0]["by"]["All"]
        out.append((f"Most requested change: {e(short(sugg[0]['label']).lower())}",
                    f"{pct(num(c['pct']))} of all “improve” comments ({c['n']} of {c['denominator']})."))
    return out


def build(data: dict, min_n: int) -> str:
    d, groups, themes = data["clean"], data["groups"], data["themes"]
    man, tman = data["manifest"], data["themes_manifest"]
    h = headline(d, min_n)
    when = next(i for i in h["items"] if i["key"] == "when")
    checked = int(tman.get("validation", {}).get("rows_checked", 0))
    span = (f"{h['first']:%b %Y} – {h['last']:%b %Y}" if pd.notna(h["first"]) else "dates unknown")

    status = ("Comment themes are provisional: hand check pending" if not checked
              else f"Comment themes checked against {checked} hand-coded comments")
    badges = "".join(
        f'<div class="badge-c"><div class="circle"><b>{signed(i["gain"])}</b><span>points</span></div><p>{e(i["label"])}</p></div>'
        for i in sorted(h["items"], key=lambda i: -i["gain"]))
    nps_fact = f"<span>Would recommend: <b>NPS {signed(h['nps'])}</b></span>"
    hero = f"""
<div class="wrap hero">
  <p class="eyebrow">Post-training survey · {e(span)}</p>
  <h1>After the training, <em>{pct(when["after"])}</em> say they know when to use 988.</h1>
  <p class="lead">Up from {pct(when["before"])} who recall knowing before. Self-rated agreement on all three questions, before and after:</p>
  <div class="badges">{badges}</div>
  <div class="facts"><span><b>{h["n"]:,}</b> responses</span><span><b>{h["sessions"]}</b> training session{"" if h["sessions"] == 1 else "s"}</span>
    <span><b>{pct(h["student_pct"])}</b> students</span>{nps_fact}</div>
  <div class="pill"><i></i>{e(status)}</div>
</div>"""

    fs = findings(h, themes)
    finding_cards = "".join(f'<div class="finding"><div class="n">{i:02d}</div><h3>{t}</h3><p>{e(p)}</p></div>'
                            for i, (t, p) in enumerate(fs, 1))
    key = (f'<section><div class="wrap"><div class="sec-head"><p class="eyebrow">At a glance</p>'
           f'<h2>What the survey says, in {len(fs)} findings</h2></div><div class="findings">{finding_cards}</div></div></section>')

    all_up = all(num(r["value"]) > 0 for it in ITEMS for r in role_rows(groups, f"agree_change_{it[0]}") if r["status"] == "shown")
    q = themes[(themes["field"] == "improve") & (themes["id"] == "question")].set_index("group")
    s_q = num(q.loc[STUDENTS, "pct"]) if STUDENTS in q.index else float("nan")
    months = h["months"]
    split = h["nps_split"]
    nps_title = ("Would they recommend it?" if not split or abs(split[0] - split[2]) < 20 else
                 "Students and everyone else <em>see it differently</em>")

    body = "".join([
        section("01 · Confidence", "Every group reports more confidence after the training" if all_up else "Self-rated confidence, before and after",
                "Share answering agree or strongly agree (4–5). Both ratings were collected after the session; "
                "the ‘before’ is recalled, so this is self-report, not measured learning.",
                dumbbells(groups, h)),
        section("02 · Recommendation", nps_title,
                "Net Promoter Score: the share scoring 9–10 minus the share scoring 0–6, from −100 to +100.",
                nps_bars(groups, h),
                f"{h['fives']} respondents chose exactly 5 and {pct(h['fives_student_pct'])} of them were students "
                f"(students are {pct(h['student_pct'])} of respondents). Some may have used the midpoint as ‘no opinion’; "
                "the data can't confirm this.", band=True),
        section("03 · What stuck", "What people found <em>most valuable</em> depends on who they are",
                f"Themes in answers to ‘the most valuable thing you learned’. Sorted from themes non-students mention more "
                f"(top) to themes students mention more (bottom). A comment can have several themes.",
                theme_dots(theme_rows(themes, "most_valuable", "mv_"))),
        section("04 · The feedback box",
                "For students, the feedback box <em>became a question box</em>" if s_q == s_q and s_q >= 40
                else "What the ‘how to improve’ box was used for",
                "Every substantive answer to ‘how could the training be improved’, by what kind of answer it was.",
                intent_bars(themes) + '<h3>What they still wanted to know</h3><p class="muted" style="margin:0 0 18px">'
                f'Topics of the comments that asked a question (all respondents).</p>' + ranked_bars(theme_rows(themes, "improve", "q_"))),
        section("05 · Suggestions", "What participants would change",
                "Themes in suggestion and question comments, as a share of all substantive ‘improve’ comments.",
                ranked_bars(theme_rows(themes, "improve", "im_"))),
        section("06 · Reach", (f"{datetime.strptime(months.idxmax(), '%Y-%m'):%B %Y} was the busiest month, "
                               f"with <em>{months.max():,}</em> responses") if not months.empty else "Responses by month",
                "Survey responses by the month they were submitted.", month_columns(months)),
    ])

    caveats = [
        ("Self-report, not measured learning", "Before and after ratings were both given after the training, with ‘before’ recalled. "
         "There is no knowledge test, comparison group or record of calls."),
        ("Nothing is dropped", f"{h['flagged']} responses carry a review flag (speeders, possible duplicates and similar). "
         "They are kept, and the data-quality report shows results without them."),
        ("Two labels left unscored", f"{h['ambiguous_rows']} responses use “Very agree” or “Satisfied”, which aren't on the standard scale. "
         "They stay unscored until EveryMind confirms what they mean."),
        ("“Likely to call 988” is partial", f"Only {h['likely_n']:,} people answered it, so it is left out of the headline figures."),
        ("Comment themes are keyword rules", "A first pass by rules, not a person reading each comment. "
         + ("A hand check of a random sample is pending." if not checked else f"Checked against {checked} hand-coded comments.")),
        ("Small groups are protected", f"Groups and cells under {min_n} are pooled or suppressed. ‘Thin’ marks groups of 10–29."),
    ]
    cav = "".join(f"<div><b>{e(t)}</b><p>{e(p)}</p></div>" for t, p in caveats)
    body += (f'<section><div class="wrap"><div class="sec-head"><p class="eyebrow">07 · About this data</p>'
             f'<h2>Read this before using the numbers</h2></div><div class="cav">{cav}</div></div></section>')
    body += (f'<section style="padding-top:0"><div class="wrap"><details class="tv"><summary>All group comparisons (tables)</summary>'
             f'<div class="card" style="margin-top:16px">{group_tables_html(groups)}</div></details></div></section>')

    inp = man.get("input", {})
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Understanding 988 Survey</title><style>{font_faces()}{CSS}</style></head>
<body>
<header class="wrap topbar"><span class="mark">Understanding 988</span><span class="meta">Training survey report · run {e(man.get("run_id", "?"))}</span></header>
{hero}{key}{body}
<footer><div class="wrap"><span class="mark">Understanding 988</span>
<p>Built by dashboard.py from {e(inp.get("name", "?"))} (SHA-256 {e(inp.get("sha256", "")[:12])}…) on {datetime.now():%Y-%m-%d %H:%M}.
Aggregate figures only; no comment text. Small cells are suppressed within each table, but comparing tables could still narrow
down a small group, so treat this as an internal working file.</p>
<p>Full detail: data_quality_report.md and themes/themes_report.md. Fonts: Newsreader and Hanken Grotesk (SIL Open Font License).</p>
</div></footer>
<div id="tip" role="tooltip"></div><script>{JS}</script></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "output"), help="folder holding the checked bundle (default ./output)")
    ap.add_argument("--config", default=str(HERE / "cleaning_config.json"), help="for the small-group threshold")
    a = ap.parse_args(argv)
    out = Path(a.out)
    min_n = int(strip_comments(json.loads(Path(a.config).read_text()))["thresholds"]["small_group_min_n"])
    page = build(load(out), min_n)
    dest = out / "dashboard.html"
    tmp = out / ".dashboard.html.tmp"
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(dest)                                        # never leave a half-written page
    print(f"Dashboard: {dest}")


if __name__ == "__main__":
    main()
