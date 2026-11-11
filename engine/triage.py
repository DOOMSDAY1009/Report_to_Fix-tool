#!/usr/bin/env python3
"""Report -> Fix triage engine.

  python engine/triage.py --dry-run          # no API calls: size estimate
  python engine/triage.py --limit 3          # live run on the first 3 questions (cheap smoke test)
  python engine/triage.py                    # live run on everything -> results.json
  python engine/triage.py --offline          # recorded judgements (no API) -> results.recorded.json

Pipeline:  A. group by question + pre-checks  ->  B. LLM: key-blind solve, then one judge call per question
           ->  C. deterministic guards + routing (code, not the model, decides auto_apply vs faculty_review).
"""
import json, re, sys, os, copy, math, argparse, datetime, collections
from concurrent.futures import ThreadPoolExecutor, as_completed
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from prompts import SOLVER_SYSTEM, render_judge_system
import html as _html, xml.etree.ElementTree as ET
from llm import LLM, AuthError, DailyLimit, RequestTooLarge, load_api_key, DEFAULT_CHAINS

D = lambda f: json.load(open(os.path.join(ROOT, "data", f), encoding="utf-8"))
QS = {q["_id"]: q for q in D("questions.json")}
REPORTS, TAX = D("reports.json"), D("taxonomy.json")
REPAIRS = TAX["repairs"]
JUDGE_SYSTEM = render_judge_system(TAX)
INJECTION = re.compile(r"ignore (all )?(previous|prior)|disregard (all )?(previous|prior)|you are (now )?(the )?admin|system prompt|mark this report|change the answer to|override", re.I)
WHOLE = "@@WHOLE@@"
ADDITIVE = {"stem_data", "option_repair", "answer_form"}      # these repairs may only ADD text, never remove digits

# ---------- edit application ----------
def get_field(q, f):
    if f == "questionType": return q["questionType"]
    return q[q["questionType"]].get(f)
def set_field(q, f, v):
    if f == "questionType": q[v] = q.pop(q["questionType"]); q["questionType"] = v   # payload key follows the type
    else: q[q["questionType"]][f] = v
def _s(cur): return cur if isinstance(cur, str) else json.dumps(cur)
def resolve(q, e):
    cur = get_field(q, e.get("field"))
    return {**e, "find": _s(cur) if (e["find"] == WHOLE and cur is not None) else e["find"]}
def apply_edit(q, e):
    """'find' must match exactly once (answer/questionType: the whole current value). Raises ValueError otherwise."""
    q = copy.deepcopy(q); f = e["field"]; cur = get_field(q, f)
    if cur is None: raise ValueError(f"field {f!r} does not exist")
    s = _s(cur)
    if f in ("answer", "questionType"):
        if re.sub(r"\s", "", s) != re.sub(r"\s", "", e["find"]): raise ValueError(f"{f} is {s!r}, not {e['find']!r}")
        new = json.loads(e["replace"]) if e["replace"].lstrip().startswith("[") else e["replace"]
    else:
        n = s.count(e["find"]) if e["find"] else 0
        if n != 1: raise ValueError(f"find string occurs {n} times in {f}")
        new = s.replace(e["find"], e["replace"])
    set_field(q, f, new); return q
text = lambda h: re.sub(r"<[^>]+>", "", h)
digits = lambda h: collections.Counter(re.findall(r"\d", text(h)))

# ---------- answer comparison (used for the deterministic blind-solve check) ----------
def parse_final(t):
    m = re.findall(r"FINAL\s*:\s*(.+)", t or "", re.I)
    return m[-1].strip().strip("*`. ") if m else None
def norm_answer(v, qtype):
    if v is None: return None
    s = " ".join(map(str, v)) if isinstance(v, list) else str(v)
    if "UNSOLVABLE" in s.upper(): return None
    if qtype in ("scq", "mcq"):
        L = set(re.findall(r"\b([A-D])\b", s)); return frozenset(L) if L else None
    m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", s.replace(",", "")); return float(m.group()) if m else None
def answers_agree(a, b, qtype):
    x, y = norm_answer(a, qtype), norm_answer(b, qtype)
    if x is None or y is None: return None
    return math.isclose(x, y, rel_tol=0.01, abs_tol=1e-9) if isinstance(x, float) else x == y
def parse_replace(r): return json.loads(r) if r.lstrip().startswith("[") else r

# ---------- Stage A ----------
FIG = re.compile(r"\b(figure|fig\.|diagram|graph|shown below|as shown|circuit shown|given below)\b", re.I)
EXAM_TAG = re.compile(r"\(\s*(?:JEE|NEET|AIPMT|AIEEE|IIT|BITSAT|CBSE|AIIMS)[^)]{0,40}\d{4}[^)]{0,20}\)", re.I)
def auto_facts(q):
    """Facts computed by code, not by the model. Each one is a strong sign of a real defect."""
    t = q["questionType"]; d = q[t]; a = d["answer"]; f = []
    if t == "mcq" and (not isinstance(a, list) or len(a) < 2 or any((not str(x).strip()) or "," in str(x) for x in a)): f.append("answer_format")
    if t == "scq" and (isinstance(a, list) or not re.fullmatch(r"[A-D]", str(a).strip())): f.append("answer_format")
    pv = plain_view(d["question"])
    if FIG.search(pv) and not d.get("quesImages"): f.append("missing_figure")
    if EXAM_TAG.search(pv): f.append("exam_tag")
    raw_q = re.sub(r"<math\b.*?</math>", "", d["question"], flags=re.S)
    if re.search(r"\\[A-Za-z]{2,}", re.sub(r"<[^>]+>", " ", raw_q)): f.append("markup_leak")        # raw LaTeX outside MathML
    if plain_view(d.get("solution") or "").lstrip().startswith("="): f.append("solution_fragment")     # solution opens mid-equation: truncated or from another question
    return f

def stage_a(r, q):
    return {"flags": ["prompt_injection"] if INJECTION.search(r["comment"]) else [],
            "fixed_since": [h for h in q["errorHistory"] if h["at"] > r["reportedAt"]]}

# ---------- Stage B (live) ----------
def extract_json(t):
    t = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", t.strip())
    try: return json.loads(t)
    except ValueError: pass
    i = t.find("{")
    if i < 0: raise ValueError("no JSON object in model output")
    depth, ins, esc = 0, False, False
    for k in range(i, len(t)):
        c = t[k]
        if ins:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc: ins = False
        elif c == '"': ins = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: return json.loads(t[i:k + 1])
    raise ValueError("unbalanced JSON in model output")

_XML_ENT = {"lt", "gt", "amp", "quot", "apos"}
def _math_text(el):
    tag = el.tag.split("}")[-1]; kids = [_math_text(c) for c in el]; own = (el.text or "").strip()
    if tag in ("mi", "mn", "mo", "mtext", "ms"): return own
    j = "".join
    if tag == "mfrac" and len(kids) == 2: return f"({kids[0]})/({kids[1]})"
    if tag == "msup" and len(kids) == 2: return f"{kids[0]}^({kids[1]})"
    if tag == "msub" and len(kids) == 2: return f"{kids[0]}_({kids[1]})"
    if tag == "msubsup" and len(kids) == 3: return f"{kids[0]}_({kids[1]})^({kids[2]})"
    if tag == "msqrt": return f"sqrt({j(kids)})"
    if tag == "mroot" and len(kids) == 2: return f"root({kids[1]})({kids[0]})"
    if tag in ("mover", "munder") and len(kids) == 2: return f"{kids[0]}{kids[1]}"
    if tag == "munderover" and len(kids) == 3: return f"{kids[0]}_({kids[1]})^({kids[2]})"
    if tag == "mfenced": return "(" + ",".join(kids) + ")"
    if tag == "mtr": return " ".join(kids)
    if tag == "mtable": return "; ".join(kids)
    return j(kids)
def _one_math(m):
    frag = re.sub(r"&([A-Za-z]+);", lambda x: x.group(0) if x.group(1) in _XML_ENT else _html.unescape(x.group(0)), m.group(0))
    try: return " " + re.sub(r"\s+", " ", _math_text(ET.fromstring(frag))).strip() + " "
    except ET.ParseError: return " " + re.sub(r"<[^>]+>", " ", frag) + " "
def plain_view(h):
    """Readable text of a question: MathML -> linear text, tags removed. Used for the key-blind solver (smaller and easier to read)."""
    h = re.sub(r"<math\b.*?</math>", _one_math, h, flags=re.S)
    h = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>|</div>", "\n", h, flags=re.I)
    h = _html.unescape(re.sub(r"<[^>]+>", " ", h))
    return "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in h.split("\n") if ln.strip())

def solve_blind(llm, q):
    t = q["questionType"]; d = q[t]
    txt, _ = llm.call(SOLVER_SYSTEM, plain_view(d["question"]), 2500, "solve")
    final = parse_final(txt)
    return {"text": txt, "final": final, "agrees": answers_agree(final, d["answer"], t) if final else None}

def normalize_judgement(j):
    if not isinstance(j, dict): return None
    edits = j.get("edits") or ([j["edit"]] if isinstance(j.get("edit"), dict) else [])
    edits = [{"field": str(e.get("field")), "find": str(e.get("find", "")), "replace": str(e.get("replace", ""))} for e in edits if isinstance(e, dict)]
    if not isinstance(j.get("verdict"), str) or not j.get("reply") or not j.get("evidence"): return None
    try: conf = max(0.0, min(1.0, float(j.get("conf", j.get("confidence", 0)))))
    except (TypeError, ValueError): conf = 0.0
    try: signals = int(j.get("signals", 0))
    except (TypeError, ValueError): signals = 0
    return {"verdict": j["verdict"], "repair": j.get("repair") or None, "edits": edits, "conf": conf, "signals": signals,
            "evidence": str(j["evidence"]), "reply": str(j["reply"]), "flags": list(j.get("flags") or []), "side": list(j.get("side") or [])}

def fallback(reason):
    return {"verdict": "not_actionable", "repair": None, "edits": [], "conf": 0.0, "signals": 0, "fallback": True, "flags": [], "side": [],
            "evidence": f"Automatic triage failed for this report ({reason}); routed to a human.",
            "reply": "Thanks for reporting this. A faculty member will take a look at the question."}

def judge_live(llm, q, reps, pres, blind):
    t = q["questionType"]; d = q[t]
    payload = {"subject": q["subject"], "chapter": q["chapter"], "questionType": t, "answer_find_text": _s(d["answer"]),
               "question_html": d["question"], "solution_html": d["solution"], "has_question_images": bool(d.get("quesImages")),
               "errorHistory": q["errorHistory"], "automatic_checks": auto_facts(q),
               "blind_solve": {"final": blind["final"], "agrees_with_stored_key": blind["agrees"], "working": (blind["text"] or "")[-900:]},
               "reports": [{"reportId": r["reportId"], "comment": r["comment"], "source": r["source"], "reportedAt": r["reportedAt"],
                            "precheck_flags": pres[r["reportId"]]["flags"], "record_changed_after_report": pres[r["reportId"]]["fixed_since"]} for r in reps]}
    user = json.dumps(payload, ensure_ascii=False); max_tokens = 6000; obj = None
    for attempt in range(2):
        txt, stop = llm.call(JUDGE_SYSTEM, user, max_tokens, "judge")
        try:
            if stop == "max_tokens": raise ValueError("judge output truncated")
            obj = extract_json(txt); break
        except ValueError as e:
            llm.invalidate(JUDGE_SYSTEM, user, max_tokens, "judge")          # never cache a bad output
            if attempt == 1: return {r["reportId"]: fallback(str(e)) for r in reps}
            max_tokens *= 2
    return {r["reportId"]: (normalize_judgement(obj.get(r["reportId"])) or fallback("missing or invalid entry")) for r in reps}

def judge_offline(rid):
    from offline import J
    j = dict(J[rid]); src = J[j["dup"]] if "dup" in j else j
    j.setdefault("edits", src.get("edits", [])); return j

# ---------- Stage C: guards + routing (deterministic; the LLM cannot bypass these) ----------
def guard_and_route(q, j, pre, blind=None):
    notes, route = [], "no_change"
    verdict, repair = j["verdict"], j.get("repair")
    if verdict not in TAX["verdicts"]: return "not_actionable", None, [], "faculty_review", ["invalid verdict key -> human"]
    try: edits = [resolve(q, e) for e in j.get("edits", [])]
    except (KeyError, TypeError): edits = []; notes.append("malformed edit discarded")
    if verdict == "defect":
        if repair not in REPAIRS:
            return "defect", ("unsound" if "unsound" in REPAIRS else None), edits, "faculty_review", ["invalid repair key -> human"]
        spec = REPAIRS[repair]; route = "auto_apply" if spec["auto"] else "faculty_review"
        if repair == "option_repair" and j["conf"] < 0.9 and edits:
            edits = []; notes.append("lost option is not recoverable from the record: no edit proposed (never invent options)")
        work = copy.deepcopy(q)
        for e in edits:
            if e["field"] not in spec["fields"]:
                route = "faculty_review"; notes.append(f"scope guard: {repair} may not touch {e['field']}"); continue
            try: work = apply_edit(work, e)
            except (ValueError, KeyError) as ex: route = "faculty_review"; notes.append(f"edit guard: {ex}"); continue
            if repair == "spelling" and (re.findall(r"\d", text(e["find"])) != re.findall(r"\d", text(e["replace"])) or "<math" in e["find"]):
                route = "faculty_review"; notes.append("spelling guard: digits/math changed")
            if repair in ADDITIVE and (digits(e["find"]) - digits(e["replace"])):
                route = "faculty_review"; notes.append(f"{repair} must be additive: a number was removed or changed")
        if spec["auto"] and not edits: route = "faculty_review"; notes.append("auto repair but no usable edit")
        if repair == "answer_key" and (j["conf"] < 0.9 or j["signals"] < 2): route = "faculty_review"; notes.append("answer_key needs >=2 independent confirmations")
        if repair == "stem_data" and j["conf"] < 0.9: route = "faculty_review"; notes.append("stem_data: value not certain from the solution")
        ae = next((e for e in edits if e["field"] == "answer"), None)
        if ae and blind is not None and route == "auto_apply":     # new key must match the key-blind solve
            ok = answers_agree(blind["final"], parse_replace(ae["replace"]), work["questionType"]) if blind["final"] else None
            if ok is not True: route = "faculty_review"; notes.append("new key not confirmed by the independent key-blind solve")
        if route == "auto_apply" and j["conf"] < 0.8: route = "faculty_review"; notes.append("low confidence")
        if route == "auto_apply" and pre["fixed_since"]: route = "faculty_review"; notes.append("record was edited after this report: verify it is not already fixed")
    facts = auto_facts(q)
    if facts and verdict != "defect":      # safety net: a verifiable problem must never be closed silently
        route = "faculty_review"; notes.append(f"automatic checks found {facts} but the verdict is {verdict}: human check")
    if verdict == "no_error" and blind is not None and blind["agrees"] is False:
        route = "faculty_review"; notes.append("verdict no_error, but the key-blind solve disagrees with the stored key: human check")
    if "prompt_injection" in pre["flags"]:
        if route == "auto_apply": route = "faculty_review"
        notes.append("comment tried to instruct the system; ignored")
    if j.get("fallback"): route = "faculty_review"; notes.append("LLM output unusable for this report")
    return verdict, repair, edits, route, notes

# ---------- orchestration ----------
def run(mode="live", limit=None, workers=1, llm=None):
    groups = collections.defaultdict(list)
    for r in sorted(REPORTS, key=lambda x: x["reportId"]): groups[r["questionId"]].append(r)
    qids = sorted(groups, key=lambda k: groups[k][0]["reportId"])
    if limit: qids = qids[:limit]
    blinds, judged, errors = {}, {}, []
    if mode == "live":
        def work(qid):
            reps, q = groups[qid], QS[qid]; pres = {r["reportId"]: stage_a(r, q) for r in reps}
            b = solve_blind(llm, q); return qid, b, judge_live(llm, q, reps, pres, b)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, qid): qid for qid in qids}
            for i, f in enumerate(as_completed(futs), 1):
                qid = futs[f]
                try: _, b, jr = f.result(); blinds[qid] = b; judged.update(jr); print(f"  [{i}/{len(qids)}] {qid[-6:]} ok")
                except (AuthError, DailyLimit): ex.shutdown(wait=False, cancel_futures=True); raise
                except Exception as e:
                    errors.append((qid, str(e))); print(f"  [{i}/{len(qids)}] {qid[-6:]} FAILED: {e}")
                    for r in groups[qid]: judged[r["reportId"]] = fallback("API error: " + str(e)[:120])
    results, owner = [], {}
    for r in sorted((r for q in qids for r in groups[q]), key=lambda x: x["reportId"]):
        q = QS[r["questionId"]]; pre = stage_a(r, q)
        j = judged[r["reportId"]] if mode == "live" else judge_offline(r["reportId"])
        b = blinds.get(r["questionId"])
        verdict, repair, edits, route, notes = guard_and_route(q, j, pre, b)
        res = {"reportId": r["reportId"], "questionId": r["questionId"], "verdict": verdict, "repair": repair,
               "edit": edits[0] if edits else None, "route": route, "confidence": j["conf"],
               "evidence": j["evidence"], "student_reply": j["reply"]}
        if len(edits) > 1: res["edits"] = edits
        if verdict == "defect" and edits:                       # grouping: the edit is written once per question
            if r["questionId"] in owner: res["duplicate_of"] = owner[r["questionId"]]
            else: owner[r["questionId"]] = r["reportId"]
        fl = sorted(set(pre["flags"] + j.get("flags", [])))
        if fl: res["flags"] = fl
        if j.get("side"): res["side_findings"] = j["side"]
        if b and b["final"]: res["blind_solve_final"] = b["final"]
        if notes: res["guard_notes"] = notes
        results.append(res)
    meta = {"mode": mode, "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "questions": len(qids), "reports": len(results), "api_errors": len(errors),
            "fallback_reports": sum(1 for r in results if any("LLM output unusable" in n for n in r.get("guard_notes", [])))}
    if llm:
        um = llm.used_models()
        meta.update({"model": f"{um['judge']} (judge) + {um['solve']} (solver)", "provider": "groq", "calls": llm.stats["calls"], "cache_hits": llm.stats["cache_hits"],
                     "retries": llm.stats["retries"], "input_tokens": llm.stats["in"], "output_tokens": llm.stats["out"]})
    return results, meta

def dry_run():
    qs = [QS[k] for k in {r["questionId"] for r in REPORTS}]
    ti = to = 0
    for q in qs:
        d = q[q["questionType"]]
        ti += (len(SOLVER_SYSTEM) + len(plain_view(d["question"]))) / 3.5 + (len(JUDGE_SYSTEM) + len(d["question"]) + len(d["solution"])) / 2.8 + 900
        to += 700 + 900
    print(f"{len(qs)} questions, {len(REPORTS)} reports -> about {2 * len(qs)} API calls")
    print(f"rough size: ~{ti/1e3:.0f}k input + ~{to/1e3:.0f}k output tokens, ~{(ti+to)/1e3:.0f}k in total")
    print("Groq free tier limits tokens per minute AND per day, per model. The first model of each stage is used until its quota runs out,")
    print("then the next one in the chain; progress is cached, so you can simply re-run later to continue.")
    print(f"solver chain: {DEFAULT_CHAINS['solve']}\njudge chain:  {DEFAULT_CHAINS['judge']}")
    print("\n--- judge system prompt: %d chars (~%d tokens) ---" % (len(JUDGE_SYSTEM), len(JUDGE_SYSTEM) / 3))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="use recorded judgements (engine/offline.py), no API")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--limit", type=int, help="only the first N questions")
    ap.add_argument("--workers", type=int, default=1, help="parallel questions (free tier is rate-limited; 1-2 is best)")
    ap.add_argument("--solve-model"); ap.add_argument("--judge-model")
    ap.add_argument("--no-cache", action="store_true"); ap.add_argument("--out"); ap.add_argument("--force", action="store_true", help="overwrite results.json even if some reports failed")
    ap.add_argument("--judge-effort", default="medium", choices=["low", "medium", "high"]); ap.add_argument("--solve-effort", default="low", choices=["low", "medium", "high"])
    a = ap.parse_args()
    if a.dry_run: return dry_run()
    llm = None
    if not a.offline:
        key = load_api_key(ROOT)
        if not key: sys.exit("No API key. Set GROQ_API_KEY (or put it in a .env file), or use --offline. See README.md.")
        chains = {k: list(v) for k, v in DEFAULT_CHAINS.items()}
        for stage, arg, env in (("solve", a.solve_model, "GROQ_SOLVE_MODEL"), ("judge", a.judge_model, "GROQ_JUDGE_MODEL")):
            m = arg or os.environ.get(env)
            if m: chains[stage] = [m]
        llm = LLM(key, chains, None if a.no_cache else os.path.join(ROOT, "engine", ".cache"), efforts={"judge": a.judge_effort, "solve": a.solve_effort})
        print(f"live run on Groq: solver {chains['solve'][0]}, judge {chains['judge'][0]}, {a.workers} worker(s). Rate-limit waits are normal; this can take 10-30 min.")
    try: res, meta = run("offline" if a.offline else "live", a.limit, a.workers, llm)
    except AuthError as e: sys.exit(f"API key rejected: {e}")
    except DailyLimit as e: sys.exit(f"\nDaily free quota used up on every model in the chain:\n  {e}\nNothing is lost: finished calls are cached in engine/.cache. Run the same command again after the quota resets (or pass --judge-model / --solve-model with another model).")
    out = a.out or os.path.join(ROOT, "results.recorded.json" if a.offline else "results.json")
    if not a.offline and not a.force and (meta["api_errors"] or meta["fallback_reports"]):
        out = os.path.join(ROOT, "results.partial.json")        # never overwrite a good results.json with a run that had failures
        print(f"\n!! {meta['api_errors']} questions failed / {meta['fallback_reports']} reports fell back. Writing results.partial.json instead; results.json is untouched. Re-run later (finished calls are cached).")
    json.dump(res, open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    if not a.offline: json.dump(meta, open(os.path.join(ROOT, "results_meta.json"), "w", encoding="utf-8"), indent=2)
    print(f"\nwrote {out}"); print(json.dumps(meta, indent=1))
    print(collections.Counter((x["verdict"], x["route"]) for x in res))
    for x in res:
        if x.get("guard_notes"): print(x["reportId"], x["route"], x["guard_notes"])
    if not a.offline and a.limit: print("\n(partial run: --limit was set, so results.json has only some reports. Re-run without --limit for the full file.)")

if __name__ == "__main__": main()
