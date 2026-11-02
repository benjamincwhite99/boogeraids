#!/usr/bin/env python3
"""Tests for themes.py using synthetic comments (no real survey data).

    python3 tests/test_themes.py

Exits with status 1 if any test fails.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import themes  # noqa: E402
from test_edge_cases import row, write_xlsx, tables_well_formed  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def run(args):
    return subprocess.run([sys.executable, str(ROOT / "themes.py"), *args], capture_output=True, text=True)


def main():
    spec = themes.compile_codebook(json.loads((ROOT / "theme_codebook.json").read_text(encoding="utf-8")))
    code = lambda field, text: themes.code_comment(text, spec[field], field)

    # --- rules on single comments
    for text, want in [("How many people call 988 each day?", "question"), ("Nothing, it was great!", "no_change"),
                       ("The training was great!", "no_change"), ("No, I have no more questions.", "no_change"),
                       ("nothing more", "no_change"), ("Maybe make it more interactive. Great job!", "suggestion"),
                       ("warmline", "question"), ("By making it longer", "suggestion")]:
        got = code("improve", text)[0]
        check(f"intent of {text!r} is {want}", got == want, f"got {got}")
    i, t = code("improve", "Maybe make it more interactive. Great job!")
    check("praise + suggestion keeps the suggestion theme", "im_more_interactive" in t, t)
    i, t = code("improve", "Can people see if you called 988 through phone bills?")
    check("phone-bill question is a privacy question", "q_privacy_police" in t, t)
    i, t = code("improve", "how many people call")
    check("question themes don't leak into suggestions", not any(x.startswith("im_") for x in t), t)
    i, t = code("most_valuable", "That 988 isn't just for suicide")
    check("'isn't just for suicide' -> beyond suicide", t == ["mv_beyond_suicide"], t)
    i, t = code("most_valuable", "you don't have to call 988 only for suicide, you can call if you need to talk")
    check("'don't have to call only for suicide' isn't flagged as a misconception", "mv_possible_misconception" not in t
          and "mv_beyond_suicide" in t, t)
    i, t = code("most_valuable", "Anyone can call 24/7")
    check("one comment, two themes", {"mv_anyone_any_age", "mv_always_available"} <= set(t), t)
    i, t = code("most_valuable", "988")
    check("bare '988' -> unspecific", t == ["mv_unspecific"], t)
    i, t = code("most_valuable", "Aprendí que no solo es para suicidio")
    check("accents are folded before matching (Spanish)", "mv_beyond_suicide" in t, t)
    i, t = code("most_valuable", "That it’s not just for suicidal people.")
    check("curly apostrophe matches", "mv_beyond_suicide" in t, t)

    # Validation-driven intent boundaries, using synthetic wording.
    cases = [
        ("Please call 988!", "unclear"),
        ("I would like to learn more about identifying warning signs", "suggestion"),
        ("I want to learn more about being able to help a neighbor", "suggestion"),
        ("I would like to learn more about how the service started", "question"),
        ("Nothing, but I appreciate the visit", "no_change"),
        ("Nothing, but I appreciate the visit; more games please", "suggestion"),
    ]
    for text, want in cases:
        check(f"review regression: {text}", code("improve", text)[0] == want)
    check("when to call does not imply around-the-clock availability",
          "mv_always_available" not in code("most_valuable", "Learning when to call")[1])

    # Malformed annotations must not silently become false positives.
    frame = pd.DataFrame([dict(source_row=1, field="improve", text_redacted="A synthetic comment",
                               themes="im_content", intent="suggestion")])
    sample = pd.DataFrame([dict(sample_id="A-001", source_row="1", field="improve",
                                comment="A synthetic comment", auto_intent="suggestion",
                                auto_themes="im_content", reviewed="y", correct_intent="",
                                wrong_themes="", missing_themes="", notes="", reviewer="A")])
    bad = sample.copy()
    bad.loc[0, "auto_themes"] = "im_typo"
    scored = themes.score_sample(bad, frame, spec)
    check("unknown original auto-code is excluded and warned", scored["rows"] == 0 and
          "unknown theme" in scored["warnings"][0] and not scored["reviewers"])
    bad = sample.copy()
    bad.loc[0, "correct_intent"] = "no_change"
    scored = themes.score_sample(bad, frame, spec)
    check("intent correction with incompatible themes is scored for intent only",
          scored["rows"] == 1 and scored["intent_n"] == 1 and scored["theme_rows"] == 0 and
          not scored["per_theme"] and "conflict" in scored["warnings"][0])
    bad = sample.copy()
    bad.loc[0, "correct_intent"] = "typo"
    check("invalid intent is excluded", themes.score_sample(bad, frame, spec)["rows"] == 0)
    bad = sample.copy()
    bad.loc[0, "wrong_themes"] = "im_logistics"
    check("removing a code that was not displayed is flagged",
          themes.score_sample(bad, frame, spec)["rows"] == 0)

    check("learning a specific feature does not imply discovering 988 exists",
          "mv_what_988_is" not in code("most_valuable", "I didn't know that you could text 988")[1])
    check("Spanish age eligibility is not beyond-suicide evidence",
          "mv_beyond_suicide" not in code("most_valuable", "No solo es para los estudiantes, tambien para adultos")[1])
    check("calling when lonely is beyond-suicide evidence",
          "mv_beyond_suicide" in code("most_valuable", "You can call when feeling lonely")[1])
    check("more activities does not imply more people",
          "im_reach_more_people" not in code("improve", "Bring more activities")[1])
    check("more people remains outreach",
          "im_reach_more_people" in code("improve", "Bring in more people")[1])
    check("Spanish praise is no change", code("improve", "Me encanto la presentacion")[0] == "no_change")
    check("standalone information request is a question",
          code("improve", "More about 988") == ("question", ["q_unspecific"]))
    quoted = sample.copy()
    quoted.loc[0, "wrong_themes"] = '"im_content"'
    check("quoted correction codes are parsed without changing source entries",
          themes.score_sample(quoted, frame, spec)["per_theme"]["im_content"]["fp"] == 1)

    # --- codebook errors
    tmp = Path(tempfile.mkdtemp(prefix="themes_test_"))
    try:
        cb = json.loads((ROOT / "theme_codebook.json").read_text(encoding="utf-8"))
        bad = json.loads(json.dumps(cb))
        bad["most_valuable"]["themes"][0]["patterns"].append("(unclosed")
        (tmp / "bad.json").write_text(json.dumps(bad))
        p = run(["--codebook", str(tmp / "bad.json"), "--out", str(tmp / "none")])
        check("invalid regex stops with the theme id, no traceback", p.returncode != 0 and "mv_beyond_suicide" in p.stderr
              and "Traceback" not in p.stderr, p.stderr[-300:])
        dup = json.loads(json.dumps(cb))
        dup["improve"]["themes"].append(dict(dup["improve"]["themes"][0]))
        (tmp / "dup.json").write_text(json.dumps(dup))
        p = run(["--codebook", str(tmp / "dup.json"), "--out", str(tmp / "none")])
        check("duplicate theme id is refused", p.returncode != 0 and "used twice" in p.stderr, p.stderr[-300:])
        p = run(["--out", str(tmp / "none")])
        check("no cleaned bundle -> clear message", p.returncode != 0 and "Run clean.py" in p.stderr, p.stderr[-300:])

        # --- end to end on a synthetic export
        comments = [("That 988 isn't just for suicide", "How many people call 988?"),
                    ("Anyone can call 24/7", "Nothing, it was great!"),
                    ("988", "More interactive activities please"),
                    ("When to call 988", "=1+1"),
                    ("You can call for a friend", "what | happens when you call?"),
                    ("Warning signs", "Can people see if you called 988 through phone bills?")]
        rows = [row(i + 1, valuable=v, improve=m) for i, (v, m) in enumerate(comments)]
        rows += [row(i, valuable=f"anyone can call {i}", improve=None) for i in range(7, 31)]
        rows += [row(31, valuable=None, improve="None")]
        write_xlsx(rows, tmp / "export.xlsx")
        out = tmp / "out"
        c = subprocess.run([sys.executable, str(ROOT / "clean.py"), str(tmp / "export.xlsx"), "--out", str(out)],
                           capture_output=True, text=True)
        check("clean.py runs on the synthetic export", c.returncode == 0, c.stderr[-400:])
        p = run(["--out", str(out)])
        check("themes.py runs end to end", p.returncode == 0, p.stderr[-400:])
        th = out / "themes"
        files = ["comment_codes.csv", "theme_summary.csv", "themes_report.md", "themes_manifest.json"]
        check("all theme outputs written", all((th / f).exists() for f in files), list(th.iterdir()) if th.exists() else "")
        man = json.loads((th / "themes_manifest.json").read_text())
        check("manifest fingerprints match the files", all(man["files"][f]["sha256"] == themes.sha256(th / f) for f in themes.BUNDLE))
        check("manifest records the cleaned file it read", man["clean_responses_sha256"] == themes.sha256(out / "clean_responses.csv"))
        codes = pd.read_csv(th / "comment_codes.csv", keep_default_na=False, na_values=[""])
        d = pd.read_csv(out / "clean_responses.csv", keep_default_na=False, na_values=[""])
        want = int(d["text_most_valuable_type"].eq("substantive").sum() + d["text_improve_type"].eq("substantive").sum())
        check("one coded row per substantive comment", len(codes) == want, f"{len(codes)} vs {want}")
        check("literal 'None' is not coded as a comment", not ((codes["field"] == "improve") & (codes["text"] == "None")).any())
        md = (th / "themes_report.md").read_text()
        check("report tables are well formed (pipe in a comment)", tables_well_formed(md))
        check("report says not yet validated", "Not yet validated" in md)
        s = pd.read_csv(th / "theme_summary.csv", keep_default_na=False, na_values=[""])
        sup = s[(s["group"] != "All") & s["status"].str.startswith("suppressed")]
        check("group with under 10 comments is suppressed (both groups)", len(sup) > 0 and sup["n"].isna().all()
              and set(sup["group"]) == {"Students", "All other respondents"}, sup.head(3).to_string())
        alls = s[s["group"] == "All"]
        check("overall figures are never suppressed", alls["status"].ne("suppressed").all() and alls["n"].notna().all())

        # --- validation samples: one file per reviewer
        paths = themes.sample_paths(out)
        check("three reviewer files created", [x.name for x in paths] == [f"{themes.SAMPLE_PREFIX}{x}.xlsx" for x in "ABC"],
              [x.name for x in paths])
        from openpyxl import load_workbook
        sm = themes.read_samples(paths)
        returned = sm[sm.reviewer.eq("B")][themes.SAMPLE_COLS].copy()
        returned.loc[returned.index[0], "reviewed"] = "y"
        csv_path = out / "theme_validation_sample_B.csv"
        returned.to_csv(csv_path, index=False)
        imported = themes.read_samples(themes.sample_paths(out))
        check("returned CSV replaces matching blank assignments without double counting",
              len(imported) == len(sm) and imported.reviewed.eq("y").sum() == 1)
        returned.loc[returned.index[0], "comment"] = "changed source text"
        returned.to_csv(csv_path, index=False)
        try:
            themes.read_samples(themes.sample_paths(out))
            refused = False
        except SystemExit:
            refused = True
        check("CSV with conflicting source text is refused", refused)
        csv_path.unlink()
        check("instructions are the first sheet", load_workbook(paths[0]).sheetnames == ["How to fill in", "Sample", "Codebook"])
        keys = sm.assign(k=sm["field"] + ":" + sm["source_row"]).groupby("reviewer")["k"].apply(set)
        shared = keys["A"] & keys["B"] & keys["C"]
        check("every reviewer gets the overlap comments", len(shared) > 0, len(shared))
        check("other comments don't repeat across reviewers",
              all(not ((keys[x] & keys[y]) - shared) for x, y in [("A", "B"), ("A", "C"), ("B", "C")]))
        big = pd.DataFrame({"field": ["most_valuable"] * 400 + ["improve"] * 400, "source_row": range(800),
                            "text_redacted": "x", "intent": "", "themes": ""})
        dr = themes.draw_samples(big, 3, 10)
        check("full-size draw: 200 per reviewer, 100 per question", all(len(v) == 200 and (v["field"] == "improve").sum() == 100
                                                                         for v in dr.values()), {k: len(v) for k, v in dr.items()})
        allk = [(f, r) for v in dr.values() for f, r in zip(v["field"], v["source_row"])]
        check("full-size draw: 560 different comments (20 shared)", len(set(allk)) == 560, len(set(allk)))
        check("same seed, same draw", all(themes.draw_samples(big, 3, 10)[x].equals(dr[x]) for x in dr))
        wb = load_workbook(paths[0])
        ws = wb["Sample"]
        formula = [c for c in ws.iter_rows(min_row=2) for c in c if c.data_type == "f"]
        check("comment '=1+1' is stored as text in the sample, not a formula", not formula)
        check("dropdowns on reviewed and correct_intent", len(ws.data_validations.dataValidation) == 2)
        hdr = [c.value for c in ws[1]]
        col = {h: i + 1 for i, h in enumerate(hdr)}
        # reviewer A checks two rows: one agrees, one says a theme was wrong
        marked = 0
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, col["field"]).value == "most_valuable" and marked < 2:
                ws.cell(r, col["reviewed"]).value = "y"
                if marked == 1:
                    ws.cell(r, col["wrong_themes"]).value = (ws.cell(r, col["auto_themes"]).value or "").split(";")[0]
                marked += 1
        wb.save(paths[0])
        before = themes.sha256(paths[0])
        p = run(["--out", str(out)])
        md = (th / "themes_report.md").read_text()
        check("reviewed rows are scored in the report", p.returncode == 0 and "2 sample comments checked by hand" in md, p.stderr[-300:])
        check("a wrong theme lowers precision below 100%", "precision 100.0%" not in md.split("Overall:")[1][:60] if "Overall:" in md else False)
        check("plain re-run leaves the filled samples untouched", themes.sha256(paths[0]) == before)
        p = run(["--out", str(out), "--new-sample"])
        check("--new-sample refuses to replace samples with entries", p.returncode != 0 and "--force" in p.stderr
              and themes.sha256(paths[0]) == before, p.stderr[-300:])
        # B and C check a shared comment; B and C disagree on it
        sid = {}
        for x, mark in [("B", ""), ("C", "WRONG")]:
            wb = load_workbook(paths["ABC".index(x)])
            ws = wb["Sample"]
            for r in range(2, ws.max_row + 1):
                if (ws.cell(r, col["field"]).value + ":" + str(ws.cell(r, col["source_row"]).value)) in shared:
                    ws.cell(r, col["reviewed"]).value = "y"
                    if mark:
                        ws.cell(r, col["missing_themes"]).value = "mv_warning_signs"
                    sid[x] = ws.cell(r, col["sample_id"]).value
                    break
            wb.save(paths["ABC".index(x)])
        p = run(["--out", str(out)])
        md = (th / "themes_report.md").read_text()
        import re
        ag = re.search(r"identical theme codes in (\d+) of (\d+) reviewer pairs", md)
        check("reviewer disagreement on a shared comment is reported", ag and int(ag[1]) < int(ag[2]) and "3 reviewer(s)" in md,
              md[md.find("**Reviewer agreement"):][:300])
        # entries without reviewed = y are flagged, not counted
        wb = load_workbook(paths[1])
        ws = wb["Sample"]
        ws.cell(ws.max_row, col["wrong_themes"]).value = "mv_statistics"
        wb.save(paths[1])
        p = run(["--out", str(out)])
        check("entries without reviewed = y are warned about", "reviewed isn't y" in p.stdout, p.stdout[-300:])
        # a teammate returns a renamed copy alongside the original
        dup = out / f"{themes.SAMPLE_PREFIX}A (1).xlsx"
        shutil.copy(paths[0], dup)
        p = run(["--out", str(out)])
        check("a duplicate copy of a reviewer's file is refused", p.returncode != 0 and "same sample rows" in p.stderr, p.stderr[-300:])
        dup.unlink()
        # comment edited in the sample -> skipped, not scored against the wrong row
        wb = load_workbook(paths[0])
        ws = wb["Sample"]
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, col["reviewed"]).value == "y":
                ws.cell(r, col["comment"]).value = "something else entirely"
                break
        wb.save(paths[0])
        p = run(["--out", str(out)])
        md = (th / "themes_report.md").read_text()
        check("sample row whose comment no longer matches is skipped with a warning",
              "3 sample comments checked by hand" in md and "no longer matches" in p.stdout, p.stdout[-300:])
        p = run(["--out", str(out), "--new-sample", "--force", "--reviewers", "2"])
        check("--new-sample --force --reviewers 2 replaces the files", p.returncode == 0 and
              [x.name for x in themes.sample_paths(out)] == [f"{themes.SAMPLE_PREFIX}{x}.xlsx" for x in "AB"], p.stderr[-300:])

        # --- refuses a cleaned file that doesn't match its manifest
        with open(out / "clean_responses.csv", "a") as f:
            f.write("\n")
        p = run(["--out", str(out)])
        check("edited clean_responses.csv is refused", p.returncode != 0 and "doesn't match run_manifest" in p.stderr, p.stderr[-300:])
        check("failed run leaves the previous themes output in place", (th / "themes_report.md").exists()
              and not any(x.name.startswith(".themes.staging") for x in out.iterdir()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    width = max(len(n) for n, _, _ in results)
    fails = 0
    for name, ok, detail in results:
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else detail}")
    print(f"\n{len(results) - fails}/{len(results)} theme tests passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
