# Wind tunnel evidence (wt640): committed inputs, reproducible

Headline (re-scored Sun 15 Nov 05:50 on the commit that added `wt640_gem__pipeline__team__final.jsonl.gz`, pipeline mode, deterministic oracle `windtunnel/oracle.py`):

| System | Policy violations | Allergen violations | Escalation recall | Escalation precision | Injection blocked |
|---|---|---|---|---|---|
| Trusted gateway (final policy) | **0** | **0** | 99.7% | 69.5% | 100% |
| Naive agent, direct API access | 372 | 193 | 17.8% | 90.8% | 0% |

The same numbers are in `docs/windtunnel-results.json` under `main`. The earlier Sat 18:24 run, before the policy changes (1 policy violation), is kept under `main_before_s3`. 9 of 640 scenarios errored in the pipeline and are counted as errors, not as passes.

Files:
- `wt640_gem.jsonl.gz`: the 640 Gemini-written scenarios (16 families × 40, 3 policy archetypes), with oracle labels.
- `snapshot.tar.gz`: the recorded Valio mock-API responses (public price and availability data) that the pipeline replays offline.
- `wt640_gem__pipeline__team__final.jsonl.gz` and `wt640_gem__pipeline__baseline.jsonl.gz`: the run files behind the table (`wt640_gem__pipeline__team__a99692f.jsonl.gz` is also kept for the earlier `a99692f` 68.2%-precision run).

Reproduce (offline, about 3 min). No Gemini key is needed, but the pipeline reads the challenge product data `data/valio_aimo_junction26_product_data.json`, which a keyless clone does not have: run `uv run aimo fetch-data` once with `AIMO_API_KEY` in `.env`. The committed run was made on the final policy: it was re-run after the last policy change (a pre-approved pack swap no longer escalates as "outside preference"; the weekly-volume window counts only orders placed without a human), in the commit that added the run file. Before that change precision was 68.2% (run on `a99692f`). The injection column is the regex screen only (Gemini is off in pipeline mode); on the separate 36-payload evaluation set that screen has 44% (`44.4%`, 16 of 36; `94.4%` with `AIMO_SCREEN_GEMINI=1`) recall (`docs/windtunnel-results.md`).
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

## Why the gateway still asks (145 unnecessary escalations of 476)

Escalation precision is 69.5% (331 needed, 145 not), at 99.7% recall with 0 violations. The breakdown of the unnecessary ones, from the committed run file:

| Cause | Share | Why |
|---|---|---|
| Deliberate safety stops | about 37 | a detected injection (21) or a price jump (16): the gateway asks a human by design |
| Planner chose a non-usual product where the usual one would do | about 43 | a real improvement to make: prefer the customer's usual product whenever it fits |
| School and hospital policies ("any substitution needs the kitchen manager or dietitian") | most of the rest | strict by the customer's own rule |

What already keeps it down: a pack swap the customer listed (or a verified best-tier swap where the policy allows best) passes on its own under the spend limit; `auto_approve_alternatives_below_eur`; and "always allow this swap", which a human can tick once on approval (`aimo/trust/learned.py`).

## Zero-key browser guardrail surfaces

Judges can test the same policy, allergen, prompt-injection, price-drift, and human-approval guardrails interactively in any browser with zero API keys and zero backend setup via the **One-Page Judge Walkthrough** ([../../demo/index.html](../../demo/index.html), `docs/demo/index.html`) and **"Try to Scam the Agent"** ([../../arena/](../../arena/), `docs/arena/index.html`; `CC0-1.0` Kenney pixel-art asset ledger in [../../demo/ASSETS.md](../../demo/ASSETS.md), `docs/demo/ASSETS.md`, and [../../arena/assets/manifest.json](../../arena/assets/manifest.json); see also [../README.md](../README.md) and [../live-run/README.md](../live-run/README.md)).
