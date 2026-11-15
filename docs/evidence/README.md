# Evidence: Gemini montage ledgers

The Gemini cost and escalation numbers in [GEMINI.md](../GEMINI.md), [SUBMISSION.md](../SUBMISSION.md) and [demo-montage.md](../demo-montage.md) come from the shared usage ledger that `aimo/gemini.py` writes for every Gemini call. The ledger itself lives in the gitignored `data/gemini/ledger/`, so the three montage runs are copied here, sanitized, so anyone can recompute the numbers from a fresh clone without keys.

| File | Run | Rows |
|---|---|---|
| `montage-ledger-before-trim.jsonl` | `uv run aimo demo --montage`, round 2 (ledger pid 33508), before the token trim | 55 |
| `montage-ledger-after-trim.jsonl` | `uv run aimo demo --montage`, Sat 14 Nov 2026 18:21 Helsinki (ledger pid 61669), after the token trim | 50 |
| `montage-ledger-sun.jsonl` | `uv run aimo demo --montage`, Sun 15 Nov 2026 02:54 Helsinki, re-recorded on current main (human-only approvals, policy changes) with a scratch state dir; **the run in demo-montage.md** | 53 |

Reproduce (offline, about 1 s):
```bash
uv run python docs/evidence/montage_stats.py
```
Expected output: before trim 55 calls, 99,373 input tokens per run, 46% cached, hero 87,184, **$0.0822 per order**; after trim 50 calls, 30,533 per run, 31% cached (57,661 of 183,196), hero 27,403, **$0.0339 per order**; escalation 6/6 runs, 6/36 → 6/31 agent steps. Sunday re-record (`sun`): 53 calls, 37,584 per run, 38% cached (86,792 of 225,503), hero 30,502, **$0.0352 per order**; escalation 5/6 runs, 5/34 agent steps (15%).

## What each row contains
`ts`, `feature` (`buyer.parse_need`, `buyer.agent_step`, `buyer.explain`, `buyer.summary`), `model`, `kind`, token counts (`in` includes `cached`; `out`; `thinking`), `latency_s`, `ok`, `error`, `run_id`, `meta` (thinking level, step number, whether the step escalated to gemini-4-hydrogen), plus two fields added here: `scenario` and `montage`.

## Sanitization
- The ledger never stores prompts, responses or keys (`aimo/gemini.py`), so there was nothing to redact there.
- Removed: `host` (machine name) and `pid`. Nothing else was changed.
- Added: `scenario`, assigned by run order (the montage always runs `happy, stockout, school-allergen, injection, drift, event`, `aimo/buyer/run_demo.py` `MONTAGE`). For the after-trim run this matches the run ids printed in demo-montage.md at the time. For the `sun` run, `scenario` comes from the run ids printed in the current demo-montage.md (`run-21ad151bbc` happy … `run-518a9f4ae1` event), and only `buyer.*` rows of those six runs are kept (the gateway's own 7 catalog calls in that window, `catalog.recipe_compatible` and `catalog.embed.query`, are left out, as before).

Source: the buyer engineer's worktree ledger (`.claude/worktrees/agent-a600a0aaf0279d5cb/data/gemini/ledger/Silass-MacBook-Pro-{33508,61669}.jsonl`), extracted Sat 14 Nov 23:00 by the submit session. The `sun` rows come from the scratch montage data dir's ledger (`~/aimo-montage-data/gemini/ledger/`), extracted Sun 15 Nov 03:05 by the live session ([reports/live.md](../reports/live.md)).
