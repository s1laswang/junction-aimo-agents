# Evidence: Gemini montage ledgers

The Gemini cost and escalation numbers in [GEMINI.md](../GEMINI.md), [SUBMISSION.md](../SUBMISSION.md) and [demo-montage.md](../demo-montage.md) come from the shared usage ledger that `aimo/gemini.py` writes for every Gemini call. The ledger itself lives in the gitignored `data/gemini/ledger/`, so the two montage runs are copied here, sanitized, so anyone can recompute the numbers from a fresh clone without keys.

| File | Run | Rows |
|---|---|---|
| `montage-ledger-before-trim.jsonl` | `uv run aimo demo --montage`, round 2 (ledger pid 33508), before the token trim | 55 |
| `montage-ledger-after-trim.jsonl` | `uv run aimo demo --montage`, Sat 14 Nov 2026 18:21 Helsinki (ledger pid 61669), the run in demo-montage.md | 50 |

Reproduce (offline, about 1 s):
```bash
uv run python docs/evidence/montage_stats.py
```
Expected output: before trim 55 calls, 99,373 input tokens per run, 46% cached, hero 87,184, **$0.0822 per order**; after trim 50 calls, 30,533 per run, 31% cached (57,661 of 183,196), hero 27,403, **$0.0339 per order**; escalation 6/6 runs, 6/36 → 6/31 agent steps.

## What each row contains
`ts`, `feature` (`buyer.parse_need`, `buyer.agent_step`, `buyer.explain`, `buyer.summary`), `model`, `kind`, token counts (`in` includes `cached`; `out`; `thinking`), `latency_s`, `ok`, `error`, `run_id`, `meta` (thinking level, step number, whether the step escalated to gemini-4-hydrogen), plus two fields added here: `scenario` and `montage`.

## Sanitization
- The ledger never stores prompts, responses or keys (`aimo/gemini.py`), so there was nothing to redact there.
- Removed: `host` (machine name) and `pid`. Nothing else was changed.
- Added: `scenario`, assigned by run order (the montage always runs `happy, stockout, school-allergen, injection, drift, event`, `aimo/buyer/run_demo.py` `MONTAGE`). For the after-trim run this matches the run ids printed in demo-montage.md.

Source: the buyer engineer's worktree ledger (`.claude/worktrees/agent-a600a0aaf0279d5cb/data/gemini/ledger/Silass-MacBook-Pro-{33508,61669}.jsonl`), extracted Sun 15 Nov by the submit session.
