# Running Trusted Trade Agents

Everything needed to run, test and approve locally. Back to the [README](../README.md).

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| Python 3.11+ | runtime (`requires-python = ">=3.11"`) | system package or uv-managed |
| [uv](https://docs.astral.sh/uv/) | one-command runner and lockfile installer | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Network | first-run download of the challenge product JSON from the Valio mock API | — |

Environment variables (names only; copy `.env.example` to `.env` and fill them in — never commit `.env`):

| Variable | Required for | If missing |
|---|---|---|
| `AIMO_API_KEY` | live Valio Aimo mock API, and `aimo serve` / `aimo fetch-data` downloading the full catalog `data/valio_aimo_junction26_product_data.json` (and the reliability / CO2 CSVs) | **Replay mode, automatically:** prices and stock come from the recorded snapshot and the catalog from a committed 669-product subset, so the gateway, UI, scenarios, conformance and milk flow all run. Tests that need the full catalog are marked `needs_data` and skip (keyless: 261 passed, 0 failed). |
| `AIMO_PORTAL_URL`, `AIMO_PORTAL_PASSWORD` | portal login to obtain a mock-API key if you do not already have `AIMO_API_KEY` | leave blank if you already have a key |
| `GEMINI_API_KEY` | `uv run aimo demo` (Gemini buyer), embeddings / allergen hints, wind-tunnel live runs | The UI's agent runs on a deterministic mock model; `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000` runs the flow with no LLM. `aimo serve --no-build` skips Gemini catalog builds. |
| `AIMO_APPROVER_PIN` | the **human approver credential** (see [Approving as a human](#approving-as-a-human)) | Generated once into `data/keys/approver_pin` (a random 12-character passphrase). No API route or MCP tool ever returns it. |
| `AIMO_REPLAY` | `=1` forces replay from the bundled snapshot even with a key (no network for prices and stock); `=<dir>` uses another snapshot; `=0` turns the automatic keyless replay off | Unset: live with a key, replay without one. Replayed responses say `served_from: replay` and the UI header shows a REPLAY pill. |
| `ELEVENLABS_API_KEY` | optional: ElevenLabs voice for **Decide by voice** | The browser's own speech synthesis and recognition are used instead. |
| `AIMO_NOW` | simulated clock, e.g. `AIMO_NOW=2026-11-15T10:00:00+02:00` (add `AIMO_NOW_FROZEN=1` to stop it running forward) | Real clock. Replay mode anchors the clock at the snapshot's recording time (Sat 14 Nov 23:50 Helsinki) unless `AIMO_NOW` is set. |

Do not put real values in the README. A judge machine with none of these variables set runs `make accept` end to end in replay mode: on a fresh clone with an empty `.env` it gives `RESULT: PASS` (PASS=17 FAIL=0 SKIP=3, Sun 15 Nov 05:10 on d4b0b41: keyless pytest 261 passed / 0 failed, scenarios 8/8 in replay, conformance 44/44, the milk flow places an order answered with Peppol AP, audit verify ok). The three skips are the two key checks and the live Gemini demo.

One-command check:

```bash
cp .env.example .env    # fill keys if you have them
make accept             # or: uv run bash scripts/judge_accept.sh
```

`make accept` runs pytest, starts the gateway on a free port with a fresh approver credential, checks the discovery and docs endpoints, then the acceptance scenarios s0–s7, the conformance suite and `qa_milk_flow.py` (search → offers → plan → signed quote → pending approval → wrong-hash 409 → human approval → Peppol AP → audit verify), live with a key or in replay without one. It prints the placed order id, its Peppol `OrderResponseCode` and the audit verify result, then a PASS/FAIL/SKIP table ending in `RESULT: PASS` or `RESULT: FAIL`.

### Keyless vs keyed

| | Keyless (empty `.env`) | Keyed (`AIMO_API_KEY`, `GEMINI_API_KEY`) |
|---|---|---|
| Price and availability | Replay of recorded mock-API responses (`tests/fixtures/snapshot/replay/`: 669 products, delivery dates 16–20 Nov, recorded Sat 14 Nov 23:50 Helsinki with 10 upstream calls). Dates that were not recorded are refused, never invented. | Live Valio Aimo mock API with a 5-minute cache; if the upstream is unreachable after retries it falls back to the bundled snapshot (`served_from: replay`) |
| Clock | Anchored at the snapshot's recording time, so the story's Monday cut-offs still hold | Real time, or `AIMO_NOW` |
| Catalog | Committed subset of the public challenge records for the 669 snapshot products, unpacked on first start. Search covers only those. With no catalog at all, search and plan answer 503 `catalog_missing` ("run `uv run aimo fetch-data`"), never a 500. | Full 17,546 products, downloaded on first `aimo serve` |
| Buyer agent | `/ui` with a deterministic mock model (the hero request reaches the approval wait), `qa_milk_flow.py` (scripted, no LLM) or `/ui?mock=1` (no backend) | `/ui` or `uv run aimo demo` with Gemini |
| Search | Full-text (FTS5 trigram) | Full-text plus gemini-embedding-2 |

## Quickstart details

Requirements: see Prerequisites.

```bash
cp .env.example .env    # fill AIMO_PORTAL_URL, AIMO_PORTAL_PASSWORD, AIMO_API_KEY, GEMINI_API_KEY
uv run aimo serve       # downloads the challenge data on first run (needs AIMO_API_KEY), builds the catalog, serves on :8000
```

Without `AIMO_API_KEY` the download is skipped and `aimo serve` starts in replay mode on the committed snapshot and catalog subset (`/healthz` shows `"replay": {"mode": "auto"}`), so `/ui`, the REST and MCP flows and the milk-flow script all work. The full 17,546-product catalog and the catalog-backed tests need the key.

Then open:

| URL | What |
|---|---|
| http://localhost:8000/ui | Approval UI: agent console, good-better-best cards, policy verdict, approval inbox, audit timeline (`?mock=1` plays the story with no backend) |
| http://localhost:8000/docs | OpenAPI 3.1 REST API (interactive) |
| http://localhost:8000/mcp | MCP server (streamable HTTP), 13 tools |
| http://localhost:8000/.well-known/otap.json | Discovery manifest: endpoints, MCP URL, JWKS URL, policies, standards |
| http://localhost:8000/.well-known/jwks.json | Public key to verify quote and audit signatures |
| http://localhost:8000/healthz | Which components are live vs fallback |

Terminal demo of the Gemini buyer agent, with the gateway running in another terminal:
```bash
uv run aimo demo --auto-approve-as anna@hotel-aurora.example     # 1,000 L milk end to end, about 40 s
uv run aimo demo --scenario stockout|school-allergen|injection|drift|event
uv run aimo demo --montage                                        # all 6 in sequence: 6/6 PASS, $0.035 per order (docs/demo-montage.md)
# the montage injects faults as the agent on purpose: start that gateway with AIMO_DEMO_FAULTS=1 (see docs/DEMO.md)
```
Verified Sat 14 Nov: parse → search → offers → plan (cut-off on 463038, best 464306) → escalation to gemini-4-hydrogen HIGH → signed quote → needs approval (3 reasons) → approved → placed, 38 s.

On first start with `GEMINI_API_KEY` set, `aimo serve` builds the gemini-embedding-2 vectors and the Gemini allergen hints in the background (about 1.5 minutes; full-text search until done). `--build-sync` waits for them, `--no-build` skips them; `uv run python -m aimo.catalog build` builds them by hand.

Tests: `uv run --extra dev pytest -q`. With the challenge product JSON: **362 passed, 7 skipped, 1 xfailed, 1 xpassed** (main at d4b0b41, Sun 15 Nov 05:10). Without that file, catalog-backed tests skip instead of failing. Acceptance scenarios: `uv run python -m scenarios.run_all` (8/8 with the live mock API). Run `uv run aimo fetch-data` (needs `AIMO_API_KEY`) once, then re-run pytest for the catalog-backed count.

### Approving as a human

Approvals are human-only. The approver credential is `AIMO_APPROVER_PIN` from `.env`, or, if that is empty, the passphrase the gateway generated into `data/keys/approver_pin` on first start (`cat data/keys/approver_pin` on the machine running the gateway). No API route and no MCP tool ever returns it; in production it is replaced by the customer's SSO. That file is Anna's (anna@hotel-aurora.example) credential; without `AIMO_APPROVER_PIN` every other approver has their own passphrase in `data/keys/approver_pins.json`, and a credential only signs in or decides as its own approver.

- **In the UI:** click **Sign in as approver** (header, or the "Approvals are human-only" card in the inbox). Pick yourself from the approver directory (the role the order needs is preselected, e.g. Anna Virtanen · Procurement Manager) and type the PIN in a masked field. The UI exchanges them once for a signed 8-hour session bound to that person and keeps only the session (per browser tab), never the PIN. The decision form then reads "Deciding as Anna Virtanen". A header pill counts blocked attacks (audited `approval.refused` and other refusals).
- **By voice (optional):** **Decide by voice** reads the order out (ElevenLabs if `ELEVENLABS_API_KEY` is set, else the browser's speech), transcribes the answer and classifies it with Gemini plus a rule safety net. Voice never decides: it shows the transcript and intent, and one confirm click sends the normal signed-in decision bound to the quote hash. `aimo/voice/`, docs/reports/ui.md.
- **Over REST:** `POST /v1/auth/approver-session` with `X-Approver-Pin` and `{"approver": "anna@hotel-aurora.example"}` returns `{"session": ...}`; send it as `X-Approver-Session` on decisions. A session cannot decide as anyone else (403 `approver_mismatch`).
- **Agents can never approve:** any decision request carrying `X-Agent-Id` gets 403 `agent_cannot_approve`, even if it names a real approver; no credential gets 401. Every refusal is written to the audit log as `approval.refused`. Five wrong credentials from one client lock that client for 5 minutes (429); signed-in sessions are never locked out.
- `uv run aimo demo --auto-approve-as …` and `qa_milk_flow.py` read the operator's credential from the same env var or file, so they only work on the machine that runs the gateway.


## Repo map

| Path | What |
|---|---|
| `aimo/gateway/` | FastAPI app (`/v1`, `/.well-known`, `/ui`), MCP server, shared service layer |
| `aimo/backend/` | Valio mock API client (cache, retry, chunking, fault injection), SQLite catalog store, order store with Peppol AP/CA/RE |
| `aimo/catalog/` | Search (FTS5 trigram + gemini-embedding-2, RRF), units, allergens (GS1 to EU14), good-better-best, `plan_need`, Gemini allergen and recipe checks |
| `aimo/trust/` | Policy engine and YAML policies, Ed25519 JWS signing, approvals, hash-chained signed audit, injection screening |
| `aimo/buyer/` | Gemini buyer agent, need parsing, tool transports (MCP / in-process / fake), terminal demo, `POST /v1/agent/runs` + SSE |
| `web/` | No-build approval UI served at `/ui`, with `?mock=1` offline mode |
| `windtunnel/` | Synthetic customers, snapshot replay with faults, oracle, scoring vs a naive baseline ([results](windtunnel-results.md)) |
| `aimo/gemini.py` | One Gemini client plus the usage ledger (`data/gemini/ledger/*.jsonl`) |
| `tests/` | Offline tests for gateway, catalog, trust, buyer, human-only approval, fuzzing of every `/v1` route, edge cases and quantity validation; `tests/fixtures/snapshot/replay/` is the recorded mock-API replay plus catalog subset (`scripts/record_replay.py` re-records it, needs the key) |
| `scenarios/`, `conformance/` | Acceptance scenarios s0–s7 (`python -m scenarios.run_all`) and the `otap/0.1` conformance suite |
| `docs/evidence/` | Sanitized Gemini ledgers behind the montage cost numbers (`montage_stats.py`) |
| `docs/` | [ASSUMPTIONS](ASSUMPTIONS.md), [DECISIONS](DECISIONS.md), [NUMBERS](NUMBERS.md), [TRUST](TRUST.md), [ARCHITECTURE](ARCHITECTURE.md), [GEMINI](GEMINI.md), [DEMO](DEMO.md), [SUBMISSION](SUBMISSION.md), [interfaces](interfaces.md), [PLAN](PLAN.md) |

Data: the challenge files (product JSON, delivery reliability CSV, CO2 CSV) download into `data/`, which is gitignored together with `.env`, signing keys and the SQLite state.

## Getting the code

The repository is public:

```bash
git clone https://github.com/s1laswang/junction-aimo-agents.git aimo && cd aimo
cp .env.example .env && make accept      # keyless acceptance; add keys to .env for the live mock API and Gemini
```

The judged version is the tag **`aimo-v1`**, created at the code freeze (Sun 15 Nov 06:00 Helsinki). To review exactly that: `git checkout aimo-v1`. Do not confuse this repo with AfterQuery Canary/Sleeper (`s1laswang/agent-arena`) or HoldUp (`s1laswang/junction-meta-glasses`).

