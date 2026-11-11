# Acadza Report → Fix

Triage engine (LLM + deterministic guards) and a faculty review console for broken-question reports.

```
data/            questions.json, reports.json, taxonomy.json (from the assignment)
engine/          triage.py (pipeline) · llm.py (Groq client) · prompts.py (all prompts) · offline.py (recorded judgements)
console/         template.html -> index.html (static faculty console, no backend)
results.json     precomputed run (recorded judgements through the real guards); a live run replaces it      results.recorded.json   same, kept for compare.py
tests/           mock_test.py, test_llm.py (no API key needed)             compare.py              live vs recorded
```

## 1. Get a free Groq API key (only external requirement)
1. Go to https://console.groq.com/keys → sign up (no credit card) → **Create API Key** → copy it (`gsk_...`).
2. Give it to the program, either way:
   - Mac/Linux: `export GROQ_API_KEY=gsk_...`
   - Windows PowerShell: `$env:GROQ_API_KEY="gsk_..."`
   - or copy `.env.example` to `.env` and paste the key there (`.env` is git-ignored).

Python 3.9+ is the only other requirement. There is nothing to `pip install` (standard library only).

## 2. Run it live
```bash
python engine/triage.py --dry-run     # no API call: size estimate
python tests/mock_test.py             # no API call: pipeline wiring
python tests/test_llm.py              # no API call: rate-limit / fallback handling
python engine/triage.py --limit 2     # real smoke test on 2 questions: do this first!
python engine/triage.py               # full run -> results.json + results_meta.json
python compare.py                     # how the live run differs from the recorded one
python engine/build_console.py        # bakes results.json into console/index.html
```
Open `console/index.html` in a browser (or `python -m http.server -d console 8000`). Shortcuts: `j/k` move, `a` approve, `e` edit, `r` reject.
The banner at the top of the console says whether it shows a live run or recorded judgements.

Options: `--solve-model` / `--judge-model` (any Groq model id, e.g. `llama-3.3-70b-versatile`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b`), `--workers 2`, `--no-cache`, `--offline` (no API; writes `results.recorded.json`).
Model names and limits change; the list of current models is at https://console.groq.com/docs/models.

**Quality note:** open models on the free tier are weaker at JEE-level maths than frontier models. That is why the guards matter: a new answer key is only auto-applied if it matches the independent key-blind solve, and anything doubtful goes to faculty.
Expect more rows in "Needs you" than the recorded run. Read `results.json` yourself, check every `auto_apply` row, and tune `engine/prompts.py` if verdicts look wrong (then delete `engine/.cache/` and re-run).

## 3. How it works
- **A. Group** reports by question; cheap pre-checks (prompt-injection patterns, "record changed after the report").
- **B. LLM** (2 calls per question): a solver that never sees the key, then a judge that sees everything and returns a verdict, repair, surgical edit, evidence and student reply for every report on that question.
- **C. Guards (code, not the model):** the edit must stay inside the repair's allowed fields and its `find` must match exactly once; a new answer key must equal the key-blind solve and have ≥2 confirmations; spelling edits can't touch digits/math; `stem_data`/`option_repair`/`answer_form` can only add; lost options are never invented; a `no_error` verdict is sent to a human if the blind solve disagrees with the key; any LLM failure becomes `faculty_review`.

## 4. Deploy the console (free, 2 minutes)
The console is one static file, so any static host works. Do this **after** `build_console.py` has produced `console/index.html` from your live `results.json`.

**Vercel (CLI):** `cd console && npx vercel --prod` (log in when asked; accept the defaults).
**Vercel (GitHub):** push the repo, import it at vercel.com/new; `vercel.json` already sets the output folder to `console`.
**Netlify:** drag the `console` folder onto https://app.netlify.com/drop (or connect the repo; `netlify.toml` is included).

Put the live URL in your email. Free tiers stay up for the 7 days required.

## 5. Submission checklist
- [ ] `results.json` committed (precomputed by default; replace with a COMPLETE live run if quota allows; a run with failures writes `results.partial.json` and leaves `results.json` alone, `--force` overrides) · [ ] repo is public (don't commit `.env`)
- [ ] PROMPTS.md: fill the TODOs (model/date/cost, what you changed after seeing results, your own AI-assistant prompts, honest failures)
- [ ] Deployed URL works and shows the "Live LLM run" banner
- [ ] 3-minute video: queue ordering (0:00) → a multi-report case with before/after (0:40) → a tricky case, e.g. R034 or R020 (1:20) → the prompt-injection report R027 (2:00) → approve + audit trail (2:30)
- [ ] Email: repo, live URL, video, and the four personal questions

## Troubleshooting
- `API key rejected` → wrong key. · Long pauses → normal, it is waiting out a per-minute limit. · `Daily free quota used up` → re-run later; progress is cached. · `model unavailable` → pass `--solve-model` / `--judge-model` with a model listed on console.groq.com/docs/models. · HTTP 403 "1010" → network/VPN blocking Groq; try another network.
- A report ends with "LLM output unusable" → it was routed to faculty on purpose; re-run (only that call repeats).
