"""Wiring test, no API key needed. A fake LLM replays the recorded judgements through the REAL live code path
(prompt payloads, JSON parsing, blind-solve check, guards, routing, dedup). Run: python tests/mock_test.py"""
import json, os, sys, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.join(ROOT, "engine"))
import triage as T
from offline import J

STEM2Q = {T.plain_view(q[q["questionType"]]["question"]): q for q in T.QS.values()}

class FakeLLM:
    stats = {"calls": 0, "cache_hits": 0, "retries": 0, "in": 0, "out": 0}
    def used_models(self): return {"solve": "fake", "judge": "fake"}
    def invalidate(self, *a): pass
    def call(self, system, user, max_tokens=0, tag=""):
        if tag == "solve":
            q = STEM2Q[user]; t = q["questionType"]; ans = q[t]["answer"]
            for rid, r in ((r["reportId"], r) for r in T.REPORTS if r["questionId"] == q["_id"]):
                j = T.judge_offline(rid)
                if j.get("repair") in ("unsound", "diagram", "identical_options"): return "cannot solve\nFINAL: UNSOLVABLE", "end_turn"
                for e in j.get("edits", []):
                    if e["field"] == "answer": ans = json.loads(e["replace"]) if e["replace"].startswith("[") else e["replace"]
            return "working...\nFINAL: " + (", ".join(ans) if isinstance(ans, list) else str(ans)), "end_turn"
        p = json.loads(user); out = {}
        for r in p["reports"]:
            j = T.judge_offline(r["reportId"])
            out[r["reportId"]] = {"verdict": j["verdict"], "repair": j.get("repair"), "edits": j.get("edits", []), "conf": j["conf"],
                                  "signals": j.get("signals", 0), "evidence": j["evidence"], "reply": j["reply"], "flags": j.get("flags", []), "side": j.get("side", [])}
        return "Here you go:\n```json\n" + json.dumps(out) + "\n```", "end_turn"

res, meta = T.run("live", workers=4, llm=FakeLLM())
rec = {x["reportId"]: x for x in json.load(open(os.path.join(ROOT, "results.recorded.json"), encoding="utf-8"))}
assert len(res) == 44, len(res)
diff = [(x["reportId"], rec[x["reportId"]]["route"], x["route"], x.get("guard_notes")) for x in res if x["route"] != rec[x["reportId"]]["route"] or x["verdict"] != rec[x["reportId"]]["verdict"]]
print("reports:", len(res), "| differences vs recorded run:", len(diff))
for d in diff: print("  ", d)
fb = [x["reportId"] for x in res if any("unusable" in n for n in x.get("guard_notes", []))]
assert not fb, f"parser fell back for {fb}"
assert all(x["route"] in ("auto_apply", "faculty_review", "no_change") for x in res)
r027 = next(x for x in res if x["reportId"] == "R027"); assert r027["route"] == "no_change" and "prompt_injection" in r027["flags"]
r010 = next(x for x in res if x["reportId"] == "R010"); assert r010["edit"] is None, "invented option must not be proposed"
for rid, want in (("R025", "missing_figure"), ("R021", "answer_format"), ("R013", "exam_tag")):
    r = next(r for r in T.REPORTS if r["reportId"] == rid); q = T.QS[r["questionId"]]
    assert want in T.auto_facts(q), (rid, T.auto_facts(q))
    v, rp, ed, route, notes = T.guard_and_route(q, {"verdict": "display_issue", "repair": None, "edits": [], "conf": 0.9, "signals": 0, "evidence": "x", "reply": "y"}, {"flags": [], "fixed_since": []})
    assert route == "faculty_review" and notes, (rid, route)      # a silent dismissal is impossible
print("OK: parser, guards, injection handling and no-invented-option rule all behave")
