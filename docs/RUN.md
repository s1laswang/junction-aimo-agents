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
| `GEMINI_API_KEY` (alias: `GOOGLE_API_KEY`) | live Gemini buyer in `uv run aimo demo`, embeddings / allergen hints, wind-tunnel live runs | `uv run aimo demo` finishes end to end via the deterministic regex parser and scripted buyer fallback over MCP (and `make accept` runs it keyless); `/ui` runs on a deterministic mock model; `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` runs the REST milk flow with no LLM; `aimo serve --no-build` skips Gemini catalog builds. |
| `AIMO_APPROVER_PINS` / `AIMO_APPROVER_PIN` | **human approver credentials** (see [Approving as a human](#approving-as-a-human)): per-approver JSON/CSV map (`AIMO_APPROVER_PINS`), with `AIMO_APPROVER_PIN` kept as single-credential override | Distinct 12-char passphrases are generated per directory approver into `data/keys/approver_pins.json` (mode `0600`, overridable via `AIMO_APPROVER_PINS`), with Anna's primary credential (`anna@hotel-aurora.example`) mirrored into `data/keys/approver_pin` for local CLI scripts (`AIMO_APPROVER_PIN` kept as single-credential override). Brute-force lockout is per `(approver_id, client)` (`5` wrong guesses -> 429 `approver_locked` for 5 min, `30` per 10 min per-approver backstop). No API route or MCP tool ever returns any credential. |
| `AIMO_REPLAY` | `=1` (or `=tests/fixtures/snapshot/replay`, or `=tests/fixtures/snapshot`) forces replay from the bundled snapshot even with a key (no network for prices and stock); `=<dir>` uses another snapshot; `=0` turns automatic keyless replay off | Unset with `AIMO_API_KEY`: live mock API (with automatic fallback to `tests/fixtures/snapshot/replay` if upstream is unreachable). Unset without `AIMO_API_KEY`: automatic replay from `tests/fixtures/snapshot/replay` (669 product ids + 669-product catalog subset across `2026-11-16`..`2026-11-20`). Replayed responses say `served_from: replay` and the UI header shows a REPLAY pill. |
| `ELEVENLABS_API_KEY` | optional: ElevenLabs voice for **Decide by voice** | The browser's own speech synthesis and recognition are used instead. |
| `AIMO_NOW` | simulated clock, e.g. `AIMO_NOW=2026-11-15T10:00:00+02:00` (add `AIMO_NOW_FROZEN=1` to stop it running forward) | Real clock. Replay mode anchors the clock at the active snapshot's recording time (`2026-11-14T23:50:28+0200` for `tests/fixtures/snapshot/replay`, `2026-11-14T14:20:06+0200` for `tests/fixtures/snapshot`) unless `AIMO_NOW` is set. |

Do not put real values in the README. A judge machine with none of these variables set (an empty `.env`, no `AIMO_API_KEY`, no `GEMINI_API_KEY`) runs `make accept` end to end in replay mode (`RESULT: PASS`: `scenarios.run_all` 8/8 PASS in replay, `conformance` 44/44, `qa_milk_flow.py` places an order answered with Peppol `AP` and verified audit chain, and `aimo demo` finishes via the scripted buyer over MCP; full-catalog offline `pytest` cases skip cleanly when `data/valio_aimo_junction26_product_data.json` is absent).

One-command check:

```bash
cp .env.example .env    # fill keys if you have them
make accept             # or: uv run bash scripts/judge_accept.sh
```

`make accept` runs pytest, runs `scenarios.run_all` (8/8 PASS, live when keyed or off `tests/fixtures/snapshot/replay` when keyless), starts the gateway on a free port with a fresh approver credential, checks the discovery and docs endpoints, runs the conformance suite (44/44), `qa_milk_flow.py` (search -> offers -> plan -> signed quote -> pending approval -> wrong-hash 409 -> human approval -> Peppol AP -> audit verify), `requote_beat.py` when present (`409 requote_needed` on an expired price-moved quote -> replacement signed quote -> human approval -> Peppol AP + audit verify), and (when keyless) `uv run aimo demo` via the scripted buyer. It prints the placed order id, its Peppol `OrderResponseCode` and the audit verify result, then a PASS/FAIL/SKIP table ending in `RESULT: PASS` or `RESULT: FAIL`.

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

Terminal demo of the buyer agent, with the gateway running in another terminal:
```bash
uv run aimo demo --auto-approve-as anna@hotel-aurora.example     # 1,000 L milk end to end (about 40 s with Gemini, ~6 s keyless over MCP)
uv run aimo demo --scenario stockout|school-allergen|injection|drift|event
uv run aimo demo --montage                                        # all 6 in sequence: 6/6 PASS, $0.034 per order (docs/demo-montage.md)
```
Verified Sat 14 Nov (live Gemini, see [demo-montage.md](demo-montage.md)): parse -> search -> offers -> plan (cut-off on 463038, best 464306) -> escalation to gemini-4-hydrogen HIGH -> signed quote -> needs approval (3 reasons) -> approved -> placed, 38 s. Without `GEMINI_API_KEY` (and `GOOGLE_API_KEY`), `uv run aimo demo` finishes end to end via the deterministic regex parser and scripted buyer fallback over MCP (~6 s against a running gateway, or 2.6 s across all 6 montage scenarios with `--transport fake`).

On first start with `GEMINI_API_KEY` set, `aimo serve` builds the gemini-embedding-2 vectors and the Gemini allergen hints in the background (about 1.5 minutes; full-text search until done). `--build-sync` waits for them, `--no-build` skips them; `uv run python -m aimo.catalog build` builds them by hand.

Tests: `AIMO_OFFLINE=1 uv run --extra dev pytest -q` (or `make test`). Result: **1150 passed, 105 skipped, 1 xfailed, 1 xpassed** keyless offline across 1,257 collected tests in `tests/` on `6973c2d` (0 failures without keys; `743 passed, 7 skipped, 2 xfailed` with the 87 MB full 17,546-product catalog JSON on `2468bdd` prior to the S2 fuzz, input-validation, buyer and doc-contract suites; standalone `main` `2a68792` / `738c93a`: `344 passed, 7 skipped` keyed). Without `data/valio_aimo_junction26_product_data.json`, full-catalog offline `pytest` cases skip cleanly instead of failing. Acceptance scenarios: `uv run python -m scenarios.run_all` (**8/8 PASS, 88 checks** both with the live mock API and keyless in replay mode). Conformance suite: `uv run python -m conformance` (**44/44** checks). Run `uv run aimo fetch-data` (needs `AIMO_API_KEY`) once, then re-run pytest to include the 105 full-catalog cases.

### Approving as a human

Approvals are human-only. Distinct 12-char passphrases are generated per directory approver into `data/keys/approver_pins.json` (mode `0600`, overridable via `AIMO_APPROVER_PINS`), with Anna's primary credential mirrored into `data/keys/approver_pin` for local CLI scripts (`cat data/keys/approver_pin` on the machine running the gateway; `AIMO_APPROVER_PIN` is kept as a single-credential override). One approver's passphrase cannot sign in as another (401), and no API route or MCP tool ever returns any credential; in production this is replaced by the customer's SSO.

- **In the UI:** click **Sign in as approver** (header, or the "Approvals are human-only" card in the inbox). Pick yourself from the approver directory (the role the order needs is preselected, e.g. Anna Virtanen · Procurement Manager) and type the PIN in a masked field. The UI exchanges them once for a signed 8-hour session bound to that person and keeps only the session (per browser tab), never the PIN. The decision form then reads "Deciding as Anna Virtanen". A header pill counts blocked attacks (audited `approval.refused` and other refusals).
- **By voice (optional):** **Decide by voice** reads the order out (ElevenLabs if `ELEVENLABS_API_KEY` is set, else the browser's speech), transcribes the answer and classifies it with Gemini plus a rule safety net. Voice never decides: it shows the transcript and intent, and one confirm click sends the normal signed-in decision bound to the quote hash. `aimo/voice/`, [reports/ui.md](reports/ui.md).
- **Over REST:** `POST /v1/auth/approver-session` with `X-Approver-Pin` and `{"approver": "anna@hotel-aurora.example"}` returns `{"session": ...}`; send it as `X-Approver-Session` on decisions. A session cannot decide as anyone else (403 `approver_mismatch`).
- **Expired lock or price change while waiting:** if the 15-min quote lock expires or price moves while waiting, `POST /v1/approvals/{id}/decision` returns `409 requote_needed` with an old-vs-new diff, a fresh signed quote and pending order (`order.requoted` in audit; see [DEMO.md](DEMO.md)).
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
| `tests/` | 1,257+ collected pytest cases across gateway, catalog, trust, buyer, voice, replay, `judge_accept` gate checks, per-approver and known-customer auth, input/quantity validation, `/v1` route fuzzing, docs hygiene, openness and scenarios; `tests/fixtures/snapshot/replay/` holds the 669-id recorded mock-API replay plus 669-product catalog subset (`scripts/record_replay.py` re-records it, needs `AIMO_API_KEY`) |
| `Dockerfile` | Slim Python 3.12 container image (port 8000, non-root `aimo` user, `/app/data` volume, `/healthz` check, copies `tests/fixtures/snapshot` so keyless containers boot straight into replay mode) |
| `scenarios/`, `conformance/`, `toywholesaler/` | Acceptance scenarios s0..s7 (`python -m scenarios.run_all`), the `otap/0.1` conformance suite (`python -m conformance`, 44/44), and second synthetic wholesaler (*Nordic Bakery Supply*) |
| `docs/evidence/` | Sanitized Gemini ledgers behind the montage cost numbers (`montage_stats.py`) |
| `docs/` | [CONNECT](CONNECT.md), [OPENNESS](OPENNESS.md), [COMBINE](COMBINE.md), [ASSUMPTIONS](ASSUMPTIONS.md), [DECISIONS](DECISIONS.md), [NUMBERS](NUMBERS.md), [TRUST](TRUST.md), [ARCHITECTURE](ARCHITECTURE.md), [GEMINI](GEMINI.md), [DEMO](DEMO.md), [SUBMISSION](SUBMISSION.md), [QA](pitch/QA.md), [interfaces](interfaces.md), [PLAN](PLAN.md) |

Data: the challenge files (product JSON, delivery reliability CSV, CO2 CSV) download into `data/`, which is gitignored together with `.env`, signing keys and the SQLite state.

## Getting the code

The repository is public:

```bash
git clone https://github.com/s1laswang/junction-aimo-agents.git aimo && cd aimo
cp .env.example .env && make accept      # keyless acceptance; add keys to .env for the live mock API and Gemini
```

The judged version is the tag **`aimo-v1`**, created at the code freeze (Sun 15 Nov 06:00 Helsinki). To review exactly that: `git checkout aimo-v1`. Do not confuse this repo with AfterQuery Canary/Sleeper (`s1laswang/agent-arena`) or HoldUp (`s1laswang/junction-meta-glasses`).
