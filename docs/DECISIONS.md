# Decisions (ADR log)

Short architecture decision records for every choice a judge or a Valio IT architect might question. Each has context, the decision, the alternatives we rejected and why, and the evidence. Assumptions behind them are in [ASSUMPTIONS.md](ASSUMPTIONS.md); numbers are reproducible from [NUMBERS.md](NUMBERS.md).

Status: **Accepted** (built and on main), **In progress** (decided, being built), **Proposed** (production only).

---

## D1. MCP and OpenAPI as two equal surfaces over one service layer
- **Status:** Accepted.
- **Context:** the challenge asks for an interface "for an external agent", built on open standards. Buyers will arrive with different stacks: LLM agents (Gemini, Claude, Cursor), and classic ERP integrations that speak REST.
- **Decision:** one FastAPI gateway exposing REST `/v1` (OpenAPI 3.1 at `/openapi.json`) and an MCP server (`/mcp`, streamable HTTP, plus stdio) that call the *same* functions in `aimo/gateway/service.py`, so both return identical JSON. A discovery manifest at `/.well-known/otap.json` lists endpoints, the MCP URL, the JWKS URL and policies.
- **Rejected:** MCP only (ERPs and procurement suites don't speak MCP); REST only (every LLM agent would need custom glue); a proprietary agent protocol or SDK (lock-in, and the brief asks for open standards); A2A-style agent-to-agent negotiation (adds a second agent we don't control on Valio's side; the gateway is deterministic on purpose).
- **Evidence:** `uv run aimo mcp-check` lists the MCP tools over HTTP and stdio; the Gemini buyer uses only MCP (`aimo/buyer/tools.py`); README has configs for Claude Desktop, Cursor and Gemini CLI.

## D2. Ed25519 JWS for quote and audit signatures
- **Status:** Accepted.
- **Context:** an agent (and later an auditor) must be able to prove what price was offered, by whom, until when, without trusting our database.
- **Decision:** quotes carry `quote_hash = sha256(canonical JSON)` (close to RFC 8785) and a compact JWS (RFC 7515) with `alg: EdDSA` (RFC 8037), key id = RFC 7638 thumbprint, public key at `/.well-known/jwks.json`. Audit events are signed the same way.
- **Rejected:** HMAC (verifiers would need the secret); RSA/ECDSA P-256 (work, but larger keys and signatures, and ECDSA needs good randomness per signature; Ed25519 is deterministic); plain hashes without signatures (prove integrity, not origin); a blockchain (cost and latency without a trust benefit for a single issuer).
- **Consequence for AP2:** AP2 v0.2 requires a non-deterministic signature (e.g. ECDSA) for the merchant's checkout JWT, not Ed25519. So we describe the quote and approval binding as **AP2-style**, not AP2-compliant. An AP2 bridge would add an ES256 `kid` for AP2 objects only (see D-P2). We did not add ES256 to `otap/0.1` quotes: the profile and conformance suite require Ed25519 keys, both gateways pass 44/44 on it, and ES256 alone would not make the flow AP2-conformant without AP2's mandate objects.
- **Evidence:** `tests/test_trust.py` (sign/verify, tamper, `alg: none` refused); the UI verifies the signature in the browser against the JWKS (frontend report); s1 verifies a quote as a third party (docs/SCENARIOS.md).

## D3. Hash-chained, signed, append-only audit log
- **Status:** Accepted.
- **Context:** "logged and auditable" is a judging criterion; a log the operator can silently edit proves nothing.
- **Decision:** one SQLite table with triggers that refuse UPDATE and DELETE; each event stores `prev_hash`, `hash` and a JWS over `{seq, hash}`; `verify()` recomputes everything; `render()` produces readable sentences; `evidence_pack()` bundles what a third party needs offline.
- **Rejected:** plain application logs (mutable, unreadable for a buyer); a full transparency log or ledger service (right for production, too heavy for 48 hours; the chain head can be anchored later); storing only final orders (loses refused approvals and policy reasons, which are what auditors ask about).
- **Evidence:** `curl -X POST localhost:8000/v1/demo/tamper/3` then `GET /v1/audit/verify` reports `broken_at: 3`; untamper restores it (pitch report).
- **Production:** WORM storage or a transparency log, chain head anchored by RFC 3161 timestamps (D-P3 below).

## D4. Customer policies as versioned YAML, evaluated by our engine
- **Status:** Accepted.
- **Context:** the customer, not Valio and not the agent, must decide what runs automatically. Rules must be readable by a procurement manager and explain themselves.
- **Decision:** one YAML file per customer (`aimo/trust/policies/*.yaml`) with typed rules (spend, weekly budget, category, substitution, allergens, delivery, drift, injection), validated on load, versioned; every rule returns a plain-language reason; the strictest outcome wins; a crashing rule yields needs_approval.
- **Rejected:** rules in the agent's prompt (the agent is the party being constrained); OPA/Rego or Cedar now (powerful but unreadable for the buyer persona and slower to iterate during a hackathon; the YAML maps onto them one to one); hard-coded rules per customer (no versioning, no customer ownership).
- **Evidence:** `tests/test_trust.py` per-customer outcomes; the hero decision shows its needs-approval reasons in plain language (2 with the final policy on `main` `7225d06`: spend and weekly volume, after listed pack swaps pass under the limit; 3 in the earlier Saturday and Sunday 02:54 runs; [NUMBERS.md](NUMBERS.md), [demo-montage.md](demo-montage.md)).

## D5. Verified-best substitutes auto-approve, plus learned approvals
- **Status:** Accepted (Sun 15 Nov 00:30, refined Sun 05:50 in `7225d06`). The policy re-derives each line's tier from the catalog's own ranking (`policy.catalog_verify`); a remembered human "always allow this swap" (`aimo/trust/learned.py`, `GET /v1/customers/{id}/learned`) allows that swap while every other rule still applies; `auto_approve_alternatives_below_eur` (hotel: €50) auto-approves small verified alternatives; a pack swap the customer already listed passes on its own under the spend limit, and `quantity_drift`'s 7-day rolling window counts only earlier orders placed without a human (split orders still add up). Measured on `wt640`: **0 policy and 0 allergen violations** at **99.7%** recall, with escalation precision improving `54.0%` → `68.2%` on `a99692f` (154 unnecessary escalations) → `69.5%` on the final policy (`7225d06`: 331 needed, 145 unnecessary escalations, 74.8% outcome accuracy; `docs/evidence/windtunnel/README.md`, [windtunnel-results.md](windtunnel-results.md), [reports/need.md](reports/need.md)).
- **Context:** escalating every substitution is safe but makes the agent useless (the PLAN checklist records baseline escalation precision at 54%); trusting the agent's own tier label is unsafe (pitch QA issue 2).
- **Decision:** the policy derives the tier itself: `preferred` only for the customer's listed usual products; `best` only for customer-approved alternatives or catalog evidence (same brand, category and unit, all claims kept); anything else is `unverified` → needs approval. When a human approves a recurring swap with `remember` / `remember_scope`, the gateway records a customer-scoped learned rule (`/v1/customers/{id}/learned`) as an audited policy update.
- **Rejected:** escalate every substitution (precision too low, approvers learn to rubber-stamp); trust the agent's tier (red-teamed: a substitute sent as "preferred" was auto-placed before the fix); fully learned/ML approval models (not explainable, can't be audited rule by rule).
- **Evidence:** trust report round 2; re-test in pitch report (469363 sent as "preferred" comes back "better, outside preference" → needs approval).

## D6. Tiers and flags computed server-side
- **Status:** Accepted.
- **Context:** the gateway must be safe for *any* agent, including a careless or hostile one.
- **Decision:** `create_quote` ignores agent-supplied tier and flags for policy purposes; the gateway recomputes them and the policy re-verifies against the customer profile and catalog. Identity comes from the authenticated header, never the body.
- **Rejected:** relying on our own buyer agent to send correct values (true for our agent, not for others); refusing quotes that carry a tier (breaks clients that pass the plan through).
- **Evidence:** repro table in docs/reports/pitch.md, round 2 (issues 2 and 3 FIXED).

## D7. Peppol BIS order-response semantics (AP / CA / RE) and T116 XML export
- **Status:** Accepted (JSON codes + T116 UBL 2.3 XML export); Proposed (Peppol network).
- **Context:** Finnish B2B already uses Peppol for e-invoicing and ordering; Valio's customers' ERPs understand AP (accepted), CA (accepted with changes) and RE (rejected).
- **Decision:** every order returns per-line Peppol response codes (`AP` / `CA` / `RE`) in JSON, and `GET /v1/orders/{id}/peppol-order-response.xml` exports a Peppol BIS Advanced Ordering 3.0 Order Response Advanced (T116) UBL 2.3 XML document (`PEPPOL-T116` rules checked by default in `tests/test_peppol_export.py`; OASIS UBL 2.3 XSD validation is opt-in via `AIMO_UBL_XSD`), so the flow maps onto Peppol BIS Ordering 3 without remodelling.
- **Rejected:** custom status strings (every customer would map them differently); XML-only responses in the agent tool surface (noise in a JSON agent API).
- **Evidence:** `OrderResponseLine.code` in `aimo/models.py`, `aimo/backend/peppol.py`, `docs/peppol/sample-order-response-advanced-{AP,CA,RE}.xml`, and `tests/test_peppol_export.py`; hero order returns AP (`qa_milk_flow.py`).

## D8. Gemini model choices, thinking-level escalation, quota fallback and opt-in layer-2 screening
- **Status:** Accepted.
- **Context:** the agent must be fast enough for a live conversation, careful on the decision a human will later defend, resilient to per-model daily quota limits, and cheap per order.
- **Decision:** `gemini-3.8-flash` with LOW thinking for routine agent steps and need parsing; escalate only the quote-decision turn to `gemini-4-hydrogen` with HIGH thinking when the plan contains a substitution, flag or shortfall; automatic daily-quota / 2x-429 model fallback via `GEMINI_FALLBACK_MODELS` (`gemini-3.5-flash`, then `gemini-4-hydrogen`, skipping an exhausted model for 30 minutes and logging `meta.fallback_from`); `gemini-3.1-flash-lite` for bulk classification (allergen inference, recipe check, and opt-in injection screen layer 2 via `AIMO_SCREEN_GEMINI=1`, off by default because the shipped regex screen has `0` of `34,159` false positives on real supplier texts and `44.4%` recall on the 36-payload eval set vs `94.4%` with Gemini, which flags `42/60` normal buyer requests); `gemini-embedding-2` for search. Structured output everywhere with deterministic fallbacks.
- **Rejected:** the strongest model for every step (the playbook measured 23 s per call for gemini-4-carbon at high thinking; a montage run makes 8–9 Gemini calls); flash only (no extra care on the substitution decision; measured in `windtunnel/thinking.py`: flash LOW is 93.8% accurate on `substitute` and 85.4% on `parse` vs 100.0% for hydrogen HIGH at p50 3.5–3.7 s / p95 < 7.2 s, while flash HIGH reaches ~98–100% only with a 15–69.5 s p95; [windtunnel-results.md](windtunnel-results.md) "Thinking level"); enabling Gemini layer-2 screening by default (`42/60` false positives on clean buyer requests); an open-weights model hosted by us (GPU ops we can't run at a hackathon, and no embeddings, batch and structured output from one API).
- **Evidence:** Sunday final-code montage `6/6 PASS`, escalation on `5` of `34` agent steps (`15%`), `$0.0352` per placed order, down from `$0.0822` before a token trim (`$0.0339` and `6/31` = `19%` on Saturday post-trim; [demo-montage.md](demo-montage.md), [evidence/README.md](evidence/README.md)).

## D9. Embeddings plus full-text search instead of long-context catalog prompts
- **Status:** Accepted.
- **Context:** the catalog is 17,546 products in an 87 MB JSON file; Gemini models have a 1M-token window.
- **Decision:** hybrid retrieval: SQLite FTS5 trigram over fi/sv/en names merged by reciprocal rank fusion with gemini-embedding-2 vectors (768-d) over name, brand, ingredients and claims. Long context is used for the agent's run conversation, not for the catalog.
- **Rejected:** the whole catalog in context (far beyond 1M tokens, and every query would re-read it); context caching of a catalog subset (stale as soon as prices and stock change, and still large); embeddings only (misses exact codes and brand names that FTS gets right); FTS only (misses "dairy-free drink for coffee" → Oatly iKaffe).
- **Evidence:** catalog report (17,546 vectors built in 44 s; hybrid examples); `data/embeddings.npy` shape (17546, 768).

## D10. All-or-nothing quotes and partial-stock approval re-quote
- **Status:** Accepted.
- **Context:** when a line can't be delivered (cut-off passed, partial stock) either at quote time or while an order awaits human approval, the gateway can either silently deliver less or refuse and re-quote.
- **Decision:** `POST /v1/quotes` returns 409 `lines_unavailable` with per-line reasons (`unavailable`, `partial_stock` with requested vs available), so the agent re-plans (split or substitute) and re-quotes; if live stock falls below `qty_sales_units` (or a lock/cut-off expires) before human approval, `POST /v1/approvals/{order_id}/decision` returns 409 `requote_needed` (`detail.reason = "partial_stock"` | `"quote_expired"` | `"cutoff_passed"` | `"line_unavailable"`) with a replacement signed quote instead of placing a silent partial (`CA`) order. A signed quote always means "all of this, at this price, until this time".
- **Rejected:** silently dropping lines (the buyer gets less than asked without noticing, which violates "solves the user need"); partial quotes with warnings (agents ignore warnings; humans would approve a quote that doesn't cover the need).
- **Evidence:** gateway report; s4 re-quote gets 409 naming the product, then a re-plan covers 200 L (docs/SCENARIOS.md).

## D11. Hosting: one-command keyless replay locally, with optional laptop tunnel
- **Status:** Accepted (replay snapshot `tests/fixtures/snapshot/replay/` with its 669-product gzipped catalog subset and `Dockerfile` shipped; [SUBMISSION.md](SUBMISSION.md) documents the one-command keyless `make accept` verification and `uv run aimo serve` local run, with an optional `cloudflared` laptop tunnel for live venue sharing).
- **Context:** judges should be able to run the full system or connect their own MCP client with zero setup; we have no shared cloud project, and the mock API key and Gemini key must not leave the team's machines.
- **Decision:** ship automatic keyless replay (`tests/fixtures/snapshot/replay/`: 669 product ids + 669-product catalog subset across `2026-11-16`..`2026-11-20`) so a fresh clone with an empty `.env` runs `make accept` and `uv run aimo serve` locally with one command, paired with an optional `cloudflared` tunnel from the demo laptop for live venue sharing (PLAN, decided with Silas). `Dockerfile` also bundles `tests/fixtures/snapshot` so a keyless container image boots directly into automatic replay mode (`served_from: replay`) with the 669-product catalog subset out of the box.
- **Rejected:** Cloud Run or similar (needs a GCP project and secret handling we can't set up safely in time); keyed-only localhost (judges without keys or downloaded `data/` couldn't try it); a recorded video only (not a working demo).
- **Evidence:** [SUBMISSION.md](SUBMISSION.md) "Public URL", [reports/replay.md](reports/replay.md) (`make accept` keyless `PASS=16 FAIL=0 SKIP=4`, `scenarios.run_all` 8/8, `conformance` 44/44), and `Dockerfile` (`COPY tests/fixtures/snapshot tests/fixtures/snapshot`, [reports/port.md](reports/port.md)).

---

## Production decisions (proposed)
- **D-P1 OAuth 2.1** per the MCP authorization spec (protected resource metadata, RFC 9728) with scopes `catalog:read quote:create order:submit`; fine-grained limits as RFC 9396 `authorization_details`. Rejected: API keys per agent (say who, not what).
- **D-P2 Mandates as W3C Verifiable Credentials** (VC Data Model 2.0), issued by the customer to its agent, in the style of AP2 mandates (pattern only; an AP2 bridge needs AP2's own mandate objects and an ES256 checkout signature). Rejected: mandates stored only at Valio (the customer can't prove or revoke what it delegated).
- **D-P3 Keys in a KMS/HSM** and audit in WORM storage with anchored chain heads.
- **D-P4 Peppol BIS Ordering 3** over the Peppol network for orders and responses.
