# Wind tunnel evidence (wt640): committed inputs, reproducible

Headline (re-scored Sun 15 Nov 05:20 on main `a99692f`, pipeline mode, deterministic oracle `windtunnel/oracle.py`):

| System | Policy violations | Allergen violations | Escalation recall | Escalation precision | Injection blocked |
|---|---|---|---|---|---|
| Trusted gateway (main a99692f) | **0** | **0** | 99.7% | 68.2% | 100% |
| Naive agent, direct API access | 372 | 193 | 17.8% | 90.8% | 0% |

The same numbers are in `docs/windtunnel-results.json` under `main`. The earlier Sat 18:24 run, before the policy changes (1 policy violation), is kept under `main_before_s3`. 9 of 640 scenarios errored in the pipeline and are counted as errors, not as passes.

Files:
- `wt640_gem.jsonl.gz`: the 640 Gemini-written scenarios (16 families × 40, 3 policy archetypes), with oracle labels.
- `snapshot.tar.gz`: the recorded Valio mock-API responses (public price and availability data) that the pipeline replays offline.
- `wt640_gem__pipeline__team__a99692f.jsonl.gz` and `wt640_gem__pipeline__baseline.jsonl.gz`: the run files behind the table.

Reproduce (offline, about 3 min). No Gemini key is needed, but the pipeline reads the challenge product data `data/valio_aimo_junction26_product_data.json`, which a keyless clone does not have: run `uv run aimo fetch-data` once with `AIMO_API_KEY` in `.env`. The committed run was made on `a99692f`; no later commit up to `7c290cf` changes the code the pipeline runs (`git diff --stat a99692f 7c290cf -- aimo/catalog aimo/trust aimo/backend windtunnel` is empty). The injection column is the regex screen only (Gemini is off in pipeline mode); on the separate 36-payload evaluation set that screen has 44% recall (`docs/windtunnel-results.md`).
```bash
mkdir -p /tmp/wt/scenarios /tmp/wt/runs
gunzip -c docs/evidence/windtunnel/wt640_gem.jsonl.gz > /tmp/wt/scenarios/wt640_gem.jsonl
tar -xzf docs/evidence/windtunnel/snapshot.tar.gz -C /tmp/wt
WT_DIR=/tmp/wt AIMO_SEMANTIC=0 uv run python -m windtunnel.run pipeline --scenarios /tmp/wt/scenarios/wt640_gem.jsonl --sut team
WT_DIR=/tmp/wt AIMO_SEMANTIC=0 uv run python -m windtunnel.run pipeline --scenarios /tmp/wt/scenarios/wt640_gem.jsonl --sut baseline
WT_DIR=/tmp/wt uv run python -m windtunnel.score --scenarios /tmp/wt/scenarios/wt640_gem.jsonl \
  --runs /tmp/wt/runs/wt640_gem__pipeline__team__0of1.jsonl /tmp/wt/runs/wt640_gem__pipeline__baseline__0of1.jsonl \
  --labels "trusted gateway" "naive agent" --name wt640 --no-md
```
