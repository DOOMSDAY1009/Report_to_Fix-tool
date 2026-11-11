"""Minimal Groq client (OpenAI-compatible chat API). Standard library only.
Handles: free-tier 429s (waits as told), daily-quota exhaustion (falls back to the next model, else stops cleanly),
disk cache (re-runs and next-day resumes are free), per-stage model chains, token accounting."""
import json, os, re, time, random, hashlib, threading, urllib.request, urllib.error

API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Each stage has its own model chain: the first is used until it is unavailable or its daily quota is gone, then the next.
# Separate models have separate free quotas, which is why the two stages use different first choices.
DEFAULT_CHAINS = {"solve": ["llama-3.3-70b-versatile", "openai/gpt-oss-20b"],
                  "judge": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]}
UA = "acadza-triage/1.0"   # Groq sits behind Cloudflare, which can reject urllib's default User-Agent

class AuthError(RuntimeError): pass
class ModelNotFound(RuntimeError): pass
class DailyLimit(RuntimeError): pass
class RequestTooLarge(RuntimeError): pass

def load_api_key(root):
    k = os.environ.get("GROQ_API_KEY")
    if k: return k.strip()
    p = os.path.join(root, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line.startswith("GROQ_API_KEY") and "=" in line:
                return line.split("=", 1)[1].strip().strip("\"'")
    return None

def _wait_from(msg, headers):
    m = re.search(r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?\s*(?:([\d.]+)ms)?", msg)
    if m and any(m.groups()):
        h, mi, s, ms = (float(x) if x else 0 for x in m.groups()); return h * 3600 + mi * 60 + s + ms / 1000
    try: return float(headers.get("retry-after") or 0)
    except ValueError: return 0

class LLM:
    def __init__(self, api_key, chains=None, cache_dir=None, max_retries=10, efforts=None):
        self.efforts = {"solve": "low", "judge": "medium", **(efforts or {})}   # reasoning effort for gpt-oss models: low|medium|high
        self.key, self.chains, self.cache_dir, self.max_retries = api_key, {k: list(v) for k, v in (chains or DEFAULT_CHAINS).items()}, cache_dir, max_retries
        self.idx = {k: 0 for k in self.chains}; self.lock = threading.Lock(); self.quota_hit = {k: None for k in self.chains}
        self.stats = {"calls": 0, "cache_hits": 0, "retries": 0, "in": 0, "out": 0}
        if cache_dir: os.makedirs(cache_dir, exist_ok=True)

    def _path(self, tag, system, user, max_tokens):
        if not self.cache_dir: return None
        h = hashlib.sha256(json.dumps([self.chains[tag][0], self.efforts.get(tag), system, user, max_tokens], ensure_ascii=False).encode()).hexdigest()[:32]
        return os.path.join(self.cache_dir, h + ".json")
    def invalidate(self, system, user, max_tokens, tag="judge"):
        p = self._path(tag, system, user, max_tokens)
        if p and os.path.exists(p): os.remove(p)

    def _request(self, model, system, user, max_tokens, effort="low"):
        body = {"model": model, "temperature": 0, "max_completion_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if model.startswith("openai/gpt-oss"): body["reasoning_effort"] = effort     # higher = better verdicts, but reasoning tokens count against your daily quota
        data = json.dumps(body).encode()
        hdr = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": UA}
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(urllib.request.Request(API_URL, data=data, headers=hdr), timeout=180) as resp:
                    d = json.load(resp)
                ch = d["choices"][0]; text = ch["message"].get("content") or ""
                return text, ("max_tokens" if ch.get("finish_reason") == "length" else ch.get("finish_reason")), d.get("usage", {})
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:700]; low = msg.lower()
                if e.code in (401, 403) and "1010" not in msg and "cloudflare" not in low: raise AuthError(f"HTTP {e.code}: {msg}")
                if e.code == 404 or (e.code == 400 and "model" in low and any(w in low for w in ("not found", "decommission", "does not exist", "not supported"))):
                    raise ModelNotFound(f"{model}: {msg}")
                if e.code == 413: raise RequestTooLarge(f"{model}: {msg}")
                if e.code == 429:
                    if "per day" in low or "(tpd)" in low or "(rpd)" in low: raise DailyLimit(f"{model}: {msg}")
                    rq, lim = re.search(r"Requested\s+(\d+)", msg), re.search(r"Limit\s+(\d+)", msg)
                    if rq and lim and int(rq.group(1)) > int(lim.group(1)): raise RequestTooLarge(f"{model}: single request bigger than the per-minute limit: {msg}")
                if e.code in (429, 500, 502, 503, 529) and attempt < self.max_retries:
                    wait = min(90, (_wait_from(msg, e.headers) or 2 ** min(attempt, 5)) + 0.5 + random.random())
                    with self.lock: self.stats["retries"] += 1
                    time.sleep(wait); continue
                raise RuntimeError(f"HTTP {e.code}: {msg}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries:
                    with self.lock: self.stats["retries"] += 1
                    time.sleep(min(60, 2 ** min(attempt, 5)) + random.random()); continue
                raise RuntimeError(f"network error: {e}")

    def call(self, system, user, max_tokens=2000, tag="judge"):
        """Returns (text, stop_reason). tag selects the model chain ('solve' or 'judge')."""
        p = self._path(tag, system, user, max_tokens)
        if p and os.path.exists(p):
            c = json.load(open(p, encoding="utf-8"))
            with self.lock: self.stats["cache_hits"] += 1
            return c["text"], c["stop"]
        last = None
        while True:
            with self.lock: i = self.idx[tag]; model = self.chains[tag][i] if i < len(self.chains[tag]) else None
            if model is None:
                if self.quota_hit[tag] or isinstance(last, DailyLimit): raise self.quota_hit[tag] or last   # a quota problem stays a quota problem, even if a later fallback model does not exist
                raise RuntimeError(f"no usable model for stage '{tag}'. Last error: {last}")
            try:
                text, stop, usage = self._request(model, system, user, max_tokens, self.efforts.get(tag, "low")); break
            except (ModelNotFound, DailyLimit) as e:
                last = e
                with self.lock:
                    if isinstance(e, DailyLimit): self.quota_hit[tag] = e
                    if self.idx[tag] == i: self.idx[tag] += 1
                print(f"  [llm] {tag}: {model} unavailable ({type(e).__name__}); trying next model in the chain")
        with self.lock:
            self.stats["calls"] += 1; self.stats["in"] += usage.get("prompt_tokens", 0); self.stats["out"] += usage.get("completion_tokens", 0)
        if p: json.dump({"text": text, "stop": stop, "model": model, "tag": tag}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        return text, stop

    def used_models(self):
        return {t: c[min(self.idx[t], len(c) - 1)] for t, c in self.chains.items()}
