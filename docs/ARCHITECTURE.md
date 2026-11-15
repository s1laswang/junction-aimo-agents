# Architecture

Owner: pitch (from the code on main, Sat 14 Nov). Contracts: [interfaces.md](interfaces.md). Trust protocol: [TRUST.md](TRUST.md).

## 1. Components

| Component | Path | Responsibility | Key facts (source) |
|---|---|---|---|
| Gateway | `aimo/gateway/` | FastAPI app: REST `/v1/*` (OpenAPI 3.1 at `/openapi.json`), MCP server `valio-aimo-trusted-trade` at `/mcp` (streamable HTTP) and over stdio, `/.well-known/otap.json`, `/.well-known/jwks.json`, `/healthz`, static UI at `/ui` | REST and MCP call the same `aimo.gateway.service` functions, so both return identical JSON (`service.py`). 13 MCP tools (9 core `otap/0.1` profile tools plus `list_recipes`, `plan_recipe`, `check_standing_order_drift` and `propose_offer`; no approval tool). Known-customer enforcement across mutating `/v1` routes, catalog search, MCP and `/v1/agent/runs` (`aimo/gateway/auth.py`), human sign-in (`X-Approver-Session` or `X-Approver-Pin`) required to start, answer or list `/v1/agent/runs`, and `/v1` error hardening (`0` 5xx on fuzzed inputs, `tests/test_fuzz.py`). |
| Backend client | `aimo/backend/client.py`, `aimo/backend/replay.py`, `aimo/backend/clock.py` | Valio Aimo mock API (`/api/price` and `/api/availability` merged into `Offer`), automatic keyless replay + live-outage fallback, and simulated clock | Up to 1,000 ids per call, 5-minute cache (`provenance.valid_for_s: 300`), retry with backoff on 429/5xx, `Retry-After` passed through as 503. Automatic keyless replay (`AIMO_REPLAY` or unset `AIMO_API_KEY`) and live-outage fallback serve `tests/fixtures/snapshot/replay/` (669 product ids + 669-product gzipped catalog subset across `2026-11-16`..`2026-11-20`, `served_from: replay`); simulated clock via `AIMO_NOW` and operator-only `X-Aimo-Now`; fault injection via `AIMO_FAULTS` or operator-only `X-Aimo-Faults` (both headers ignored on any request carrying agent identity unless `AIMO_DEMO_FAULTS=1`; never honoured in `AIMO_ENV=production`). |
| Order store | `aimo/backend/orders.py`, `aimo/backend/peppol.py` | Quotes, orders, approvals in SQLite (`data/state.sqlite`); Peppol BIS order-response codes per line (AP accepted, CA accepted with changes, RE rejected) plus T116 UBL 2.3 XML export | Valio Aimo has no order API, so this is our mock order backend. Orders are idempotent per quote; `GET /v1/orders/{id}/peppol-order-response.xml` exports Peppol BIS T116 XML (PEPPOL-T116 rules tested by default; OASIS UBL 2.3 XSD check is opt-in via `AIMO_UBL_XSD`). |
| Catalog | `aimo/catalog/`, `aimo/backend/catalog_store.py` | 17,546 products in SQLite with an FTS5 trigram index over fi/sv/en names; hybrid search with gemini-embedding-2 (768-d) by reciprocal rank fusion; content per sales unit; GS1 allergen codes mapped to the EU14 tri-state; good-better-best ranking; `plan_need` split planner with pre-planning quantity validation (`quantity <= 0` -> 4xx before planning), customer policy allergen hard-blocks enforced inside `plan_need`, and `injection_screen` surfaced on `PlanLine` and `Candidate` | Catalog report (single source): parser agrees with live comparison prices on 702 of 752 sampled products (93%). 9,978 products have structured allergen data, 7,568 have none (unknown). |
| Trust | `aimo/trust/` | YAML policy engine (spend thresholds, 7-day rolling `split_order` and policy-only `quantity_drift` window, `price_jump` >15%, server-verified substitution tiers with listed pack swaps allowed under the spend limit, allergen hard-blocks, customer-scoped learned swaps via `remember` / `/v1/customers/{id}/learned`), Ed25519 JWS signing of quotes, human-only approvals bound to the quote hash (`409 requote_needed` diff and replacement signed quote on expired price locks, cut-offs or `partial_stock`), append-only hash-chained signed audit log, readable report, evidence pack, injection screening across supplier text, `need.raw_text` and `need.product_query` (regex shipped by default; optional Gemini layer 2 via `AIMO_SCREEN_GEMINI=1`, off by default) | Regex screen flags 0 of 34,159 real supplier marketing texts (`docs/pitch/repro_injection_fp.py`) and 80/80 `wt640` scenario payloads (`44.4%` recall on the harder 36-payload eval set vs `94.4%` with opt-in Gemini, which flags `42/60` buyer requests; a flag only sends the order to a human). Covered by offline unit and integration tests (`tests/test_trust.py`, `tests/test_gateway.py`, `tests/test_human_auth.py`, `tests/test_per_approver_auth.py`; [NUMBERS.md](NUMBERS.md)). |
| Gemini wrapper | `aimo/gemini.py` | One shared `google-genai` client, retries on 429/5xx, automatic daily-quota / 2x-429 model fallback (`GEMINI_FALLBACK_MODELS`: `gemini-3.5-flash,gemini-4-hydrogen`, 30-min skip, `meta.fallback_from`), usage ledger `data/gemini/ledger/*.jsonl` (no prompts, no keys) | Summarised by `docs/pitch/ledger_stats.py` (Sunday final-code montage: `$0.0352` per placed order, `15%` escalation on `5/34` steps; Saturday post-trim: `$0.0339`, `19%` on `6/31`). |
| Buyer agent | `aimo/buyer/` | Gemini function-calling agent over the MCP tools, plus deterministic regex parser and scripted keyless fallback (`mock_model.py`); structured-output need parsing; vision intake (`vision.py`); clarifying-question pause; harness guardrails; terminal demo; human-gated `POST /v1/agent/runs` + SSE | Tool loop is run by our code (automatic function calling disabled) so every step is logged and streamed; starting, answering and listing `/v1/agent/runs` requires `X-Approver-Session` or `X-Approver-Pin` (`401` otherwise), and body `faults` plus `inproc`/`fake` transports are ignored unless `AIMO_DEMO_FAULTS=1` (never in `AIMO_ENV=production`). |
| Approval UI | `web/`, `aimo/voice/` | No-build console: approver sign-in panel (`X-Approver-Session` per tab, used before starting runs and approving orders), `Decide by voice` (ElevenLabs + browser Web Speech fallback, one-click confirm bound to `quote_hash`), `409 requote_needed` diff card, clarifying-question card, recipes/ops/blocked-attacks feed, `REPLAY` header pill, agent timeline, good-better-best cards, verdict with every rule, approval inbox, Peppol response, audit chain rail, "Verify chain", evidence pack download; `?mock=1` offline mode | Verifies the quote's Ed25519 signature in the browser against the JWKS (frontend report); voice routes never execute approvals directly (`decides: false`). |
| Wind tunnel | `windtunnel/` | Deterministic scenario skeletons plus Gemini-written request text (640 Gemini + 2,560 template scenarios); recorded API snapshots replayed with faults; independent oracle; scoring vs a naive baseline agent | Final-policy `wt640` re-score (`7225d06`, advancing `a99692f` `68.2%`): `0` policy and `0` allergen violations, `99.7%` recall, `69.5%` escalation precision (`331` needed, `145` unnecessary), `74.8%` outcome accuracy, and `80/80` (`100%`) scenario injection payloads flagged vs `372` / `193` / `0/80` for the naive agent (`windtunnel/score.py`, `docs/evidence/windtunnel/README.md`, [windtunnel-results.md](windtunnel-results.md)). |
| Openness & conformance | `conformance/`, `toywholesaler/`, `scenarios/` | External `otap/0.1` conformance suite (44/44 checks), second synthetic wholesaler (*Nordic Bakery Supply*), and `s0`..`s7` end-to-end acceptance scenarios (8/8) | Independent Ed25519 JWS and audit-chain verification without importing `aimo` ([OPENNESS.md](OPENNESS.md), [SCENARIOS.md](SCENARIOS.md)). |

## 2. Data flow: "1,000 litres of lactose-free milk by Monday"

All values below were observed on a live run against the mock API on Sat 14 Nov 2026 (`docs/pitch/qa_milk_flow.py`, see [reports/pitch.md](reports/pitch.md)).

```mermaid
sequenceDiagram
  autonumber
  participant A as Buyer agent (Gemini)
  participant G as Gateway (MCP / REST)
  participant C as Catalog
  participant V as Valio mock API
  participant T as Trust
  participant H as Procurement manager
  A->>A: parse request (structured output): lactose-free milk, 1000 L, by Mon 16 Nov
  A->>G: search_products / plan_need
  G->>C: plan_need(need)
  C->>V: /api/price + /api/availability (one batched call each, cached 5 min)
  V-->>C: 463038 cut-off passed Thu 12 Nov 15:00; 464306 in stock at 1.01; 469363 only 384 units
  C-->>G: plan: best 464306 x 1000 = EUR 1,010 (flag outside_preference); better 469363 (partial_stock); 463038 rejected
  A->>G: create_quote(lines)
  G->>V: re-fetch price and availability live (provenance served_from: live)
  G->>C: recompute tier and substitution flags server-side
  G->>T: sign_quote: sha256 over canonical JSON, Ed25519 JWS, expires in 15 min
  G-->>A: signed Quote (quote_id, quote_hash, signature, kid)
  A->>G: place_order(quote_id, quote_hash)
  G->>T: evaluate(quote, mandate): 11 rules
  T-->>G: needs_approval (spend > EUR 500; 3.3x usual weekly milk volume)
  G-->>A: Order pending_approval
  H->>G: POST /v1/approvals/{order}/decision (X-Approver-Pin or X-Approver-Session, approve, role, quote_hash)
  G->>T: check_approval: human credential, no X-Agent-Id, hash, signature, expiry, role, approver directory
  G->>G: place in order store, Peppol response AP
  G->>T: audit events (quote.created, policy.evaluated, order.pending_approval, approval.verified, order.approved, order.placed)
  H->>G: GET /v1/audit/report/{order}, GET /v1/audit/verify -> ok
```

What each step proves:
1. **Lookup is real.** `POST /v1/offers` for 463038, 464306, 469363 returned `cutoff_passed=true` (issues `terminal_unknown_qty`, `cutoff_passed`), `unit_price 1.01` with unlimited stock, and `available_amount 384` (`limited_stock`), with header `X-Aimo-Served-From: live`.
2. **The plan explains itself.** `POST /v1/plan` returned one line `best 464306 x 1000 = 1010.0`, shortfall 0, in 0.9 s. Rejected candidates carry their reason ("order cut-off passed (Thu 12 Nov 15:00) for delivery Mon 16 Nov").
3. **The quote is binding.** `POST /v1/quotes` returned a quote signed with kid `SnXciV3w…` (RFC 7638 thumbprint), `expires_at` 15 minutes out. Asking for 463038 returns 409 `lines_unavailable` with per-line reasons; asking for 500 × 469363 returns 409 with `partial_stock`, `requested 500`, `available_amount 384`.
4. **The policy decides, in plain language.** `POST /v1/orders` returned `pending_approval` with 11 rule results, two of them `needs_approval` under the final policy (`7225d06`, where the hotel's listed 1 L pack swap is allowed on its own; three in the earlier Saturday capture before that refinement): "Order total €1,010.00 is above the €500.00 auto-approve limit, so a procurement manager must approve it." and "This is 3.3× your usual weekly volume of milk (1,000 L vs about 300 L a week)."
5. **Approval binds to the hash.** A decision with a wrong `quote_hash` returned 409 `quote_hash_mismatch` and was audited as `approval.refused`; the correct hash returned `placed`, `response_code AP`, `approved_by "anna@hotel-aurora.example"` (role in `approver_role` since gateway round 2).
6. **The audit verifies.** `GET /v1/audit/report/{order_id}` returned a 38-line Markdown story with the rule table; `GET /v1/audit/verify` returned `{"ok": true, "checked": 11, "broken_at": null}`.

## 3. Trust protocol summary

Full spec: [TRUST.md](TRUST.md).
- **Who may act:** a `Mandate` (customer, agent, scopes `catalog:read quote:create order:submit`, policy id). Unknown agents get no scopes, so their orders are rejected ("Agent hotel-aurora/mallory is not registered with Hotel Aurora Helsinki, so it has no authority to order.", observed live).
- **What the agent may do alone:** the customer's versioned YAML policy. Every rule reports, including passes. The strictest outcome wins: reject > needs_approval > auto_approve. A crashing rule yields needs_approval, never approval. Unknown allergen data is never safe; Gemini-inferred allergens count as unknown.
- **Quotes:** `quote_hash = sha256(canonical JSON)`, close to RFC 8785; `signature` is a compact JWS (`alg: EdDSA`, RFC 7515/8037) verifiable with `/.well-known/jwks.json`; 15-minute price lock.
- **Approvals:** accepted only if the caller presents a human-only credential (`X-Approver-Pin` or a signed per-person `X-Approver-Session` token from `POST /v1/auth/approver-session`, with no `X-Agent-Id`), the decision was needs_approval, the hash and signature match, the lock is valid, and the approver acts in the named role (or an override role). Every attempt is audited, with per-client brute-force lockout (`429 approver_locked`).
- **Audit:** SQLite table with triggers that refuse UPDATE and DELETE; each event stores `prev_hash`, `hash` and a JWS over `{seq, hash}`. `verify()` recomputes everything; `evidence_pack()` bundles events, quote, decision, JWKS and verification steps for offline checking.

## 4. Production path

The prototype is shaped so that each mock piece has a named production replacement behind an existing seam.

| Today (demo) | Production | Seam that already exists |
|---|---|---|
| Valio Aimo mock price/availability API + our SQLite order store | **SAP / webshop adapter** implementing the same `BackendClient` Protocol (`price`, `availability`) plus an order adapter | `aimo/backend/__init__.py` `BackendClient`, `set_client()`; the gateway never calls the mock API directly |
| Identity from `X-Customer-Id` / `X-Agent-Id` headers | **OAuth 2.1** bearer tokens per the **MCP Authorization spec** (protected resource metadata, RFC 9728) with scopes `catalog:read quote:create order:submit`; fine-grained limits as RFC 9396 `authorization_details` | `Identity` object in `aimo/gateway/service.py`; the manifest already advertises the production scheme |
| Mandates from YAML lookup | **W3C Verifiable Credentials** (VC Data Model 2.0) issued by the customer to its agent, in the style of AP2 open/closed mandates (aligned, not AP2-conformant; AP2 checkout JWTs need ECDSA such as ES256, our quotes are Ed25519); agent identity as a DID or a key bound to the OAuth client | `Mandate` model in `aimo/models.py`; `get_mandate()` in trust |
| Ed25519 key in `data/keys/` (gitignored) | **KMS/HSM**-held signing key (e.g. Cloud KMS `EC_SIGN_ED25519`); rotation by publishing new kids in the JWKS | `aimo/trust/signing.py`; verifiers already select keys by `kid` |
| Peppol-style order response codes in JSON | **Peppol BIS Ordering 3** (Order and Order Response documents over the Peppol network); GS1 GTINs as product identity | `OrderResponseLine.code` uses AP/CA/RE; every product carries its GTIN |
| Append-only SQLite audit table | **Append-only audit store**: WORM object storage with retention lock or a transparency log; periodic anchoring of the chain head (e.g. RFC 3161 timestamp) | `verify()` already returns `head`; events are self-contained signed JSON |
| YAML policies | Same rules in a policy-as-code engine (OPA/Rego or Cedar) where a customer wants to bring its own | Policies are validated on load and versioned; `policy_version` is in every decision |
| Wind tunnel on recorded snapshots | Continuous pre-release evaluation: every policy or model change replays thousands of synthetic customers before rollout | `windtunnel/` replay client and oracle |

Assumptions we made (to confirm with Valio Aimo):
- There is no order API on the mock backend, so ordering is our mock. ASSUMPTION.
- Category names are not in the data; category groups for the hotel's scope are inferred from codes (10, 11, 17). UNVERIFIED.
- Customer ids are passed upstream as `customer`; the mock accepted `hotel-aurora` (gateway report). Real customer-specific pricing is UNVERIFIED.

## 5. Next steps: phased path to production

Each phase keeps the gateway's external contract (MCP tools, `/v1` API, signed quote format, audit format) stable, so agents built against the prototype keep working. Assumptions being retired are numbered as in [ASSUMPTIONS.md](ASSUMPTIONS.md); the reasoning behind each choice is in [DECISIONS.md](DECISIONS.md). Owners are roles, not names: "team" is us, "Valio IT" is Valio Aimo's integration and data-governance function, "online sales" is Valio Aimo's webshop/commercial owner.

| Phase | Scope | Exit criterion | Owner | Main risks and mitigation |
|---|---|---|---|---|
| **0. Hardening (completed for submission)** | Verified and shipped: replay mode (`AIMO_REPLAY`) and simulated clock (`AIMO_NOW`) for clock-independent and keyless demos, with operator-only `X-Aimo-Now` / `X-Aimo-Faults` and `/v1/agent/runs` fault/transport guards (`AIMO_DEMO_FAULTS`); known-customer `/v1` and MCP enforcement plus per-approver human credentials (`X-Approver-Session` / `X-Approver-Pin` on approvals and `/v1/agent/runs`); `otap/0.1` conformance **44/44** on two wholesalers (Valio Aimo and `toywholesaler`); Peppol T116 UBL 2.3 XML export (opt-in OASIS XSD check via `AIMO_UBL_XSD`); wind tunnel `wt640` at **0 policy and 0 allergen violations** (**68.2%** precision on `a99692f` -> **69.5%** precision / **145** unnecessary escalations on final policy `7225d06` at **99.7%** recall, [windtunnel-results.md](windtunnel-results.md)); reproducible commands in [NUMBERS.md](NUMBERS.md) | Checklist items in [PLAN.md](PLAN.md) recorded with evidence; every claimed number backed by a command in [NUMBERS.md](NUMBERS.md) (verified) | team | Mock-API cut-off clock (A4): retired for demos via `AIMO_REPLAY` and `AIMO_NOW`. Unmeasured claims: gated by [NUMBERS.md](NUMBERS.md) |
| **1. Pilot with 3 customers (estimate: 6–8 weeks)** | One hotel, one school kitchen, one hospital ward, each with its own real policy, approver directory and usual products (retires A2). Gateway still on the mock order store or a sandbox, humans approve every order (shadow mode first: the agent proposes, the existing process orders) | Approval matrix validated by each customer's procurement or dietitian; zero policy or allergen violations; measured zero-correction rate and time saved per order | online sales + team; customer procurement | Policies too strict (rubber-stamping) or too loose: start with "everything needs approval", relax per rule with audit evidence. Allergen data gaps (A6): dietitian confirmation queue |
| **2. SAP / webshop adapter (estimate: one quarter)** | Implement `BackendClient` (price, availability per date and route) and an order adapter against SAP or the webshop backend; customer price conditions (A7); real categories (A3); per-date ATP (A4) | Pilot orders flow into SAP with the same signed quotes and audit trail; conformance suite green against the real adapter | Valio IT + team | Integration access and data protection: the gateway exposes only the fields agents need (no ERP credentials leave Valio). Load on SAP: the existing 5-minute cache and 1,000-id batching |
| **3. OAuth 2.1 and VC mandates** | OAuth 2.1 per the MCP authorization spec with scopes `catalog:read quote:create order:submit` and RFC 9396 authorization details (retires A8); customer-issued W3C VC mandates for their agents, revocable | A third-party agent onboards with a token and a mandate, without code changes on our side | Valio IT (identity) + team | Customer IdP diversity: support client credentials first, VC mandates as an opt-in; mandate revocation latency: short-lived tokens |
| **4. KMS keys and append-only audit** | Signing keys in a KMS/HSM with rotation via JWKS (retires A9); audit events to WORM storage with periodic chain-head anchoring (RFC 3161) | Key rotation drill passes; an external auditor verifies an evidence pack offline | Valio IT (security) | Key compromise: KMS, rotation, short quote lifetimes (15 minutes). Audit volume: events are small JSON; partition by customer |
| **5. Peppol network** | Orders and order responses as Peppol BIS Ordering 3 documents through Valio's Peppol access point (retires A1's mock response) | A pilot customer's ERP receives AP/CA/RE order responses over Peppol | Valio IT (e-business) + customer ERP owner | Access-point onboarding time; customers without Peppol: keep the JSON API as the primary channel |

Running across all phases: the wind tunnel replays every policy and model change against recorded data before rollout; Gemini usage and cost per order are tracked from the ledger (docs/GEMINI.md).
