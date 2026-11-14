# S2 `harden` report: no 500s, sane quantities, agent runs that never hang

Branch `team/harden`. Owner files: `aimo/gateway/schemas.py`, `make_quote` + plan input checks in
`aimo/gateway/service.py`, `aimo/buyer/runs.py`, `aimo/cli.py` (serve), `tests/test_fuzz.py`, `tests/test_harden.py`.

## Done (block 1)

| Item | What changed | Evidence |
|---|---|---|
| Error shape | Every error is JSON `{error, message}`. Validation errors keep pydantic's list in `detail` (loc/msg/type, inputs not echoed). HTTPException, pydantic `ValidationError` raised in handlers, and a last-resort catch-all are covered. `schemas.install_error_handlers(app)` | `tests/test_fuzz.py` |
| Fuzz | Table-driven over every `/v1` route from the live OpenAPI: bad path ids (unknown, huge, unicode, `..`), bad query values, garbage bodies (empty, `{`, `null`, arrays, invalid UTF-8, 200-deep nesting, 20 kB strings), per-field mutations (drop, null, wrong type, huge, unicode/RTL/NUL, negative, ±1e308, SQL/path strings) and nested quote-line mutations. Runs identified and anonymous. **0 server errors in ~2,600 requests.** It found and fixed: bad JSON and non-base64 `image_b64` on `/v1/agent/runs` (500), and a 20 kB `customer_id` (`OSError: File name too long` in the policy loader, `/v1/proposals`) | `uv run --extra dev pytest -q tests/test_fuzz.py` (≈45 s) |
| Fractional count units | `2.5 ST` → **422 `fractional_quantity`**, e.g. "2.5 ST of 464306: ST is a count unit (pieces, boxes, bottles), so order whole units, e.g. 2 or 3". Every sales unit except measures (KG, L, G, M...) counts things. A fraction is allowed only when `allowedLotSize` is a real fractional step (0.01 < lot < 1, e.g. 0.5). **Assumption:** the catalog-wide default `0.01` (on all 5,607 ST products) means "no step recorded", i.e. any whole unit (as `tests/test_edge_cases.py` `_round_lot(2.2, 0.01) == 3` already assumes), not permission to sell 2.5 pieces | `test_fractional_count_unit_is_422_with_plain_reason`, `test_fraction_allowed_only_by_a_real_fractional_lot_step` |
| Absurd quantities | `qty_sales_units` > 1e6 (also inf/NaN/bool/strings) → 422, REST via schema and MCP/in-process via `make_quote`. Split lines whose total exceeds 1e6 → 422 `absurd_quantity`. `/v1/plan` and MCP `plan_need` with `quantity` > 1e6 → 422 `absurd_quantity`; `product_query` over 500 characters → 422 | `test_absurd_*`, `test_plan_absurd_quantity_is_422` |
| Implausible quantities | `make_quote` flags `unknown_stock_large_qty` when a quantity is over 50× the customer's standing weekly baseline (seeded standing-order history in `aimo/agents/drift.py`), or, without a baseline, over 1,000 sales units with no stock figure (Terminal unknown stock). The reason goes into `provenance.line_verification`. `service.evaluate` → `_quantity_guard` turns such a quote into `needs_approval` (rule `quantity.plausibility`). It is never auto-approved, and a reject stays a reject | `test_large_qty_against_standing_baseline_needs_approval`, `test_quantity_guard_never_auto_and_keeps_reject` |
| Bad run body | `POST /v1/agent/runs`: empty, non-JSON, non-object, wrong types, unknown `thinking`/`transport`, bad `customer_id` slug, non-base64 image, blank text → 422 `{error, message}`. Length caps on every field | `test_bad_run_body_is_422_never_500` (11 cases) |
| MCP URL from the served port | Order: explicit `AIMO_MCP_URL`, then `AIMO_SERVE_URL` exported by `aimo serve --port N` (before uvicorn starts, so `--reload` workers inherit it), then the local socket the request arrived on (`scope["server"]`, never the client's Host header, so a caller can't point our agent at another server). If `AIMO_MCP_URL` names a local URL on another port, serve warns and overrides it. No `localhost:8000` default remains in `runs.py` | `test_mcp_url_follows_served_port`; live check on port 8793 |
| Fail fast, never fake | `runs.open_transport`: `auto` = MCP if it answers a 2 s probe, else the in-process gateway (same server, same guardrails). FakeGateway only when asked for explicitly (`transport: fake`). If nothing works, the run ends with an `error` step naming every attempt ("no fake fallback") and then `final`, within seconds | `test_unreachable_gateway_fails_fast_never_fake` |
| Never waits forever | Watchdog per run: `AIMO_BUYER_RUN_TIMEOUT` (300 s; time spent waiting for a human answer is excluded). Slot wait is bounded by `AIMO_BUYER_QUEUE_TIMEOUT` (30 s; it used to block forever when 8 runs were stuck). Steps arriving after `final` are dropped | `test_hung_run_is_closed_by_watchdog` |
| One audit clock | Audit events are stamped with `aimo.backend.clock.now()`, the same clock as quotes, cut-offs and expiry (simulated under `AIMO_NOW` / `X-Aimo-Now` / replay anchor, otherwise real). When simulated, the real wall time is kept in `data.clock.wall_ts`. The report header is labelled, e.g. "times in Helsinki (EET), simulated clock (AIMO_NOW); real wall time is in each event's data.clock.wall_ts" (or "real clock", or "mixed" with `(sim)` markers). Gateway audit summaries that said "UTC" now say Helsinki | `test_audit_report_uses_simulated_clock_and_labels_it`, `test_audit_report_real_clock_label` |

Gate (Sat 14 Nov, branch before rebase): `pytest` 306 passed, 7 skipped, 2 xfailed. `scenarios.run_all` 8/8.
`make accept` PASS (15 pass, 1 skip = live Gemini demo).

## Edits outside the owned list (small, called out for review)
- `aimo/gateway/app.py`: **2 lines** in `create_app` mount `install_error_handlers(app)` (the handlers live in schemas.py).
- `aimo/gateway/service.py`, outside `make_quote`:
  - `mandate_for` refuses customer ids that are not a short slug (403 `unknown_customer`). Without it, a 20 kB or `../` id reached `trust.policy.load_policy` as a file path.
  - `evaluate` is wrapped by `_quantity_guard`, so `unknown_stock_large_qty` never auto-approves.
  - Two audit summary strings changed from UTC to Helsinki.
- `tests/test_gateway.py`: `test_demo_runs_in_the_past_with_simulated_clock` now sets `AIMO_NOW_FROZEN=1`. It
  asserted minute precision on a clock that runs from process start, so it failed once the suite took more than
  60 s to reach it (the fuzz test runs earlier). A latent flake, not a behaviour change.
- `aimo/trust/audit.py` (no session owns it): `append` uses the gateway clock, and `render` labels the clock (item 4).
- `docs/interfaces.md` and `aimo/models.py`: untouched. New error codes: `validation_error`, `fractional_quantity`,
  `absurd_quantity`, `bad_quantity` (was 400, now 422), `bad_product_id`, `too_many_lines`, `query_too_long`,
  `empty_need`, `invalid_json`, `invalid_body`, `invalid_image`, `text_required`, `image_too_large`. New flag and rule:
  `unknown_stock_large_qty`, `quantity.plausibility`.

## Requests
- **S3 (need / policy):** please add a native policy rule for `unknown_stock_large_qty` in `aimo/trust/policy.py`, so
  the reason shows up in the policy table like every other rule. The gateway guard stays as defense in depth either way.
- **Buyer owner (`aimo/buyer/tools.py`):** `connect("auto")` still ends in `fake` for CLI callers (`aimo demo`). Gateway runs
  no longer go through that path; consider dropping `fake` from `auto` there too.
- **S1 (`scripts/judge_accept.sh`):** pytest output goes to the fixed path `/tmp/aimo-accept-pytest.txt`, which
  parallel sessions share and overwrite. Use `mktemp` (or a path under the worktree) so a FAIL row points at your
  own log.
- **S1 (replay):** replay mode anchors the clock, so audit trails in replay are labelled "simulated clock (replay
  snapshot)". That is intended.

## Open
- None blocking. The fuzz test takes about 45 s of the suite, mostly first-call catalog warm-up in `/v1/plan`.
