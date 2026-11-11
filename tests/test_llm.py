"""Tests the Groq client against a local fake server (no key, no internet): normal reply, per-minute 429 -> wait+retry,
daily-quota 429 -> next model in chain, 404 model -> next model, oversize request -> clear error. Run: python tests/test_llm.py"""
import json, os, sys, threading, tempfile, http.server
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.join(ROOT, "engine"))
import llm as L

calls = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"]))); m = body["model"]; calls.append(m)
        assert self.headers["Authorization"] == "Bearer k" and "acadza" in self.headers["User-Agent"]
        def err(code, msg, hdr=None):
            self.send_response(code); self.send_header("content-type", "application/json")
            for k, v in (hdr or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(json.dumps({"error": {"message": msg}}).encode())
        if m == "gone-model": return err(404, "The model `gone-model` does not exist")
        if m == "daily-model": return err(429, "Rate limit reached for model `daily-model` on tokens per day (TPD): Limit 100000, Used 99999, Requested 3000. Please try again in 14m2s.")
        if m == "big-model": return err(429, "Rate limit reached on tokens per minute (TPM): Limit 6000, Used 0, Requested 9000. Please try again in 1s.")
        if m == "minute-model" and calls.count("minute-model") == 1: return err(429, "Rate limit reached on tokens per minute (TPM): Limit 6000, Used 5900, Requested 500. Please try again in 0.2s.", {"retry-after": "1"})
        out = {"choices": [{"message": {"content": "hello " + m}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(out).encode())

srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
L.API_URL = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
tmp = tempfile.mkdtemp()
c = L.LLM("k", {"solve": ["gone-model", "daily-model", "ok-model"], "judge": ["minute-model"]}, tmp)
t, stop = c.call("sys", "user", 100, "solve"); assert t == "hello ok-model" and stop == "stop", (t, stop)
assert calls == ["gone-model", "daily-model", "ok-model"], calls
t, _ = c.call("sys", "user", 100, "judge"); assert t == "hello minute-model" and c.stats["retries"] == 1
n = len(calls); c.call("sys", "user", 100, "solve"); assert len(calls) == n and c.stats["cache_hits"] == 1       # cached
try: L.LLM("k", {"judge": ["daily-model"]}).call("s", "u", 10, "judge"); raise SystemExit("expected DailyLimit")
except L.DailyLimit: pass
# regression: quota hit on model 1, model 2 does not exist -> must still end as DailyLimit (the run stops cleanly), on every later call too
c2 = L.LLM("k", {"judge": ["daily-model", "gone-model"]})
for _ in range(2):
    try: c2.call("s", "u", 10, "judge"); raise SystemExit("expected DailyLimit")
    except L.DailyLimit: pass
try: L.LLM("k", {"judge": ["big-model"]}).call("s", "u", 10, "judge"); raise SystemExit("expected RequestTooLarge")
except L.RequestTooLarge: pass
assert abs(L._wait_from("Please try again in 14m2.5s.", {}) - 842.5) < 1e-6 and abs(L._wait_from("try again in 450ms", {}) - 0.45) < 1e-6
print("OK: Groq client handles replies, rate limits, daily quota, model fallback, oversize and caching")
