# Stage and Operator Runbook

Operational runbook for preparing, running, and recovering the AIMO Trusted Trade Gateway on stage and in local or container deployments. Pair this guide with the click-by-click [stage demo script](../pitch/DEMO.md), [MCP client and Docker setup](mcp-clients.md), and [public URL plan](PUBLIC_URL.md).

## Pre-Stage T-15 Min Checklist

Run these five steps in order 15 minutes before going on stage. Never run an uncached scenario for the first time live.

### 1. Link `data/` and `.env` (T-15 min)

Link the Junction dataset directory and upstream Valio credentials into the repository root, then verify both symlinks resolve:

```sh
ln -s ~/aimo-agents/data data
ln -s ~/aimo-agents/.env .env
make check
```

- `data/` must contain `valio_aimo_junction26_product_data.json`, `valio_aimo_junction26_delivery_reliability.csv`, and `valio_aimo_junction26_carbon_footprint.csv`.
- `.env` must define `VALIO_API_BASE` and `VALIO_API_KEY` (`aimo/app.py` and `aimo/valio.py` load `.env` automatically via `python-dotenv`). No LLM keys are needed for deterministic stage runs.

### 2. Offline readiness check: `make smoke` (T-12 min)

Verify the application, routes, auth boundaries, MCP server, rule parser, hash-chained audit log, and Ed25519 checkpoint signing in-process before starting any server:

```sh
make smoke
```

`make smoke` runs `.venv/bin/python scripts/smoke_demo.py --inprocess` ([../scripts/smoke_demo.py](../scripts/smoke_demo.py)) against FastAPI `TestClient` with isolated temporary `AIMO_DB` and `AIMO_KEY_DIR` directories (`LLM_*` and `AIMO_AGENT_KEYS` cleared). Confirm `Summary: PASS (6/6 checks ..., mode=inprocess)`:

1. `/health`: `GET /health` returns HTTP `200` with `{"ok": true}`.
2. `Discovery`: `/.well-known/agent-card.json` lists all 12 tools, `/.well-known/jwks.json` serves an `EdDSA` (`Ed25519`) public key with no private `"d"` field, and `/openapi.json` exposes `/v1/*` routes.
3. `UI views`: `/`, `/console.html`, `/inbox.html`, `/audit.html`, and `/admin.html` all return HTTP `200` with non-empty HTML.
4. `Mandate auth /v1/mandate`: unauthenticated `GET /v1/mandate` returns `401`, and `X-Agent-Key: demo-hotel-aurora` returns `200` (`customer=hotel-aurora`, `per_order_eur=2500.0`).
5. `MCP /mcp`: streamable HTTP `initialize`, `notifications/initialized`, `tools/list` (12 tools), and `tools/call` `get_mandate` (`per_order_eur=2500.0`) succeed.
6. `Parse + Audit + Checkpoint`: `POST /v1/needs/parse` records a `parse.request` event, `GET /v1/audit` reports `verified.ok=True`, and `checkpoint.jws` verifies cryptographically against `/.well-known/jwks.json`.

### 3. Warm cache and boot clean stage server: `make demo` (T-10 min)

Start the full pre-stage pipeline in Terminal 1:

```sh
make demo
```

Defined in [../Makefile](../Makefile), `make demo` executes `setup`, `scen`, and `stage` sequentially:

1. `setup`: runs `check`, creates `.venv` if missing, and installs `requirements.txt`.
2. `scen`: runs `.venv/bin/python scenarios/run_all.py --base http://127.0.0.1:8081 --boot` on `SCEN_PORT` (`PORT+1`, so `8081` when `PORT=8080`). Each of the 8 scenarios (`s0_guardrails` through `s7_drift`) boots an isolated gateway with a temporary `AIMO_DB` while sharing `var/cache`, warming `var/cache/price__*.json` and `var/cache/availability__*.json` so live stage calls hit local cache first.
3. `stage`: deletes `var/demo.db` (`rm -f var/demo.db`) so test orders from `scen` do not appear in the UI, boots `AIMO_DB=var/demo.db .venv/bin/python -m uvicorn aimo.app:app --port 8080` (logging to `var-demo.log`), polls `http://localhost:8080/.well-known/agent-card.json` for up to 30 seconds, prints the UI URLs, and stays in the foreground until `Ctrl-C`.

### 4. Live HTTP readiness check: `6/6` against port `8080` (T-5 min)

In Terminal 2, run `scripts/smoke_demo.py` over real HTTP against the running stage server:

```sh
.venv/bin/python scripts/smoke_demo.py --url http://localhost:8080
```

Confirm `Summary: PASS (6/6 checks ..., mode=http://localhost:8080)`. Note that check 6 appends one `parse.request` event for `hotel-aurora` to `var/demo.db`; if you want a zero-event audit ledger at curtain rise, run `make stage` once after this check.

### 5. Open and verify operator UI tabs (T-3 min)

Open these four tabs on `http://localhost:8080` (plus `/` if showing the overview landing page):

- `http://localhost:8080/console.html` - Buyer agent console (`http://localhost:8080/console.html?demo=1` enables the inline stock-out simulation controls for Scene 4).
- `http://localhost:8080/inbox.html` - Human approvals inbox (polls `/v1/approvals` every 3 seconds; approvers match customer mandates).
- `http://localhost:8080/audit.html` - Hash-chained audit ledger, plain-language receipt view, in-browser SHA-256 verification, and signed checkpoint JWS.
- `http://localhost:8080/admin.html` - Customer mandates editor (`hotel-aurora`, `hospital-tays`, `school-hervanta`) and stock-out simulation controls.

## Environment Variables Reference

`aimo/app.py` and `aimo/valio.py` call `dotenv.load_dotenv()`, reading `.env` from the current working directory if present (existing process environment variables take precedence).

### Gateway, Agents, Makefile, and Container Variables

| Variable | Default Value | Container / Make Override | Where Read | Purpose |
|---|---|---|---|---|
| `PORT` | `8080` | `SCEN_PORT` uses `PORT+1` (`8081`); `Dockerfile` exposes `8080` | [../Makefile](../Makefile), [../Dockerfile](../Dockerfile) | TCP port bound by `uvicorn aimo.app:app` and targeted by `make scenarios` and `make stage`. |
| `AIMO_DATA_DIR` | `"data"` | `/app/data` in [../Dockerfile](../Dockerfile) | `aimo/catalog.py`, `aimo/allocate.py`, `pitch/measure_data.py`, `scripts/eval_precision.py`, `scenarios/_common.py` | Directory holding `valio_aimo_junction26_product_data.json`, `valio_aimo_junction26_delivery_reliability.csv`, and `valio_aimo_junction26_carbon_footprint.csv`. |
| `AIMO_DB` | `"var/aimo.db"` | `var/demo.db` in `make run` and `make stage`; `/app/var/aimo.db` in [../Dockerfile](../Dockerfile) | `aimo/db.py` | SQLite file storing customer mandates, single-use quotes, orders, the SHA-256 hash-chained audit table, and the FTS5 catalog index. |
| `AIMO_KEY_DIR` | `"var/keys"` | `/app/var/keys` in [../Dockerfile](../Dockerfile) | `aimo/keys.py` | Directory storing the active Ed25519 JWK (`quote-ed25519.json`, mode `0600`) and rotated public keys (`retired-<kid>.json`). |
| `AIMO_CACHE_DIR` | `"var/cache"` | `/app/var/cache` in [../Dockerfile](../Dockerfile) | `aimo/valio.py` | Directory for per-customer, per-date Valio price and availability cache files (`price__<customer>__<YYYY-MM-DD>.json`, `availability__<customer>__<YYYY-MM-DD>.json`) and stale-on-error fallback. |
| `AIMO_ADMIN_KEY` | `"demo-admin"` | - | `aimo/api.py`, `scenarios/_common.py` | Secret required in `X-Admin-Key` for `POST /v1/approvals/{order_id}`, `PUT /v1/mandate`, `POST /v1/admin/simulate`, `DELETE /v1/admin/simulate`, and cross-customer admin reads. |
| `AIMO_AGENT_KEYS` | unset (`None`), falls back to `_DEMO_KEYS` | - | `aimo/mandates.py` | JSON object mapping `X-Agent-Key` strings to `["<customer>", "<agent_id>"]` pairs. Unset or invalid JSON falls back to `demo-hotel-aurora`, `demo-hospital-tays`, and `demo-school-hervanta`. |
| `AIMO_AGENT_KEY` | unset (`None`) | `agents/drift.py` falls back to `"demo-" + customer` | `aimo/mcp_server.py`, `agents/drift.py`, `agents/buyer.py` (stdio subprocess env) | Buyer agent key used by MCP stdio transport (`python -m aimo.mcp_server`) when HTTP headers are absent, and default `--key` in `agents/drift.py`. |
| `AIMO_STDIO_ADMIN_KEY` | unset (`None`) | - | `aimo/mcp_server.py` | Admin key used by MCP stdio transport (`python -m aimo.mcp_server`) for admin-scoped tool calls such as `decide_approval`. |
| `AIMO_URL` | `"http://localhost:8080"` (`agents/seller.py`), `"http://127.0.0.1:8080"` (`agents/drift.py`, `scenarios/`) | `scenarios/run_all.py` sets `AIMO_URL` from `--base` | `agents/seller.py`, `agents/drift.py`, `scenarios/_common.py`, `scenarios/run_all.py`, `scenarios/s5_allergen_safe.py` | Base gateway URL for external agent CLIs and scenario scripts. |
| `AIMO_DRIFT_STATE` | `"var/drift.json"` | - | `agents/drift.py` | JSON state file storing standing-order price baselines and watched stock-out replacement order IDs. |
| `VALIO_API_BASE` | `""` (empty string) | Loaded from `.env` | `aimo/valio.py` | Upstream Valio price and availability API base URL. Raises `UpstreamUnavailable` (HTTP `503`) on uncached product lookups if empty. |
| `VALIO_API_KEY` | `""` (empty string) | Loaded from `.env` | `aimo/valio.py` | Upstream Valio API key sent as `X-API-Key`. Raises `UpstreamUnavailable` (HTTP `503`) on uncached product lookups if empty. |
| `LLM_BASE_URL` | unset (`None`) | Cleared in `smoke_demo.py --inprocess` and `scenarios/run_all.py` | `agents/llm.py` | Optional OpenAI-compatible base URL (`<LLM_BASE_URL>/chat/completions`). When unset or empty, `aimo/parse.py` uses the deterministic rule/recipe parser and `agents/buyer.py` ranks offers deterministically. |
| `LLM_API_KEY` | unset (`None`) | Cleared in `smoke_demo.py --inprocess` and `scenarios/run_all.py` | `agents/llm.py` | Bearer token (`Authorization: Bearer <LLM_API_KEY>`) for optional LLM calls; active only when `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` are all non-empty. |
| `LLM_MODEL` | unset (`None`) | Cleared in `smoke_demo.py --inprocess` and `scenarios/run_all.py` | `agents/llm.py` | Model identifier sent with `temperature=0` to `/chat/completions`; active only when `LLM_BASE_URL` and `LLM_API_KEY` are also non-empty. |
| `<CUSTOMER>_KEY` | `KEYS[customer]` (`demo-<customer>`) | - | `agents/seller.py` | Optional per-customer CLI key override (`HOTEL_AURORA_KEY`, `HOSPITAL_TAYS_KEY`, `SCHOOL_HERVANTA_KEY`) when `--key` is omitted. |
| `TMPDIR` | `"/tmp"` | - | `agents/drift.py` | Temporary directory for `drift-demo.json` state during `python -m agents.drift --demo`. |
| `PYTHONUNBUFFERED` | unset (`1` in container) | `1` in [../Dockerfile](../Dockerfile) | Python runtime ([../Dockerfile](../Dockerfile)) | Forces unbuffered stdout and stderr logging inside the Docker container. |

### Test and Scenario Runner Variables

| Variable | Default Value | Where Read | Purpose |
|---|---|---|---|
| `AIMO_EVAL` | unset (`None`) | `tests/test_precision.py`, `tests/test_precision_holdout.py`, `tests/test_precision_holdout2.py` | Set `AIMO_EVAL=1` (with `data/` present) to execute the catalog precision evaluation suites instead of skipping them. |
| `AIMO_LIVE` | unset (`None`) | `tests/test_offers_milk.py` | Set `AIMO_LIVE=1` to force live upstream Valio offer checks in pytest. |
| `AIMO_TEST_KEY` | `"demo-hotel-aurora"` | `tests/test_buyer.py` | Agent key override for live `agents/buyer.py` integration tests. |
| `KEY_HOTEL`, `KEY_HOSPITAL`, `KEY_SCHOOL` | `"demo-hotel-aurora"`, `"demo-hospital-tays"`, `"demo-school-hervanta"` | `scenarios/_common.py` | Override customer agent keys across scenario scripts. |
| `VERBOSE` | unset (`None`) | `scenarios/run_all.py` | Set `VERBOSE=1` to print per-scenario stdout tables while `scenarios/run_all.py` executes. |

## Stage Recovery and Operations

### Clear a Stuck Stock-Out Simulation

`POST /v1/admin/simulate` stores in-memory product availability overrides (`aimo/offers.py`) that remain active until explicitly cleared or until the server restarts. Always clear overrides immediately after Scene 4 so subsequent offers and quotes see normal stock:

```sh
curl -s -X DELETE http://localhost:8080/v1/admin/simulate \
  -H "X-Admin-Key: demo-admin"
```

Expected response: `{"ok":true}` (replace `demo-admin` with `$AIMO_ADMIN_KEY` if rotated). You can also click **Clear** in `/console.html?demo=1` or `/admin.html`.

If you need to trigger a stock-out from the terminal instead of the browser UI:

```sh
curl -s -X POST http://localhost:8080/v1/admin/simulate \
  -H "X-Admin-Key: demo-admin" \
  -H "Content-Type: application/json" \
  -d '{"product_id":"464119","available_amount":0}'
```

### Restart Cleanly Without Re-Running Scenarios (`make stage` vs `make run`)

Never run `make demo` mid-pitch because `scen` executes all 8 scenarios first. Choose between `make stage` and `make run` based on whether you want a fresh database or existing orders preserved:

| Command | Resets `var/demo.db` | Preserves `var/cache` and `var/keys` | Runs 8 Scenarios | When to Use |
|---|---|---|---|---|
| `make stage` | Yes (`rm -f var/demo.db`) | Yes | No | Reset orders, approvals, mandates, and audit events to a clean slate in ~2 seconds while keeping warmed Valio caches. |
| `make run` | No | Yes | No | Restart `uvicorn` in place while preserving existing orders, pending approvals, custom mandates, and the audit chain in `var/demo.db`. |
| `make demo` | Yes (after `scen`) | Yes (warms `var/cache`) | Yes (`PORT+1`) | Pre-stage T-15 min warmup only. |

If port `8080` is occupied or a previous background process hung, pass `PORT=8090` (for example `make stage PORT=8090`) or inspect `var-demo.log` in the repo root for startup tracebacks.

### Signing Key Rotation (`python -m aimo.keys rotate`)

Quotes (`aimo/quotes.py`) and audit checkpoints (`aimo/audit.py`) are signed with Ed25519 (`EdDSA`) using `AIMO_KEY_DIR/quote-ed25519.json` (`var/keys/quote-ed25519.json` by default). Rotate the active signing key without breaking verification of prior quotes or checkpoints:

```sh
.venv/bin/python -m aimo.keys rotate
```

1. `aimo/keys.py` writes the previous active public JWK to `var/keys/retired-<old_kid>.json`.
2. A fresh Ed25519 private key is written atomically with `0600` permissions to `var/keys/quote-ed25519.json`, and the new `kid` thumbprint is printed (`new kid <thumbprint>`).
3. Restart the gateway (`make run` or `make stage`) so the server process reloads `_cache` and signs new quotes and checkpoints with the new `kid`.
4. `GET /.well-known/jwks.json` returns `[active_public_jwk, *retired_public_jwks]`, so every previously issued JWS continues to verify by matching its `kid` header.

To rotate buyer or admin API keys, set `AIMO_AGENT_KEYS` (JSON mapping) and `AIMO_ADMIN_KEY` in the environment and restart the gateway (see [PUBLIC_URL.md](PUBLIC_URL.md)).

### Offline Quote and Checkpoint JWS Verification

Any third party can verify signed quotes and signed audit checkpoints either against the live JWKS URL or completely offline against a saved `jwks.json` snapshot.

Save the public key set once while the gateway is reachable:

```sh
curl -sf http://localhost:8080/.well-known/jwks.json > /tmp/jwks.json
```

Verify a quote JWS (`jws` field from `POST /v1/quotes`) offline:

```sh
.venv/bin/python -m aimo.quotes verify "$QUOTE_JWS" /tmp/jwks.json
```

- Exit `0`: prints `OK q_<id> customer=<customer> agent=<agent_id> lines=<n> total_gross=<amount> EUR delivery=<YYYY-MM-DD> valid_until=<iso>` (appends `(EXPIRED)` if past cutoff).
- Exit `1`: prints `BAD_SIGNATURE` if the header `alg` is not `EdDSA`, `kid` is unknown, or any payload byte was modified.

Verify an audit checkpoint JWS (`checkpoint.jws` field from `GET /v1/audit`) offline:

```sh
.venv/bin/python -m aimo.audit verify-checkpoint "$CHECKPOINT_JWS" /tmp/jwks.json
```

- Exit `0`: prints `OK seq=<seq> hash=<sha256_hex>`, proving the gateway signed that exact ledger head (`seq`, `hash`, `ts`) with an Ed25519 key in `jwks.json`.
- Exit `1`: prints `BAD_SIGNATURE` on any signature or key mismatch.

Both commands also accept `http://localhost:8080/.well-known/jwks.json` directly in place of `/tmp/jwks.json`, or omit the third argument locally to verify against `AIMO_KEY_DIR` (`var/keys`).
