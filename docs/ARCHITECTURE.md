# Architecture

Owner: pitch (from the code on main, Sat 14 Nov). Contracts: [interfaces.md](interfaces.md). Trust protocol: [TRUST.md](TRUST.md).

## 1. Components

| Component | Path | Responsibility | Key facts (source) |
|---|---|---|---|
| Gateway | `aimo/gateway/` | FastAPI app: REST `/v1/*` (OpenAPI 3.1 at `/openapi.json`), MCP server `valio-aimo-trusted-trade` at `/mcp` (streamable HTTP) and over stdio, `/.well-known/otap.json`, `/.well-known/jwks.json`, `/healthz`, static UI at `/ui` | REST and MCP call the same `aimo.gateway.service` functions, so both return identical JSON (`service.py`). 9 MCP tools, no approval tool by design (gateway report). |
| Backend client | `aimo/backend/client.py` | Valio Aimo mock API: `/api/price` and `/api/availability` merged into `Offer` | Up to 1,000 ids per call, 5-minute cache (`provenance.valid_for_s: 300`), retry with backoff on 429/5xx, `Retry-After` passed through as 503. Fault injection by header `X-Aimo-Faults` or env `AIMO_FAULTS`. |
| Order store | `aimo/backend/orders.py` | Quotes, orders, approvals in SQLite (`data/state.sqlite`); Peppol BIS order-response codes per line (AP accepted, CA accepted with changes, RE rejected) | Valio Aimo has no order API, so this is our mock order backend. Orders are idempotent per quote. |
| Catalog | `aimo/catalog/`, `aimo/backend/catalog_store.py` | 17,546 products in SQLite with an FTS5 trigram index over fi/sv/en names; hybrid search with gemini-embedding-2 (768-d) by reciprocal rank fusion; content per sales unit; GS1 allergen codes mapped to the EU14 tri-state; good-better-best ranking; `plan_need` split planner | Catalog report: parser agrees with live comparison prices on 702 of 752 sampled products (93%). 9,978 products have structured allergen data, 7,568 have none (unknown). |
| Trust | `aimo/trust/` | YAML policy engine, Ed25519 JWS signing of quotes, approvals bound to the quote hash, append-only hash-chained signed audit log, readable report, evidence pack, injection screening (regex, plus optional Gemini layer 2) | Regex screen flags 0 of 34,159 real supplier marketing texts (trust report). 28 unit tests. |
| Gemini wrapper | `aimo/gemini.py` | One shared `google-genai` client, retries on 429/5xx, usage ledger `data/gemini/ledger/*.jsonl` (no prompts, no keys) | Summarised by `docs/pitch/ledger_stats.py`. |
| Buyer agent | `aimo/buyer/` | Gemini function-calling agent over the MCP tools; structured-output need parsing; harness guardrails; terminal demo; `POST /v1/agent/runs` + SSE | Tool loop is run by our code (automatic function calling disabled) so every step is logged and streamed. |
| Approval UI | `web/` | No-build console: agent timeline, good-better-best cards, verdict with every rule, approval inbox, Peppol response, audit chain rail, "Verify chain", evidence pack download; `?mock=1` offline mode | Verifies the quote's Ed25519 signature in the browser against the JWKS (frontend report). |
| Wind tunnel *(landing from team/windtunnel)* | `windtunnel/` | Deterministic scenario skeletons plus Gemini-written request text; recorded API snapshots replayed with faults; independent oracle; scoring vs a naive baseline agent | Metrics: escalation precision/recall, policy and allergen violations (target 0), injection block rate, plan coverage, EUR error vs optimum (`windtunnel/score.py`). |

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
  H->>G: POST /v1/approvals/{order}/decision (approve, role, quote_hash)
  G->>T: check_approval: hash, signature, expiry, role, approver directory
  G->>G: place in order store, Peppol response AP
  G->>T: audit events (quote.created, policy.evaluated, order.pending_approval, approval.verified, order.approved, order.placed)
  H->>G: GET /v1/audit/report/{order}, GET /v1/audit/verify -> ok
```

What each step proves:
1. **Lookup is real.** `POST /v1/offers` for 463038, 464306, 469363 returned `cutoff_passed=true` (issues `terminal_unknown_qty`, `cutoff_passed`), `unit_price 1.01` with unlimited stock, and `available_amount 384` (`limited_stock`), with header `X-Aimo-Served-From: live`.
2. **The plan explains itself.** `POST /v1/plan` returned one line `best 464306 x 1000 = 1010.0`, shortfall 0, in 0.9 s. Rejected candidates carry their reason ("order cut-off passed (Thu 12 Nov 15:00) for delivery Mon 16 Nov").
3. **The quote is binding.** `POST /v1/quotes` returned a quote signed with kid `SnXciV3w…` (RFC 7638 thumbprint), `expires_at` 15 minutes out. Asking for 463038 returns 409 `lines_unavailable` with per-line reasons; asking for 500 × 469363 returns 409 with `partial_stock`, `requested 500`, `available_amount 384`.
4. **The policy decides, in plain language.** `POST /v1/orders` returned `pending_approval` with 11 rule results, two of them `needs_approval`: "Order total €1,010.00 is above the €500.00 auto-approve limit, so a procurement manager must approve it." and "This is 3.3× your usual weekly volume of milk (1,000 L vs about 300 L a week)."
5. **Approval binds to the hash.** A decision with a wrong `quote_hash` returned 409 `quote_hash_mismatch` and was audited as `approval.refused`; the correct hash returned `placed`, `response_code AP`, `approved_by "anna@hotel-aurora.example"` (role in `approver_role` since gateway round 2).
6. **The audit verifies.** `GET /v1/audit/report/{order_id}` returned a 38-line Markdown story with the rule table; `GET /v1/audit/verify` returned `{"ok": true, "checked": 11, "broken_at": null}`.

## 3. Trust protocol summary

Full spec: [TRUST.md](TRUST.md).
- **Who may act:** a `Mandate` (customer, agent, scopes `catalog:read quote:create order:submit`, policy id). Unknown agents get no scopes, so their orders are rejected ("Agent hotel-aurora/mallory is not registered with Hotel Aurora Helsinki, so it has no authority to order.", observed live).
- **What the agent may do alone:** the customer's versioned YAML policy. Every rule reports, including passes. The strictest outcome wins: reject > needs_approval > auto_approve. A crashing rule yields needs_approval, never approval. Unknown allergen data is never safe; Gemini-inferred allergens count as unknown.
- **Quotes:** `quote_hash = sha256(canonical JSON)`, close to RFC 8785; `signature` is a compact JWS (`alg: EdDSA`, RFC 7515/8037) verifiable with `/.well-known/jwks.json`; 15-minute price lock.
- **Approvals:** accepted only if the decision was needs_approval, the hash and signature match, the lock is valid, and the approver acts in the named role (or an override role). Every attempt is audited.
- **Audit:** SQLite table with triggers that refuse UPDATE and DELETE; each event stores `prev_hash`, `hash` and a JWS over `{seq, hash}`. `verify()` recomputes everything; `evidence_pack()` bundles events, quote, decision, JWKS and verification steps for offline checking.

## 4. Production path

The prototype is shaped so that each mock piece has a named production replacement behind an existing seam.

| Today (demo) | Production | Seam that already exists |
|---|---|---|
| Valio Aimo mock price/availability API + our SQLite order store | **SAP / webshop adapter** implementing the same `BackendClient` Protocol (`price`, `availability`) plus an order adapter | `aimo/backend/__init__.py` `BackendClient`, `set_client()`; the gateway never calls the mock API directly |
| Identity from `X-Customer-Id` / `X-Agent-Id` headers | **OAuth 2.1** bearer tokens per the **MCP Authorization spec** (protected resource metadata, RFC 9728) with scopes `catalog:read quote:create order:submit`; fine-grained limits as RFC 9396 `authorization_details` | `Identity` object in `aimo/gateway/service.py`; the manifest already advertises the production scheme |
| Mandates from YAML lookup | **W3C Verifiable Credentials** (VC Data Model 2.0) issued by the customer to its agent, comparable to AP2 intent/cart mandates; agent identity as a DID or a key bound to the OAuth client | `Mandate` model in `aimo/models.py`; `get_mandate()` in trust |
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
| **0. Hardening (now → Sun freeze)** | Replay mode and `AIMO_NOW` for clock-independent demos; minimal OAuth client-credentials; conformance suite on a second toy wholesaler; gold-set and wind-tunnel numbers | Checklist items in docs/PLAN.md ticked with evidence; every number in NUMBERS.md reproduced | team | Mock-API cut-off clock (A4): replay mode. Unmeasured claims: NUMBERS.md gate |
| **1. Pilot with 3 customers (≈ 6–8 weeks)** | One hotel, one school kitchen, one hospital ward, each with its own real policy, approver directory and usual products (retires A2). Gateway still on the mock order store or a sandbox, humans approve every order (shadow mode first: the agent proposes, the existing process orders) | Approval matrix validated by each customer's procurement or dietitian; zero policy or allergen violations; measured zero-correction rate and time saved per order | online sales + team; customer procurement | Policies too strict (rubber-stamping) or too loose: start with "everything needs approval", relax per rule with audit evidence. Allergen data gaps (A6): dietitian confirmation queue |
| **2. SAP / webshop adapter (≈ 1 quarter)** | Implement `BackendClient` (price, availability per date and route) and an order adapter against SAP or the webshop backend; customer price conditions (A7); real categories (A3); per-date ATP (A4) | Pilot orders flow into SAP with the same signed quotes and audit trail; conformance suite green against the real adapter | Valio IT + team | Integration access and data protection: the gateway exposes only the fields agents need (no ERP credentials leave Valio). Load on SAP: the existing 5-minute cache and 1,000-id batching |
| **3. OAuth 2.1 and VC mandates** | OAuth 2.1 per the MCP authorization spec with scopes `catalog:read quote:create order:submit` and RFC 9396 authorization details (retires A8); customer-issued W3C VC mandates for their agents, revocable | A third-party agent onboards with a token and a mandate, without code changes on our side | Valio IT (identity) + team | Customer IdP diversity: support client credentials first, VC mandates as an opt-in; mandate revocation latency: short-lived tokens |
| **4. KMS keys and append-only audit** | Signing keys in a KMS/HSM with rotation via JWKS (retires A9); audit events to WORM storage with periodic chain-head anchoring (RFC 3161) | Key rotation drill passes; an external auditor verifies an evidence pack offline | Valio IT (security) | Key compromise: KMS, rotation, short quote lifetimes (15 minutes). Audit volume: events are small JSON; partition by customer |
| **5. Peppol network** | Orders and order responses as Peppol BIS Ordering 3 documents through Valio's Peppol access point (retires A1's mock response) | A pilot customer's ERP receives AP/CA/RE order responses over Peppol | Valio IT (e-business) + customer ERP owner | Access-point onboarding time; customers without Peppol: keep the JSON API as the primary channel |

Running across all phases: the wind tunnel replays every policy and model change against recorded data before rollout; Gemini usage and cost per order are tracked from the ledger (docs/GEMINI.md).
