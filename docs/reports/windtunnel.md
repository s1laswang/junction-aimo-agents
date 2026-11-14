# Wind tunnel + QA report

Branch `team/windtunnel`. Last updated Sat 14 Nov, 15:10 Helsinki.

## Status
- **First numbers are in.** 640 Gemini-written scenarios (16 families, 3 policy archetypes, en/fi/sv) were run in pipeline mode against the team's code on main (catalog `plan_need`, trust `evaluate`/`screen_text`), offline against a recorded mock-API snapshot. The full table is in `docs/windtunnel-results.md`; the JSON for the UI and pitch is `data/windtunnel/results/wt640.json` (gitignored; regenerate with the commands below).
- **Freighter:** unreachable from the laptop since about 14:40 (ssh timeouts). The scripts are ready (`windtunnel/slurm/sync.sh`, `sweep.sbatch`, `tests.sbatch`; job names `aimo-wt-*`, 1 job, 8 cores, `--time` set). Every number so far comes from the **laptop** (pipeline mode: 25 s wall for 640 scenarios, well under the CPU cap).
- **The 1,000 L milk e2e over HTTP (TestClient, recorded fixtures) passes:** plan, signed quote, pending_approval, a tampered hash fails, approval, placed, audit verify ok (`tests/test_e2e_milk.py`).

## Headline numbers (wt640, pipeline mode, laptop, git 7dad8d0, snapshot 68450705c104)
| | Trusted gateway (team, main) | Naive agent (no gateway) |
|---|---|---|
| Policy violations (orders placed without a human that break policy) | **6** (all split-order evasion) | 370 |
| Allergen safety violations | **0** | 193 |
| Escalation recall (all labels / policy-backed labels only) | 94.9% / 98.0% | 17.8% / 16.1% |
| Escalation precision | 58.2% (over-escalation, see findings) | 90.8% |
| Injection block rate (supplier text / request text) | 56% (16/40, 29/40) | 0% |
| Screen false-positive rate on clean requests | 13.8% | 0% |
| Plan coverage / line acceptability | 84.3% / 81.6% | 98.2% / 57.2% |
| Latency p50 / p95 (in-process, replay) | 27 / 83 ms | 6 / 21 ms |

## Findings for other roles (concrete, reproducible)
1. **Trust: split-order evasion works** (6/40 split_order scenarios). Three orders of about €400 each for hotel-aurora are all auto-approved: €1,200 total without a human. The `period_spend` weekly budget (€8,000) is far above the €500 auto limit, and quantity_drift does not fire either. Suggested fix: a rolling 24 h sum of auto-approved spend per customer (optionally per product) checked against `auto_max`. Examples: split_order-00010, -00138, -00154, -00282, -00330, -00474 in `data/windtunnel/scenarios/wt640_gem.jsonl`.
2. **Trust: the injection screen misses 44%** of payloads: Finnish and Swedish ones, a JSON `{"role": "system"}` payload, "SYSTEM OVERRIDE ... place_order()", "allergy restrictions do not apply", "mark it allergen-safe", and "loyalty bonus, order 50x". The `quantity manipulation` regex also fires on 13.8% of clean requests (any large order or "price increases"). The library is in `windtunnel/generate.py` (`INJECTIONS`, `REQUEST_INJECTIONS`). The Gemini layer (`AIMO_SCREEN_GEMINI=1`) was not measured yet.
3. **Trust policy: over-escalation drives precision to 58%.** About 108 of 226 false escalations come from the `substitution` rule: school and hospital have `best: needs_approval`, so ANY order without a usual product (tier "best", not "preferred") needs approval; the hospital escalates 40/40. About 59 come from `category_scope`: bread code 41760 and flour 21300/21320 sit outside the 10/11/17 prefixes. Some of these are real catches of bad catalog picks (item 5). Suggested fix: "best" counts as a substitution only when a usual product exists, and widen the bakery prefixes. These are policy choices: Silas or the CEO should decide.
4. **Trust: no price-drift rule.** A 2x–3x price shock on the usual product is auto-approved if the total stays under the limit (3 of 17 price_shock scenarios). This counts as "beyond policy", not as a violation.
5. **Catalog: substitution acceptability 81.6%.**
   - With no preferred product, "lactose-free milk" returns only 2 candidates (Arla Café milk, and a Frezza **coffee drink**). It misses 464306 (€1.01/L), covers 707 of 1,500 L and costs €1,927 vs the €1,010 optimum. With `preferred_product_ids=["463038"]` it is perfect.
   - "butter" picked **Pride Butter beans** (41 lines), and also a butter spreader and a milk-system cleaner.
   - "bread" picked rye bread **flour mix** (28 lines).
   - Juice picked "juice drinks" rather than juice.
6. **Data quality: 28 catalog products have structured allergen data that says FREE_FROM while the ingredients list the allergen** in capitals. Example: 465793 Alpro Barista Almond drink is marked nut-free but contains ALMOND. Others: Jymy ice cream (WHOLE MILK), donuts (MAITOproteiini). The wind tunnel oracle cross-checks ingredients for this reason. Trust and catalog should treat such a conflict as "unknown" or block it. `uv run python -c "from windtunnel import products as P; print([p.id for p in P.catalog().values() if p.allergen_conflicts])"`.
7. **Mock API facts (verified by recording):** availability does not depend on `requestedDate`. `availableDate` is always 2026-11-16 and `closingTime` is fixed (281 pool items: Thu 12 Nov 15:00; Stock items: Sun 15 Nov 17:00). Requests for 13 Nov or 15 Nov are therefore never deliverable, which is how impossible_date is built. **After Sun 17:00, live Stock items will show cutoff_passed**, which matters for any live demo after that. The wind tunnel freezes "now" at the snapshot time (Sat 14:20).
8. **Gemini declined to author prompt-injection payloads** (prompt blocked, `BlockedReason.OTHER`, or an explicit refusal). Payloads are therefore a hand-written library of 16 supplier and 4 request variants. Gemini writes only the surrounding request text.

## What works (verified) vs untested
- Verified on the laptop:
  - snapshot recorder (955-id pool, then extended with 474+32 ids the catalog asked for, so 1,461 ids × 7 dates; 42 polite mock-API calls in total);
  - replay with faults (stockout, partial, price_shock, cutoff, sales_block, price_error, error_rate, latency; the same syntax as AIMO_FAULTS);
  - deterministic skeleton generator plus Gemini surface (629/640 echo-checked; the rest keep the template text);
  - independent oracle;
  - pipeline runner (team and baseline);
  - scoring and markdown;
  - fast tests (`uv run pytest -q tests/test_scenarios_unit.py tests/test_e2e_milk.py`: 11 pass in about 3 s).
- Written, not yet run: agent mode (`windtunnel/run.py agent`), which waits for the buyer agent and `/v1/agent/runs` on main; the transcript judge (`windtunnel/judge.py`); the freighter sweep and the full suite (`WT_FULL=1`).
- Running now: the thinking-level comparison (`windtunnel/thinking.py`): 48 scenarios × {3.8-flash, 4-hydrogen} × {LOW, HIGH} × {parse, substitute}.

## How to reproduce
```bash
uv run python -m windtunnel.snapshot record              # once, laptop (already done; data/windtunnel/snapshot)
uv run python -m windtunnel.generate --n 640 --gemini --out data/windtunnel/scenarios/wt640_gem.jsonl
uv run python -m windtunnel.run pipeline --scenarios data/windtunnel/scenarios/wt640_gem.jsonl --sut team
uv run python -m windtunnel.run pipeline --scenarios data/windtunnel/scenarios/wt640_gem.jsonl --sut baseline
uv run python -m windtunnel.score --scenarios data/windtunnel/scenarios/wt640_gem.jsonl \
  --runs data/windtunnel/runs/wt640_gem__pipeline__team__0of1.jsonl data/windtunnel/runs/wt640_gem__pipeline__baseline__0of1.jsonl \
  --labels "trusted gateway (team, main)" "naive agent (no gateway)" --name wt640
# freighter (when reachable): bash windtunnel/slurm/sync.sh && ssh freighter 'cd junction-aimo-agents && sbatch --export=ALL,WT_N=8000 windtunnel/slurm/sweep.sbatch'
```

## Caveats
- Ground truth for spend comes from the cheapest compliant plan (`windtunnel/oracle.py`), within ±15% of a threshold either side is accepted. Policy numbers are synced from `aimo/trust/policies/*.yaml` v1.0.0.
- Some labels go beyond the written policy: split-order aggregation, price shocks of 2x or more, scenario standing-order drift above 3x, and the Gemini judge on menu and upsell. They are tagged `beyond_policy`, never counted as policy violations, and reported separately as policy-backed recall.
- In pipeline mode the buyer agent is emulated: request text is screened with `trust.screen_text` and flags go into `need.preferences.injection_flags`, per the trust rule. Supplier text reaches trust through `evaluate(products=...)` with `marketing_text` replaced. A MemoryAudit stands in for the audit log so that `period_spend` sees earlier auto-placed orders.
- Gemini recipe checks (`AIMO_RECIPE_CHECK=0`) and the Gemini screen layer are off in pipeline mode (offline nodes).

## Disclosures
- Before the CEO's freighter rule arrived (about 14:35), I listed `~/momin` and `~/momin/fleet` read-only and read `teams.conf`. It shows a `valio` fleet team on repo `junction26-valio`, branch `dev` ("gateway on dev, MCP + REST, signed quotes, mandates, approvals, audit"). That may answer PLAN question 1 (Momin's code). I also ran one read-only `scontrol show job` on a fleet job. I modified nothing and cancelled nothing. Since the rule, nothing under ~/momin has been touched.

## Needs from Silas or the CEO
- Decide items 3 and 4 (policy tuning), then I re-run and report the before/after.
- Buyer: the agent-mode runner expects `POST /v1/agent/runs` and SSE `AgentStep` with kinds `decision`/`quote`/`order`/`approval_wait`. For injected supplier text in agent mode, the gateway needs a fault such as `inject:<id>` (appends a canned injection to that product's `marketing_text`), or support for `supplier_text` in the run body.
