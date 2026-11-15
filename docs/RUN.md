# Running Trusted Trade Agents

Everything needed to run, test and approve locally. Back to the [README](../README.md).

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| Python 3.11+ | runtime (`requires-python = ">=3.11"`) | system package or uv-managed |
| [uv](https://docs.astral.sh/uv/) | one-command runner and lockfile installer | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Network | optional: only needed for live Valio mock API / Gemini calls or downloading the full 17,546-product JSON (`aimo fetch-data`); keyless replay runs offline from `tests/fixtures/snapshot/replay/` | none |

Environment variables (names only; copy `.env.example` to `.env` and fill them in; never commit `.env`):

| Variable | Required for | If missing |
|---|---|---|
| `AIMO_API_KEY` (alias: `VALIO_API_KEY`) | live Valio Aimo mock API calls and `aimo fetch-data` / `aimo serve` downloading the full 17,546-product `data/valio_aimo_junction26_product_data.json` (plus reliability and CO2 CSVs), needed for the live catalog and full-catalog offline `pytest` cases | **Replay mode, automatically** (`served_from: replay`): `aimo serve`, `make accept`, `scenarios.run_all` (8/8 PASS, 88 checks), `conformance` (44/44), and `qa_milk_flow.py` run off the bundled snapshot and 669-product gzipped catalog subset in `tests/fixtures/snapshot/replay/`. Full-catalog offline `pytest` cases skip cleanly when `data/valio_aimo_junction26_product_data.json` is absent (and with `AIMO_REPLAY=0` and no catalog, `/v1/products/search` and `/v1/plan` return 503 `catalog_missing`). |
| `AIMO_PORTAL_URL` (alias: `VALIO_API_BASE`), `AIMO_PORTAL_PASSWORD` | mock API base URL (default `https://junction26.valioaimo.fi/`) and portal login to obtain a mock-API key if you do not already have `AIMO_API_KEY` | defaults to the challenge URL; leave password blank if you already have a key |
| `GEMINI_API_KEY` (alias: `GOOGLE_API_KEY`), `GEMINI_FALLBACK_MODELS` | live Gemini buyer in `uv run aimo demo`, embeddings / allergen hints, wind-tunnel live runs; `GEMINI_FALLBACK_MODELS` (default `gemini-3.5-flash,gemini-4-hydrogen`) provides immediate daily-quota fallback (`meta.fallback_from`, 30-min skip) | `uv run aimo demo` finishes end to end via the deterministic regex parser and scripted buyer fallback over MCP (and `make accept` runs it keyless); `/ui` runs on a deterministic mock model; `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` runs the REST milk flow with no LLM; `aimo serve --no-build` skips Gemini catalog builds. |
| `AIMO_GEMINI_TRACE` | optional raw per-attempt JSONL trace (`=<file>`) of Gemini requests, responses, usage, latency and `requested_model` vs answering `model` for evidence runs ([evidence/live-run/README.md](evidence/live-run/README.md), `docs/evidence/live-run/summarize.py`; never logs the API key) | Off by default (`data/gemini/ledger/*.jsonl` still records token counts and latency without prompts). |
| `AIMO_APPROVER_PINS` / `AIMO_APPROVER_PIN` | **human approver credentials** (see [Approving as a human](#approving-as-a-human)): per-approver JSON/CSV map (`AIMO_APPROVER_PINS`), with `AIMO_APPROVER_PIN` kept as single-credential override | Distinct 12-char passphrases are generated per directory approver into `data/keys/approver_pins.json` (mode `0600`, overridable via `AIMO_APPROVER_PINS`), with Anna's primary credential (`anna@hotel-aurora.example`) mirrored into `data/keys/approver_pin` for local CLI scripts (`AIMO_APPROVER_PIN` kept as single-credential override). Brute-force lockout is per `(approver_id, client)` (`5` wrong guesses -> 429 `approver_locked` for 5 min, `30` per 10 min per-approver backstop). No API route or MCP tool ever returns any credential. |
| `AIMO_REPLAY` | `=1` (or `=tests/fixtures/snapshot/replay`, or `=tests/fixtures/snapshot`) forces replay from the bundled snapshot even with a key (no network for prices and stock); `=<dir>` uses another snapshot; `=0` turns automatic keyless replay off | Unset with `AIMO_API_KEY`: live mock API (with automatic fallback to `tests/fixtures/snapshot/replay` if upstream is unreachable). Unset without `AIMO_API_KEY`: automatic replay from `tests/fixtures/snapshot/replay` (669 product ids + 669-product catalog subset across `2026-11-16`..`2026-11-20`). Replayed responses say `served_from: replay` and the UI header shows a REPLAY pill. |
| `ELEVENLABS_API_KEY` | optional: ElevenLabs voice for **Decide by voice** | The browser's own speech synthesis and recognition are used instead. |
| `AIMO_NOW` | simulated clock, e.g. `AIMO_NOW=2026-11-15T10:00:00+02:00` (add `AIMO_NOW_FROZEN=1` to stop it running forward) | Real clock. Replay mode anchors the clock at the active snapshot's recording time (`2026-11-14T23:50:28+0200` for `tests/fixtures/snapshot/replay`, `2026-11-14T14:20:06+0200` for `tests/fixtures/snapshot`) unless `AIMO_NOW` is set. |
| `AIMO_DEMO_FAULTS` / `AIMO_FAULTS` | operator-only demo fault and clock overrides: `AIMO_DEMO_FAULTS=1` lets agent-authenticated requests pass `X-Aimo-Faults` and `X-Aimo-Now` and lets `POST /v1/agent/runs` use body `faults` or `inproc`/`fake` test transports (needed for `uv run aimo demo --montage` and scenario runs); `AIMO_FAULTS` applies faults server-wide | `X-Aimo-Faults` and `X-Aimo-Now` on agent requests and body `faults` / `inproc` / `fake` transports on `POST /v1/agent/runs` (`4ccc1fb`) are ignored (and never honoured with `AIMO_ENV=production`), so an agent cannot backdate the clock past a cut-off or fake stock. |

Do not put real values in the README. A judge machine with none of these variables set (an empty `.env`, no `AIMO_API_KEY`, no `GEMINI_API_KEY`) runs `make accept` end to end in replay mode (`RESULT: PASS`: `scenarios.run_all` 8/8 PASS in replay, `conformance` 44/44, `qa_milk_flow.py` places an order answered with Peppol `AP` and verified audit chain, and `aimo demo` finishes via the scripted buyer over MCP; full-catalog offline `pytest` cases skip cleanly when `data/valio_aimo_junction26_product_data.json` is absent).

One-command check:

```bash
cp .env.example .env    # fill keys if you have them
make accept             # or: uv run bash scripts/judge_accept.sh
```

`make accept` runs in an isolated per-run temp directory (`02575d8`): it runs pytest, runs `scenarios.run_all` (8/8 PASS, live when keyed or off `tests/fixtures/snapshot/replay` when keyless), starts the gateway on a free port with a fresh approver credential, checks the discovery and docs endpoints, runs the conformance suite (44/44), `qa_milk_flow.py` (search -> offers -> plan -> signed quote -> pending approval -> wrong-hash 409 -> human approval -> Peppol AP -> audit verify), `requote_beat.py` when present (`409 requote_needed` on an expired price-moved quote -> replacement signed quote -> human approval -> Peppol AP + audit verify), and (when keyless) `uv run aimo demo` via the scripted buyer. It prints the placed order id, its Peppol `OrderResponseCode` and the audit verify result, then a PASS/FAIL/SKIP table ending in `RESULT: PASS` or `RESULT: FAIL`.

### Keyless vs keyed

| | Keyless (empty `.env`) | Keyed (`AIMO_API_KEY`, `GEMINI_API_KEY`) |
|---|---|---|
| Price and availability | Automatic replay of recorded mock-API responses (`tests/fixtures/snapshot/replay/`: 669 product ids across delivery dates `2026-11-16`..`2026-11-20`, recorded `2026-11-14T23:50:28+0200` via `scripts/record_replay.py` with 10 upstream calls; `tests/fixtures/snapshot` also holds 92 ids across 13, 15, 16 and 17 Nov). Dates that were not recorded are refused, never invented. | Live Valio Aimo mock API with a 5-minute cache; if the upstream is unreachable after retries it falls back to the bundled snapshot (`served_from: replay`) |
| Clock | Anchored at the snapshot's recording time (`2026-11-14T23:50:28+0200`), so the story's Monday cut-offs still hold | Real time, or `AIMO_NOW` |
| Catalog | Committed 669-product gzipped challenge catalog subset (`tests/fixtures/snapshot/replay/catalog/`, unpacked automatically into `<DATA_DIR>/catalog-fixture/`). Full-catalog offline `pytest` cases (17,546 products) skip cleanly when `data/valio_aimo_junction26_product_data.json` is absent. With `AIMO_REPLAY=0` and no catalog at all, search and plan answer 503 `catalog_missing` ("run `uv run aimo fetch-data`"), never a 500. | Full 17,546 products, downloaded into `data/` on first `aimo serve` or `aimo fetch-data` |
| Buyer agent | `uv run aimo demo` (deterministic regex parser and scripted buyer fallback over MCP), `/ui` with a deterministic mock model (the hero request reaches the approval wait), `qa_milk_flow.py` (scripted, no LLM) or `/ui?mock=1` (no backend) | `/ui` or `uv run aimo demo` with Gemini |
| Search | Full-text (FTS5 trigram over the 669-product replay subset, or all 17,546 products if `data/` is present) | Full-text plus gemini-embedding-2 (17,546 products) |

## Quickstart details

Requirements: see Prerequisites.

```bash
cp .env.example .env    # optional: fill AIMO_API_KEY and GEMINI_API_KEY for live upstream + Gemini, or leave empty for keyless replay
uv run aimo serve       # downloads the 17.5k catalog when AIMO_API_KEY is set, or serves the bundled 669-product replay snapshot on :8000
```

Without `AIMO_API_KEY` the download is skipped and `aimo serve` starts in replay mode on the committed snapshot and catalog subset (`/healthz` shows `replay.mode: "auto"`, `clock.mode: "replay"`, and responses carry `served_from: replay`), so `/ui`, the REST and MCP flows, `scenarios.run_all` (8/8 PASS, 88 checks), `conformance` (44/44), `qa_milk_flow.py`, and `uv run aimo demo` all work out of the box (`Dockerfile` copies `tests/fixtures/snapshot` into the image as well, so keyless Docker containers boot straight into replay mode on `:8000`). Only the live Valio upstream and full-catalog offline `pytest` cases (17,546 products) need `data/valio_aimo_junction26_product_data.json` (via `AIMO_API_KEY` + `uv run aimo fetch-data`).

Then open:

| URL | What |
|---|---|
| http://localhost:8000/ui | Approval UI: agent console, good-better-best cards, policy verdict, approval inbox, audit timeline (`?mock=1` plays the story with no backend) |
| http://localhost:8000/docs | OpenAPI 3.1 REST API (interactive) |
| http://localhost:8000/mcp | MCP server (streamable HTTP), 13 tools |
| http://localhost:8000/.well-known/otap.json | Discovery manifest: endpoints, MCP URL, JWKS URL, policies, standards |
| http://localhost:8000/.well-known/jwks.json | Public key to verify quote and audit signatures |
| http://localhost:8000/healthz | Which components are live vs fallback |
| [docs/arena/](arena/) (`docs/arena/index.html`) | **Interactive Judge Red-Team Game ("Try to Scam the Agent")**: static keyless pixel-art wholesale warehouse game (5 live guardrail scam attempts + 30 s happy-path order + scoreboard, backed by recorded gateway JWS/audit responses; `CC0-1.0` Kenney asset ledger in [docs/demo/ASSETS.md](demo/ASSETS.md)) |
| [docs/demo/](demo/) (`docs/demo/index.html`) | **One-Page Judge Walkthrough**: self-contained keyless interactive step-by-step walkthrough (Ed25519 JWS verify, 403 attack, 409 price-change requote, Peppol `AP`, and audit tamper check) |

Terminal demo of the buyer agent, with the gateway running in another terminal:
```bash
uv run aimo demo --auto-approve-as anna@hotel-aurora.example     # 1,000 L milk end to end (about 40 s with Gemini, ~6 s keyless over MCP)
uv run aimo demo --scenario stockout|school-allergen|injection|drift|event
uv run aimo demo --montage                                        # all 6 in sequence: 6/6 PASS, $0.035 per order (docs/demo-montage.md)
# the montage injects faults as the agent on purpose: start that gateway with AIMO_DEMO_FAULTS=1 (see docs/DEMO.md)
```
Verified Sat 14 Nov and Sun 15 Nov 02:54 (live Gemini, see [demo-montage.md](demo-montage.md)): parse -> search -> offers -> plan (cut-off on 463038, best 464306) -> escalation to gemini-4-hydrogen HIGH -> signed quote -> needs approval (3 reasons at the time; 2 with the final policy, which allows the hotel's listed pack swap) -> approved -> placed, 38 s ($0.0352 per order on the Sunday final-code re-record, $0.0339 after the Saturday token trim; 29 s on `gemini-3.5-flash` via `GEMINI_FALLBACK_MODELS` when `gemini-3.8-flash` hit daily quota at Sun 05:00). One complete live Gemini hero run recorded on Sun 15 Nov 05:38 (`run-b9690772fe`, 24.53 s wall clock, 8 Gemini calls: 7 `gemini-3.8-flash` + 1 `gemini-4-hydrogen` HIGH, signed quote `Q-20261115-964294b6` for EUR 1,010.00, placed order `ORD-20261115-06eda13d` `AP`, 6-event audit chain and `jwks.json`, captured via `AIMO_GEMINI_TRACE`) is committed in [evidence/live-run/README.md](evidence/live-run/README.md) and verifies offline with `uv run python docs/evidence/live-run/summarize.py` and `uv run python scripts/verify_quote.py docs/evidence/live-run/quote.json docs/evidence/live-run/jwks.json` (plus `audit-events.json`). Without `GEMINI_API_KEY` (and `GOOGLE_API_KEY`), `uv run aimo demo` finishes end to end via the deterministic regex parser and scripted buyer fallback over MCP (~6 s against a running gateway, or 2.6 s across all 6 montage scenarios with `--transport fake`).

On first start with `GEMINI_API_KEY` set, `aimo serve` builds the gemini-embedding-2 vectors and the Gemini allergen hints in the background (about 1.5 minutes; full-text search until done). `--build-sync` waits for them, `--no-build` skips them; `uv run python -m aimo.catalog build` builds them by hand.

Tests: `AIMO_OFFLINE=1 uv run --extra dev pytest -q` (or `make test`; `AIMO_OFFLINE=1 uv run --extra dev pytest --collect-only -q tests/` collects **5,861** tests in `tests/` on `fr8/valio/docs` `dd9599a`, up from `4,291` on `c3a7e6c`, `2,467` on `b5856cf`, `2,462` on `6343389`, `2,448` on `1d0b238`, and `2,393` on `450743d`). Clean full keyless offline gates: **1150 passed, 105 skipped, 1 xfailed, 1 xpassed** across 1,257 collected tests in `tests/` on `6973c2d` (`1,150 passed`) and **1,168 passed, 105 skipped, 1 xfailed, 1 xpassed** across 1,275 collected tests on `c98e72e` (0 failures without keys; `743 passed, 7 skipped, 2 xfailed` with the 87 MB full 17,546-product catalog JSON on `2468bdd` prior to the S2 fuzz, input-validation, buyer and doc-contract suites; standalone `main` `08a976c` / `62e8384` / `02575d8`: `375 passed, 7 skipped, 1 xfailed, 1 xpassed` keyed / `274 passed, 0 failed` keyless, `4ccc1fb` / `2fe8975`: `371 passed, 7 skipped` keyed / `270 passed, 0 failed` keyless, `d4b0b41`: `362 passed, 7 skipped` keyed / `261 passed, 0 failed` keyless). Without `data/valio_aimo_junction26_product_data.json`, full-catalog offline `pytest` cases skip cleanly instead of failing. Acceptance scenarios: `uv run python -m scenarios.run_all` (**8/8 PASS, 92 checks** on `fr8/valio`, 88 checks prior to `s5` expansion, both with the live mock API and keyless in replay mode). Conformance suite: `uv run python -m conformance` (**44/44** checks). Run `uv run aimo fetch-data` (needs `AIMO_API_KEY`) once, then re-run pytest to include the 105 full-catalog cases.

## Exceptions: what happens, and how to see it

Every row runs offline with no keys. The pytest commands need the dev extra; the `aimo demo` rows run on the scripted model in replay mode on a keyless clone (with `GEMINI_API_KEY` set they use Gemini instead).

| Exception | Scenario or test | One command | What you see |
|---|---|---|---|
| **Partial availability** | montage `stockout`; `tests/test_catalog.py::test_partial_stock_split_delivery_and_late_options` | `uv run aimo demo --scenario stockout` | `Verdict [stockout]: pending_approval; 4 line(s), EUR 2,544.80, shortfall 0 ... partial stock split across 464306 x400, 464201 x400, 464398 x400, 463779 x80; waiting for a human` |
| **Substitutions** | hero flow (s1); `tests/test_live_demo.py::test_verdict_shows_split_and_cutoff_substitution`, `tests/test_run_demo_hardening.py` | `uv run aimo demo --auto-approve-as anna@hotel-aurora.example` | The usual 463038 is past its cut-off, so the plan substitutes 464306 (same brand and spec, other pack), ranked good-better-best with a reason, and the policy asks the procurement manager to confirm (spend > EUR 500 and weekly volume drift; listed pack swap allowed under the spend limit on the final policy) |
| **Delivery constraints: cut-off** | `tests/test_gateway.py::test_offers_merge_cutoff_terminal_partial` | `uv run --extra dev pytest -q tests/test_gateway.py -k cutoff_terminal_partial` | Offers carry `issues: ["cutoff_passed"]` with the `closing_time`; the planner rejects the line with that reason |
| **Delivery constraints: late delivery** | `tests/test_catalog.py::test_late_available_date_is_rejected_not_silent` | `uv run --extra dev pytest -q tests/test_catalog.py -k late_available_date` | A product that can only arrive after the requested date is rejected with a reason, never planned silently |
| **Allergen safety** | montage `school-allergen`; `tests/test_trust.py::test_school_unknown_allergen_rejects_line_and_inferred_counts_as_unknown`, `tests/test_allergen_plan.py` | `uv run aimo demo --scenario school-allergen` | `Verdict [school-allergen]: placed; 1 line(s), EUR 280.00 ... excluded nuts, milk; allergen rule: [allow] All lines are confirmed free of milk or nuts by structured supplier data.; approved by policy:school-tampere; allergen probe (464306, cow's milk): rejected by policy (contains milk, which is excluded for this customer ...)`. Customer allergen hard-blocks are also enforced inside `plan_need`; unknown or Gemini-inferred "free" counts as unknown |
| **Price change** | `tests/test_need_policy.py::test_price_jump_over_15_percent_and_audit_log_last_paid_price` (`tests/test_need.py::test_price_jump_over_15_percent_needs_approval` on standalone `main`); `tests/test_edge_cases.py::test_price_change_inside_lock_is_honoured_after_lock_requote` | `uv run --extra dev pytest -q tests/test_need_policy.py tests/test_edge_cases.py -k "price_jump or price_change"` | A unit price more than 15% above the last price paid needs approval (`price_jump`); inside the 15-minute lock the signed price holds, after it a re-quote (`409 requote_needed`) is required |
| **Stock changing during approval** | `tests/test_gateway.py::test_partial_stock_at_approval_requotes_instead_of_silently_less` | `uv run --extra dev pytest -q tests/test_gateway.py -k partial_stock_at_approval` | Approval returns 409 `requote_needed` (reason `partial_stock`) with a fresh quote for the human; never a silently partial order |
| **Cut-off passing during approval** | `tests/test_gateway.py::test_cutoff_passing_while_waiting_requires_requote`, `test_expired_quote_needs_requote` | `uv run --extra dev pytest -q tests/test_gateway.py -k "cutoff_passing_while_waiting or expired_quote"` | The approval is refused with 409 `requote_needed` and a re-quote for the next feasible date; an expired price lock also needs a re-quote (`docs/pitch/requote_beat.py`) |
| **LLM quota exhausted** | `tests/test_gemini_fallback.py` | `uv run --extra dev pytest -q tests/test_gemini_fallback.py` | The next model in `GEMINI_FALLBACK_MODELS` (`gemini-3.5-flash`, then `gemini-4-hydrogen`) answers at once, the exhausted model is skipped for 30 min, and the ledger row names the answering model (`meta.fallback_from`); seen live on Sun 05:00 (29 s hero order) |
| **Prompt injection** | montage `injection`; `tests/test_overrides.py::test_injection_in_product_query_is_flagged`, `tests/test_plan_injection_screen.py` | `uv run aimo demo --scenario injection` | `Verdict [injection]: pending_approval; 1 line(s), EUR 303.00 ... injection flagged on 1 line(s), needs a human; ... nothing ordered`. Every `PlanLine` and `Candidate` carries `injection_screen`; the shipped regex screen has 44.4% recall on our 36-payload evaluation set (94.4% with opt-in `AIMO_SCREEN_GEMINI=1`, 0/34,159 false positives on real supplier texts); a flag only sends the order to a human |
| **Agent trying to move the clock or fake stock** | `tests/test_overrides.py`, `tests/test_run_api_guard.py`, `tests/test_faults_and_clock_bad_inputs.py` | `uv run --extra dev pytest -q tests/test_overrides.py tests/test_run_api_guard.py` | `X-Aimo-Now` / `X-Aimo-Faults` sent with agent identity, and the `faults` field or a caller-chosen `inproc`/`fake` test transport in a `POST /v1/agent/runs` body (`4ccc1fb`), are ignored unless the operator set `AIMO_DEMO_FAULTS=1`, and never in production; starting a run without a signed-in human (`X-Approver-Session` or `X-Approver-Pin`) gets 401 |

All acceptance scenarios at once: `uv run python -m scenarios.run_all` (8/8 PASS, 88 checks; replay without a key).

### Approving as a human

Approvals are human-only. Distinct 12-char passphrases are generated per directory approver into `data/keys/approver_pins.json` (mode `0600`, overridable via `AIMO_APPROVER_PINS`), with Anna's primary credential mirrored into `data/keys/approver_pin` for local CLI scripts (`cat data/keys/approver_pin` on the machine running the gateway; `AIMO_APPROVER_PIN` is kept as a single-credential override). One approver's passphrase cannot sign in as another (401), and no API route or MCP tool ever returns any credential; in production this is replaced by the customer's SSO.

- **In the UI:** clicking **Run agent** opens the sign-in panel first (only a signed-in human may instruct the hosted buying agent), and the same sign-in covers the approval later. You can also click **Sign in as approver** (header, or the "Approvals are human-only" card in the inbox). Pick yourself from the approver directory (the role the order needs is preselected, e.g. Anna Virtanen · Procurement Manager) and type the PIN in a masked field. The UI exchanges them once for a signed 8-hour session bound to that person and keeps only the session (per browser tab), never the PIN. The decision form then reads "Deciding as Anna Virtanen". A header pill counts blocked attacks (audited `approval.refused` and other refusals).
- **By voice (optional):** **Decide by voice** reads the order out (ElevenLabs if `ELEVENLABS_API_KEY` is set, else the browser's speech), transcribes the answer and classifies it with Gemini plus a rule safety net. Voice never decides: it shows the transcript and intent, and one confirm click sends the normal signed-in decision bound to the quote hash. `aimo/voice/`, [reports/ui.md](reports/ui.md).
- **Over REST:** `POST /v1/auth/approver-session` with `X-Approver-Pin` and `{"approver": "anna@hotel-aurora.example"}` returns `{"session": ...}`; send it as `X-Approver-Session` on decisions. A session cannot decide as anyone else (403 `approver_mismatch`).
- **Starting a hosted agent run is human-only too:** `POST /v1/agent/runs` (and answering or listing runs) needs `X-Approver-Session` or `X-Approver-Pin`; without it, 401 (`4ccc1fb` / `2fe8975`). Body `faults` and caller-chosen `inproc`/`fake` transports are ignored unless the gateway runs with `AIMO_DEMO_FAULTS=1` (and never with `AIMO_ENV=production`), and the response lists what was ignored. External agents connect over MCP (`/mcp`) or REST (`/v1`) with their own identity (`X-Customer-Id` + `X-Agent-Id`), as before.
- **Expired lock, price change or stock drop while waiting:** if the 15-min quote lock expires, price moves, or stock drops below the quoted quantity while waiting, `POST /v1/approvals/{id}/decision` returns `409 requote_needed` (e.g. reason `partial_stock`, never a silently partial order) with an old-vs-new diff, a fresh signed quote and pending order (`order.requoted` in audit; see [DEMO.md](DEMO.md)).
- **Agents can never approve:** any decision request carrying `X-Agent-Id` gets 403 `agent_cannot_approve`, even if it names a real approver; no or wrong credential gets 401. Every refusal is written to the audit log as `approval.refused`. Brute-force lockout is per `(approver_id, client)` (`5` wrong guesses -> 429 `approver_locked` for 5 min, `30` per 10 min per-approver backstop), so guessing one approver never locks out other approvers or active `X-Approver-Session` tokens.
- `uv run aimo demo --auto-approve-as ...` and `qa_milk_flow.py` read the operator's credential from the same env var or file, so they only work on the machine that runs the gateway.

## Repo map

| Path | What |
|---|---|
| `aimo/gateway/` | FastAPI app (`/v1`, `/.well-known`, `/ui`), MCP server (13 tools), shared service layer |
| `aimo/backend/` | Valio mock API client (cache, retry, chunking, fault injection), SQLite catalog store, order store with Peppol AP/CA/RE |
| `aimo/catalog/` | Search (FTS5 trigram + gemini-embedding-2, RRF), units, allergens (GS1 to EU14), good-better-best, `plan_need`, Gemini allergen and recipe checks |
| `aimo/trust/` | Policy engine and YAML policies, Ed25519 JWS signing, approvals, hash-chained signed audit, injection screening |
| `aimo/buyer/` | Gemini buyer agent + deterministic regex parser and scripted keyless fallback, vision intake, tool transports (MCP / in-process / fake), terminal demo, `POST /v1/agent/runs` + SSE |
| `aimo/voice/` | Voice approval assistant (`/v1/voice/*`, ElevenLabs TTS/STT with browser Web Speech fallback, Gemini intent + negation guard, one-click human confirmation bound to `quote_hash`) |
| `web/` | No-build approval UI served at `/ui`, with `?mock=1` offline mode |
| `windtunnel/` | Synthetic customers, snapshot replay with faults, oracle, scoring vs a naive baseline ([results](windtunnel-results.md)) |
| `aimo/gemini.py` | One Gemini client plus the usage ledger (`data/gemini/ledger/*.jsonl`) |
| `tests/` | 5,861 collected pytest cases on `fr8/valio/docs` `dd9599a` (up from 4,291 on `c3a7e6c`, 2,467 on `b5856cf`, 2,462 on `6343389`, 2,448 on `1d0b238`, and 1,257+ on `6973c2d`) across gateway, catalog, trust, buyer, voice, replay, `judge_accept` gate checks, per-approver and known-customer auth, input/quantity validation, `/v1` route fuzzing, docs hygiene, openness and scenarios; `tests/fixtures/snapshot/replay/` holds the 669-id recorded mock-API replay plus 669-product catalog subset (`scripts/record_replay.py` re-records it, needs `AIMO_API_KEY`) |
| `Dockerfile` | Slim Python 3.12 container image (port 8000, non-root `aimo` user, `/app/data` volume, `/healthz` check, copies `tests/fixtures/snapshot` so keyless containers boot straight into replay mode) |
| `scenarios/`, `conformance/`, `toywholesaler/` | Acceptance scenarios s0..s7 (`python -m scenarios.run_all`), the `otap/0.1` conformance suite (`python -m conformance`, 44/44), and second synthetic wholesaler (*Nordic Bakery Supply*) |
| `docs/arena/` | Interactive Judge Red-Team Game ("Try to Scam the Agent", [docs/arena/](arena/), `docs/arena/index.html`): static keyless pixel-art wholesale warehouse game backed by recorded gateway JWS/audit responses |
| `docs/demo/` | One-Page Judge Walkthrough ([docs/demo/](demo/), `docs/demo/index.html`) and `CC0-1.0` Kenney pixel-art asset ledger ([docs/demo/ASSETS.md](demo/ASSETS.md)) |
| `docs/evidence/` | Offline-verifiable evidence packs: sanitized Gemini montage ledgers (`montage_stats.py`, [evidence/README.md](evidence/README.md)), raw live Gemini hero run + signed quote/audit/JWKS (`summarize.py`, [evidence/live-run/README.md](evidence/live-run/README.md)), and committed 640-scenario wind-tunnel inputs + runs ([evidence/windtunnel/README.md](evidence/windtunnel/README.md)) |
| `docs/` | [CONNECT](CONNECT.md), [OPENNESS](OPENNESS.md), [COMBINE](COMBINE.md), [ASSUMPTIONS](ASSUMPTIONS.md), [DECISIONS](DECISIONS.md), [NUMBERS](NUMBERS.md), [TRUST](TRUST.md), [ARCHITECTURE](ARCHITECTURE.md), [GEMINI](GEMINI.md), [DEMO](DEMO.md), [SUBMISSION](SUBMISSION.md), [QA](pitch/QA.md), [interfaces](interfaces.md), [PLAN](PLAN.md), [ASSETS](demo/ASSETS.md) |

Data: the challenge files (product JSON, delivery reliability CSV, CO2 CSV) download into `data/`, which is gitignored together with `.env`, signing keys and the SQLite state.

## Getting the code

The repository is public:

```bash
git clone https://github.com/s1laswang/junction-aimo-agents.git aimo && cd aimo
cp .env.example .env && make accept      # keyless acceptance; add keys to .env for the live mock API and Gemini
```

The judged version is the tag **`aimo-v1`**, created at the code freeze (Sun 15 Nov 06:00 Helsinki). To review exactly that: `git checkout aimo-v1`. Do not confuse this repo with AfterQuery Canary/Sleeper (`s1laswang/agent-arena`) or HoldUp (`s1laswang/junction-meta-glasses`).
