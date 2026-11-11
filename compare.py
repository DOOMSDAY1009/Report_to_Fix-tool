#!/usr/bin/env python3
"""Compare a live run against the recorded (offline) judgements.  python compare.py [live.json] [other.json]"""
import json, sys, collections
a_path, b_path = (sys.argv + ["results.json", "results.recorded.json"][len(sys.argv) - 1:])[1:3]
A = {x["reportId"]: x for x in json.load(open(a_path, encoding="utf-8"))}
B = {x["reportId"]: x for x in json.load(open(b_path, encoding="utf-8"))}
ids = sorted(set(A) & set(B)); c = collections.Counter()
for i in ids:
    for k in ("verdict", "repair", "route"): c[k] += A[i].get(k) == B[i].get(k)
print(f"{a_path} vs {b_path}: {len(ids)} reports in common")
for k in ("verdict", "repair", "route"): print(f"  {k:8s} agree {c[k]}/{len(ids)}")
for i in ids:
    d = [(k, B[i].get(k), A[i].get(k)) for k in ("verdict", "repair", "route") if A[i].get(k) != B[i].get(k)]
    if d: print(f"  {i}: " + "; ".join(f"{k}: {b} -> {a}" for k, b, a in d))
