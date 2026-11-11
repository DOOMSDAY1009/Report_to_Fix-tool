#!/usr/bin/env python3
"""Builds console/index.html: inlines results + the affected questions into console/template.html (static, no backend)."""
import json, os, collections, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import triage as T
ROOT = T.ROOT
arg = sys.argv[1] if len(sys.argv) > 1 else None
path = arg or next(p for p in (os.path.join(ROOT, "results.json"), os.path.join(ROOT, "results.recorded.json")) if os.path.exists(p))
res = json.load(open(path, encoding="utf-8"))
mp = os.path.join(ROOT, "results_meta.json")
meta = json.load(open(mp)) if (os.path.basename(path) == "results.json" and os.path.exists(mp)) else {"mode": "offline"}
print("using", path)
SEV = {"answer_key": 100, "unsound": 100, "wrong_question": 100, "solution_fix": 95, "stem_data": 80, "option_repair": 80, "diagram": 80,
       "identical_options": 75, "type_mismatch": 80, "solution_missing": 70, "wrong_chapter": 40, "syllabus": 35, "answer_form": 40, "notation": 40,
       "spelling": 20, "noise": 20, "exam_reference": 20, "markup_leak": 25}
by = collections.defaultdict(list)
for r in res: by[r["questionId"]].append(r)
groups = []
for qid, rs in by.items():
    q = T.QS[qid]; t = q["questionType"]; d = q[t]
    own = next((r for r in rs if r["verdict"] == "defect" and not r.get("duplicate_of")), rs[0])
    for r in rs:  # attach raw student comment
        rep = next(x for x in T.REPORTS if x["reportId"] == r["reportId"]); r["comment"], r["source"] = rep["comment"], rep["source"]
    routes = [r["route"] for r in rs]
    route = "faculty_review" if "faculty_review" in routes else "auto_apply" if "auto_apply" in routes else "no_change"
    students = len({next(x for x in T.REPORTS if x["reportId"] == r["reportId"])["studentId"] for r in rs})
    pr = SEV.get(own["repair"], 0) * (1 + 0.5 * (students - 1)) if own["verdict"] == "defect" else 0
    groups.append({"id": qid, "subject": q["subject"], "chapter": q["chapter"], "verdict": own["verdict"], "repair": own["repair"],
        "route": route, "conf": own["confidence"], "evidence": own["evidence"], "reply": own["student_reply"], "priority": pr,
        "edits": [{"field": e["field"], "find": e["find"], "replace": e["replace"]} for e in own.get("edits", [own["edit"]] if own["edit"] else [])],
        "guard": own.get("guard_notes", []), "side": sum([r.get("side_findings", []) for r in rs], []), "flags": any("flags" in r for r in rs),
        "q": {"type": t, "question": d["question"], "solution": d["solution"], "answer": d["answer"]},
        "reports": [{k: r.get(k) for k in ("reportId", "comment", "source", "flags")} for r in rs]})
out = open(os.path.join(ROOT, "console", "template.html"), encoding="utf-8").read().replace("/*DATA*/", json.dumps({"groups": groups, "meta": meta}, ensure_ascii=False).replace("</", "<\\/"))
open(os.path.join(ROOT, "console", "index.html"), "w", encoding="utf-8").write(out)
print(len(groups), "groups,", len(out) // 1024, "KB")
