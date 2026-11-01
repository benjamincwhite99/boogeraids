#!/usr/bin/env python3
"""Edge-case tests for clean.py using a synthetic export (no real survey data).

Each case is something a future Microsoft Forms export could plausibly contain. The original
workbook never triggers most of them, which is why passing validate_clean.py on it isn't enough.

    python3 tests/test_edge_cases.py

Exits with status 1 if any test fails.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
HEADERS = ['Id', 'Start time', 'Completion time', 'Language', 'Are you a....?', 'Please enter the date of your training.',
           'Which county is this training affiliated with?\xa0',
           'Please tell us how much you disagree or agree with the statements..I can identify mental health challenges and crises.',
           'Please tell us how much you disagree or agree with the statements..I can describe 988 and how it works.',
           'Please tell us how much you disagree or agree with the statements..I know when to use 988.',
           'Now think back to before you took part in the Understanding 988 training.\xa0\xa0.I could\xa0identify mental health challenges and crises.',
           'Now think back to before you took part in the Understanding 988 training.\xa0\xa0.I could describe 988 and how it works.',
           'Now think back to before you took part in the Understanding 988 training.\xa0\xa0.I knew when to use 988.',
           'How much do you agree or disagree with the following aspects of the training?.The presenter(s) engaged the participants.',
           'How much do you agree or disagree with the following aspects of the training?.The presenter(s) knew the subject matter well.\xa0',
           'How much do you agree or disagree with the following aspects of the training?.The training was well organized.',
           'How likely are you to recommend the Understanding 988 training to a friend or colleague?',
           'How likely are you to call 988 if you or someone else is experiencing a mental health challenge or crisis?',
           'What was the most valuable thing you learned during the training?\xa0',
           'How can we improve this training in the future?']
H = dict(zip(["id", "start", "end", "lang", "role", "tdate", "county", "post_identify", "post_describe", "post_when",
              "pre_identify", "pre_describe", "pre_when", "sat_engaged", "sat_knowledge", "sat_organized",
              "recommend", "likely_call", "valuable", "improve"], HEADERS))


def row(i, **over):
    """A plain, valid response; keyword arguments override fields by short name."""
    r = {"id": i, "start": f"2026-05-01 10:{i % 60:02d}:00", "end": f"2026-05-01 10:{i % 60:02d}:55",
         "lang": "English (United States)‎", "role": "Student", "tdate": "2026-04-28", "county": "Anne Arundel",
         "post_identify": "Agree", "post_describe": "Strongly agree", "post_when": "Agree",
         "pre_identify": "Neither agree nor disagree", "pre_describe": "Disagree", "pre_when": "Agree",
         "sat_engaged": "Agree", "sat_knowledge": "Strongly agree\xa0", "sat_organized": "Agree",
         "recommend": 8, "likely_call": 9, "valuable": f"comment {i}", "improve": None}
    r.update(over)
    return {H[k]: v for k, v in r.items()}


CASES = [
    row(1, improve="None"),                                   # literal word, not a blank
    row(2, improve="NA"),
    row(3, recommend=8.5),                                    # decimal rating
    row(4, recommend=11),                                     # out of range
    row(5, recommend="ten"),                                  # non-numeric
    row(6, improve="alice example was really helpful"),       # lowercase listed name
    row(7, improve="ALICE EXAMPLE explained it well"),        # uppercase listed name
    row(8, sat_engaged="Very agree"),                         # ambiguous label
    row(9, sat_organized="   "),                              # whitespace-only rating = missing
    row(10, post_identify="Somewhat agree"),                  # unseen label
    row(11, county="Howard County"),                          # unseen county (in Maryland)
    row(12, post_identify=None, post_describe="Strongly disagree", post_when="Strongly disagree", recommend=9),  # 2 of 3 SD + a blank
    row(13, improve="   "),                                   # whitespace-only comment = blank
    row(14, role="Teacher"),                                  # unseen role
    row(15, likely_call=None),
    row(16, county="Prince George’s"),                        # curly apostrophe
    row(17, tdate="last Tuesday"),                            # unreadable training date
    row(18, tdate=None),                                      # blank training date
    row(19, county="   "),                                    # whitespace-only county
    row(20, start="not a time"),                              # unreadable timestamp
    row(21, role="Student;Volunteer"),                        # one recognized + one unrecognized role
    row(22, id=1.5),                                          # non-whole ID
] + [row(i) for i in range(23, 41)]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def read_clean(out):
    """Read the cleaned CSV the way the README says to, so literal answers like 'None' stay text."""
    return pd.read_csv(out / "clean_responses.csv", low_memory=False, keep_default_na=False, na_values=[""])


def run(args):
    return subprocess.run([sys.executable, str(ROOT / "clean.py"), *args], capture_output=True, text=True)


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        raw = pd.DataFrame(CASES, columns=HEADERS)
        xlsx, csv = tmp / "edge.xlsx", tmp / "edge.csv"
        raw.to_excel(xlsx, index=False)
        raw[HEADERS[::-1]].to_csv(csv, index=False)            # reversed column order, CSV format

        cfg = json.loads((ROOT / "cleaning_config.json").read_text())
        cfg["redaction"]["names"] = ["Alice Example"]
        cfg["redaction"]["names_file"] = "does-not-exist.json"
        (tmp / "cfg.json").write_text(json.dumps(cfg))
        resolved = json.loads(json.dumps(cfg))
        resolved["likert_map"]["very agree"] = 5                # EveryMind "confirms" Very agree = 5
        (tmp / "cfg_resolved.json").write_text(json.dumps(resolved))

        p = run([str(xlsx), "--config", str(tmp / "cfg.json"), "--out", str(tmp / "out")])
        check("pipeline runs without crashing on edge cases", p.returncode == 0, p.stderr[-400:])
        if p.returncode != 0:
            return finish()
        d = read_clean(tmp / "out").set_index("response_id")
        report = (tmp / "out" / "data_quality_report.md").read_text()
        log = pd.read_csv(tmp / "out" / "change_log.csv")

        check("row count preserved", len(d) == len(CASES))
        # Import preservation
        check("literal 'None' comment survives import", d.loc[1, "text_improve"] == "None" and d.loc[1, "text_improve_original"] == "None")
        check("literal 'None' typed as 'nothing', not 'blank'", d.loc[1, "text_improve_type"] == "nothing")
        check("literal 'NA' comment survives import", d.loc[2, "text_improve"] == "NA")
        check("whitespace-only comment is blank", pd.isna(d.loc[13, "text_improve"]) and d.loc[13, "text_improve_type"] == "blank")
        # 0-10 answers
        for i, label in [(3, "decimal 8.5"), (4, "out-of-range 11"), (5, "non-numeric 'ten'")]:
            check(f"recommend {label}: blank, flagged, original kept",
                  pd.isna(d.loc[i, "recommend_0_10"]) and bool(d.loc[i, "flag_unmapped_value"])
                  and "recommend_0_10" in str(d.loc[i, "unmapped_fields"]) and pd.notna(d.loc[i, "recommend_0_10_original"]))
        check("valid recommend values untouched", (d.loc[16:40, "recommend_0_10"] == 8).all())
        check("curly apostrophe county recognized", d.loc[16, "county"] == "Prince George's" and not d.loc[16, "flag_unmapped_value"])
        check("no repeated-ID flags when IDs are unique", not d["flag_repeated_id"].any())
        check("unreadable training date: blank, flagged unmapped, not called 'missing'",
              pd.isna(d.loc[17, "training_date"]) and "training_date" in str(d.loc[17, "unmapped_fields"]) and not d.loc[17, "flag_training_date_missing"])
        check("blank training date: flagged missing, not unmapped", d.loc[18, "flag_training_date_missing"] and not d.loc[18, "flag_unmapped_value"])
        check("whitespace-only county = missing, not unrecognized", pd.isna(d.loc[19, "county"]) and not d.loc[19, "flag_unmapped_value"])
        check("unreadable timestamp flagged; month blank, not 'NaT'",
              "start_time" in str(d.loc[20, "unmapped_fields"]) and pd.isna(d.loc[20, "survey_month"]))
        check("no 'NaT' text anywhere in output", not d.astype(str).isin(["NaT"]).any().any())
        r21 = d[d["source_row"] == 22].iloc[0]                    # source_row = spreadsheet row (row 1 is headers)
        check("partly unrecognized roles kept apart from single-role students",
              r21["role_group"] == "Includes unrecognized role" and bool(r21["is_student"]) and r21["roles_unrecognized"] == "Volunteer"
              and "UNRECOGNIZED:Volunteer" in r21["roles_selected"] and bool(r21["flag_unmapped_value"]))
        r22 = d[d["source_row"] == 23].iloc[0]
        check("non-whole ID: blank, original kept, flagged, row still identifiable",
              pd.isna(r22.name) and str(r22["response_id_original"]) == "1.5" and "response_id" in str(r22["unmapped_fields"]))
        check("report doesn't claim questions were or weren't asked",
              not any(p in report for p in ["Only asked in", "not asked", "asked (≥80%)"]))
        check("blank likely-call stays blank and is not flagged", pd.isna(d.loc[15, "likely_call_0_10"]) and not d.loc[15, "flag_unmapped_value"])
        # Redaction
        check("lowercase listed name redacted", "alice" not in d.loc[6, "text_improve_redacted"].lower() and "[NAME]" in d.loc[6, "text_improve_redacted"])
        check("uppercase listed name redacted", "alice" not in d.loc[7, "text_improve_redacted"].lower())
        check("original comment keeps the name (internal column)", "alice example" in d.loc[6, "text_improve"])
        # Ratings
        check("ambiguous label unscored and flagged", pd.isna(d.loc[8, "sat_engaged"]) and bool(d.loc[8, "flag_ambiguous_label"]))
        check("ambiguous row keeps its other answers", d.loc[8, "sat_knowledge"] == 5 and d.loc[8, "post_describe"] == 5)
        check("whitespace-only rating = missing, not unmapped", pd.isna(d.loc[9, "sat_organized"]) and not d.loc[9, "flag_unmapped_value"])
        check("unseen rating label blank and flagged", pd.isna(d.loc[10, "post_identify"]) and "post_identify" in str(d.loc[10, "unmapped_fields"]))
        check("unseen county kept visible, flagged, not called non-Maryland",
              str(d.loc[11, "county"]).startswith("UNRECOGNIZED:") and bool(d.loc[11, "flag_unmapped_value"]) and not d.loc[11, "flag_county_outside_md"])
        check("unseen role flagged", "role_group" in str(d.loc[14, "unmapped_fields"]))
        check("2 Strongly disagree + 1 blank is not 'all three Strongly disagree'",
              not d.loc[12, "pattern_all_post_strongly_disagree"] and not d.loc[12, "flag_possible_scale_reversal"])
        check("report lists every unmapped value", all(s in report for s in ["8.5", "'ten'", "Somewhat agree", "Howard County", "Teacher"]))
        check("redaction described as preliminary in report", "preliminary" in report.lower())

        # Resolving an ambiguous label in config updates score, flag, log and report together
        p2 = run([str(xlsx), "--config", str(tmp / "cfg_resolved.json"), "--out", str(tmp / "out_resolved")])
        check("pipeline runs with resolved label", p2.returncode == 0, p2.stderr[-400:])
        if p2.returncode == 0:
            d2 = read_clean(tmp / "out_resolved").set_index("response_id")
            log2 = pd.read_csv(tmp / "out_resolved" / "change_log.csv")
            rep2 = (tmp / "out_resolved" / "data_quality_report.md").read_text()
            check("resolved label is scored", d2.loc[8, "sat_engaged"] == 5)
            check("resolved label no longer flagged ambiguous", not d2.loc[8, "flag_ambiguous_label"])
            check("resolved label not logged as unscored", not ((log2.raw_value == "'Very agree'") & log2.cleaned_value.str.contains("unscored")).any())
            check("report says the label is now scored", "is now scored 5 per config" in rep2)
            v2 = subprocess.run([sys.executable, str(ROOT / "validate_clean.py"), str(xlsx), "--out", str(tmp / "out_resolved"),
                                 "--config", str(tmp / "cfg_resolved.json")], capture_output=True, text=True)
            check("validator passes after a label is confirmed in config", v2.returncode == 0,
                  "; ".join(l for l in v2.stdout.splitlines() if l.startswith("FAIL"))[:400] or v2.stderr[-300:])

        # Same data as CSV with reversed columns gives identical cleaned values
        p3 = run([str(csv), "--config", str(tmp / "cfg.json"), "--out", str(tmp / "out_csv")])
        check("pipeline runs on reordered CSV", p3.returncode == 0, p3.stderr[-400:])
        if p3.returncode == 0:
            d3 = read_clean(tmp / "out_csv").set_index("response_id")
            cols = ["post_identify", "sat_engaged", "recommend_0_10", "county", "role_group", "text_improve", "text_improve_type", "flag_unmapped_value"]
            same = d[cols].astype(str).equals(d3[cols].astype(str))
            check("CSV with reversed columns matches Excel result", same)

        # Questions nobody answered: pipeline and validator must both cope with zero valid answers
        for name, field in [("no recommend answers", "recommend"), ("no likely-call answers", "likely_call")]:
            empty = pd.DataFrame([row(i, **{field: None}) for i in range(1, 31)], columns=HEADERS)
            ex = tmp / f"empty_{field}.xlsx"
            empty.to_excel(ex, index=False)
            pe = run([str(ex), "--config", str(tmp / "cfg.json"), "--out", str(tmp / f"out_{field}")])
            ve = subprocess.run([sys.executable, str(ROOT / "validate_clean.py"), str(ex), "--out", str(tmp / f"out_{field}"),
                                 "--config", str(tmp / "cfg.json")], capture_output=True, text=True) if pe.returncode == 0 else None
            check(f"{name}: pipeline and validator both pass", pe.returncode == 0 and ve is not None and ve.returncode == 0,
                  (pe.stderr[-200:] if pe.returncode else "; ".join(l for l in ve.stdout.splitlines() if l.startswith("FAIL"))[:300] or ve.stderr[-300:]))
            if pe.returncode == 0:
                rep = (tmp / f"out_{field}" / "data_quality_report.md").read_text()
                check(f"{name}: report shows n/a, not nan", not re.search(r"\bnan\b", rep, re.IGNORECASE), "")

        # Missing and repeated IDs: only true repeats are flagged
        ids = pd.DataFrame([row(1), row(1), row(2, id=None), row(3, id=None)], columns=HEADERS)
        ids_x = tmp / "ids.xlsx"
        ids.to_excel(ids_x, index=False)
        p4 = run([str(ids_x), "--config", str(tmp / "cfg.json"), "--out", str(tmp / "out_ids")])
        check("pipeline runs with missing and repeated IDs", p4.returncode == 0, p4.stderr[-300:])
        if p4.returncode == 0:
            d4 = pd.read_csv(tmp / "out_ids" / "clean_responses.csv", keep_default_na=False, na_values=[""])
            check("repeated ID flagged, missing IDs not", d4["flag_repeated_id"].astype(str).tolist() == ["True", "True", "False", "False"])
            v4 = subprocess.run([sys.executable, str(ROOT / "validate_clean.py"), str(ids_x), "--out", str(tmp / "out_ids"),
                                 "--config", str(tmp / "cfg.json")], capture_output=True, text=True)
            check("validator passes with missing and repeated IDs", v4.returncode == 0,
                  "; ".join(l for l in v4.stdout.splitlines() if l.startswith("FAIL"))[:400] or v4.stderr[-300:])

        # Independent validator also passes on the synthetic export
        v = subprocess.run([sys.executable, str(ROOT / "validate_clean.py"), str(xlsx), "--out", str(tmp / "out"), "--config", str(tmp / "cfg.json")],
                           capture_output=True, text=True)
        fails = [l for l in v.stdout.splitlines() if l.startswith("FAIL")]
        check("validate_clean.py passes on the synthetic export", v.returncode == 0, "; ".join(fails)[:600] or v.stderr[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return finish()


def finish():
    width = max(len(n) for n, _, _ in results)
    fails = 0
    for name, ok, detail in results:
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else detail}")
    print(f"\n{len(results) - fails}/{len(results)} edge-case tests passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
