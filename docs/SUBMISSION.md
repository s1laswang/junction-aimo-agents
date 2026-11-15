# Junction 2026 submission text

Challenge: Valio Aimo, "Trusted Trade Agents". Also entered: Google DeepMind Best Use of Gemini API (see [GEMINI.md](GEMINI.md)).
Deadline: Sun 15 Nov 2026 10:00 Helsinki (code freeze and tag `aimo-v1` at 06:00).

| Link | Value |
|---|---|
| Repository (public) | https://github.com/s1laswang/junction-aimo-agents (judged version: tag `aimo-v1`) |
| Demo video | *Filled at the freeze.* Chintufp records it from [DEMO.md](DEMO.md) "Recording the video", uploads it unlisted, and pastes the URL into this cell and into the "Video" field of the Junction submission form. |
| Public URL | *Filled at the freeze* if a hosted gateway is up (paste it into this cell and the form's "Demo URL" field). Otherwise this cell reads: "Runs locally with one command: `make accept`, then `uv run aimo serve` and open http://localhost:8000/ui (works with no keys: replay mode on recorded mock-API responses and a committed catalog subset; see README, Keyless vs keyed)." |

## Short version (under 500 characters)

> Trusted Trade Agents is an open front door for AI purchasing agents at Valio Aimo. Any agent (Gemini, Claude, an ERP) connects over MCP or OpenAPI, searches the 17.5k-product challenge catalog, gets a signed fixed-price quote and orders, while the customer's own policy decides what runs automatically and what needs a named human. Approvals bind to the quote hash, and every step lands in a signed, hash-chained audit log anyone can verify.

(425 characters)

## Title
**Trusted Trade Agents: an open, signed front door for AI purchasing agents**

## Tagline
Let any company's AI agent buy from Valio Aimo, inside rules the customer controls, with every decision signed and explained.

## Problem
Foodservice customers want their AI agents to restock milk, plan event menus and swap out-of-stock items, but a wholesaler can't give an agent its ERP login. Today there is no standard way for an outside agent to look up products, prices and stock, get a binding price, and order, while the customer decides what the agent may do alone and what needs a human. Without that, every integration is bespoke and nobody can prove afterwards what was agreed.

## Solution
One gateway with two open surfaces: an **MCP server** (13 tools) and an **OpenAPI 3.1 REST API**, discoverable from `/.well-known/otap.json`. Behind it:
- **Live lookup** over the live Valio Aimo challenge mock price and availability API and the 17,546-product synthetic catalog, with Finnish/Swedish/English hybrid search (full-text plus Gemini embeddings) and €/L comparison.
- **Good-better-best substitution** that explains each rank. Allergens are tri-state; unknown is never safe.
- **Customer-owned guardrails**: a versioned YAML policy per customer (hotel, school kitchen, hospital) with spend limits, weekly budget, category scope, substitution tiers, allergen hard blocks, cut-off checks, standing-order drift over a rolling 7-day window, split-order detection, price jumps over 15%, learned "always allow this swap" approvals and prompt-injection screening, each returning a plain-language reason.
- **Signed quotes** (Ed25519 JWS, 15-minute price lock) and **human approval bound to the quote hash**. Agents have no approve tool.
- **A signed, hash-chained audit trail** rendered as readable sentences, with chain verification and an offline evidence pack.
- A **Gemini buyer agent** as the reference client and an approval UI for the human.

Demo: *"We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."* The usual 10 L box missed its cut-off (live mock-API data), so the agent plans Valio Eila 1 L at €1.01/L, €1,010 in total. The hotel's policy says **needs approval** (over €500, outside the standing preference, 3.3× the usual weekly volume). The procurement manager approves the exact signed quote, the order is placed in our mock order service (no upstream order API) with a Peppol-style "AP accepted" response, and the audit chain verifies.

## How we meet the core requirements
1. **Product, price and availability lookup in structured form.** `search_products`, `get_product`, `get_offers` (MCP) and `/v1/products/search`, `/v1/products/{id}`, `/v1/offers` (REST) return typed JSON: GTIN, sales unit, content per unit, claims, tri-state allergens, delivery reliability, CO2, live price and stock, cut-off time, and an `issues` list (`cutoff_passed`, `partial_stock`, `out_of_stock`, ...) with provenance (live or cached, fetched at).
2. **Inquiry, then an order at fixed prices.** `plan_need` turns a need into a split plan; `create_quote` re-checks price and stock and returns a signed quote with a 15-minute price lock; `place_order` must present the quote id and hash. Unavailable lines are refused with per-line reasons (409), so the agent re-plans instead of silently getting less.
3. **Permissions and human approval.** A mandate says who may act (scopes `catalog:read quote:create order:submit`); the customer's policy says what runs automatically. Outcomes are auto-approve, needs approval by a named role, or reject, and the strictest rule wins. Approvals are human-only: the approver signs in with a credential no agent can obtain (`POST /v1/auth/approver-session`, PIN in, signed 8-hour session bound to that one person out; production: the customer's SSO). A decision is accepted only from a named person in the customer's approver directory acting in the right role, for the exact quote hash, before the lock expires. Any request carrying agent identity gets 403 even if it names a real approver, MCP has no approval tool at all, and every attempt (including refused ones) is audited as `approval.refused`. Tier and substitution flags are computed by the gateway, not taken from the agent.

## Optional requirements
- **Delivery time and sustainability.** Each product shows the delivery reliability and category CO2 (kg CO2e/kg) from the challenge CSVs; cut-off times and requested dates come from the live API. Organic and origin labels (e.g. Finnish Blue Swan) are parsed from the data.
- **Substitution and exceptions.** Good-better-best ranking with reasons; cut-off passed, partial stock, stock-out, sales block, price shock, upstream 429/503 (retry, then a clear 503 with `Retry-After`). Fault injection per request (`X-Aimo-Faults`) makes every exception demonstrable.
- **Traceability and explainability.** Hash-chained, Ed25519-signed audit events with a readable report per order, `GET /v1/audit/verify`, and an evidence pack (events, signed quote, decision, JWKS, verification steps) for offline checking.
- **Scenarios covered:** last-minute event, supplier stock-out with a compatible substitute (Gemini recipe check), allergen-safe substitution for a school (milk and nuts) and a hospital (gluten), prompt injection in supplier text, standing-order drift (3× usual volume). Six of these run live end to end with the Gemini agent and all six pass (see Robustness). Menu-to-order and proactive selling are MCP tools (`plan_recipe`, `list_recipes`, `propose_offer`, `check_standing_order_drift`) and wind-tunnel scenario families (`menu_recipe` and `upsell_pitch`, 40 Gemini-written scenarios each, 0 policy and 0 allergen violations; [windtunnel-results.md](windtunnel-results.md)).

## Robustness (measured)
**Live montage, 6 of 6 scenarios PASS** (`uv run aimo demo --montage`, Gemini agent over MCP against the live mock API, Sat 14 Nov 18:21; [demo-montage.md](demo-montage.md)):
| Scenario | What happens |
|---|---|
| happy | Usual product past its cut-off, same-brand substitute, EUR 1,010, needs approval (spend, substitution, 3.3× volume), approved, placed |
| stockout | Injected partial stock: the order is split into 4 lines, shortfall 0, approved, placed |
| school-allergen | Milk products rejected for a school with milk and nut allergies; Oatly oat drink approved by the kitchen manager |
| injection | Planted instructions in supplier text: 4 texts flagged and redacted before the model saw them, the policy cites injection, the quantity stays at 300 |
| drift | 4,000 L standing order (13× usual): quote prepared, order held for human confirmation |
| event | Multi-item request (cream and butter, lactose-free): one quote with 2 lines, placed |

Each scenario takes 22–37 s end to end. The agent escalated to gemini-4-hydrogen with high thinking on every run, but only for the quote-decision turn (6 of 31 agent steps, 19%). Cost per placed order: **$0.0339** in Gemini calls (down from $0.0822 before a token trim, −59%).

Wind tunnel ([windtunnel-results.md](windtunnel-results.md), pipeline mode, measured on git dfecb2a, which is in main's history): 640 Gemini-written scenarios (16 families, 3 policy archetypes), scored by a deterministic oracle (`windtunnel/oracle.py`). After the need session's policy changes (re-run Sun 15 Nov 00:30 on the same 640 scenarios): the gateway has **0 policy violations and 0 allergen violations**, escalation recall **99.7%**, escalation precision **68.2%** (up from 54.0%; our 90% target is not met), outcome accuracy 73.4% (from 55.0%). The naive agent with direct API access, scored on Sat 14 Nov 18:24 against the earlier policy version: **372 policy violations, 193 allergen violations**, recall 17.8%. Before those changes (Sat 18:24): gateway 1 policy violation, recall 98.8%, precision 54.0%; injection block 56.2% vs 0%; median € over the cheapest compliant plan 0.0% for both. At 2,560 template scenarios: 11 vs 1,452 policy violations, 0 vs 728 allergen violations. Injection screen alone: regex 44.4% recall, regex + Gemini 94.4% (Gemini flags 42 of 60 buyer requests, a 70% false-positive rate there, and 0 of 60 supplier texts; a flag only sends the order to a human). Input hardening: a fuzz test sends about 2,600 malformed requests to every `/v1` route with 0 server errors; fractional pieces (2.5 ST) and absurd quantities (> 1e6) get a plain 422, and quantities far above the customer's usual volume or unknown stock always need a human (`tests/test_fuzz.py`, `tests/test_harden.py`). Agent mode (the Gemini buyer through the real gateway, 48 scenarios, Sat 15:16): 0 policy and 0 allergen violations, escalation recall 100%; a Gemini judge scored the transcripts 0.95 mandate followed, 1.00 no invented data, 1.00 injection resisted.

Also measured: the regex injection screen flags 0 of 34,159 supplier marketing texts in the challenge catalog; the catalog unit parser agrees with live comparison prices for 93% of a 752-product sample (single source, the catalog report); `pytest` gives 344 passed, 7 skipped, 1 xfailed, 1 xpassed. An agent naming a real approver gets 403 and the refusal is in the audit log (re-run Sat 23:10; [DEMO.md](DEMO.md) "Permissions attack"). Commands for every number: [NUMBERS.md](NUMBERS.md).

## Tech
Python 3.11, FastAPI, Pydantic v2, SQLite (FTS5 trigram), official `mcp` SDK (streamable HTTP and stdio), `google-genai`, `cryptography` (Ed25519), PyYAML; no-build HTML/JS approval UI. Gemini: `gemini-3.8-flash` (buyer agent, need parsing, scenario generation), `gemini-4-hydrogen` with high thinking (substitution decisions), `gemini-3.1-flash-lite` (allergen inference over 4,076 products, recipe check, injection screen), `gemini-embedding-2` (17,546-product hybrid search). About 31% of the agent's input tokens are served from Gemini's implicit cache. One command: `uv run aimo serve`.

## Openness and production path
Open standards throughout: MCP, OpenAPI 3.1, JWS/EdDSA (RFC 7515, 8037) with a JWKS, RFC 7638 key ids, canonical JSON close to RFC 8785, GS1 GTINs, Peppol BIS order-response codes (AP/CA/RE), EU14 allergens (EU FIC 1169/2011). Production path: a SAP/webshop adapter behind the existing `BackendClient` interface; OAuth 2.1 per the MCP authorization spec with RFC 9396 authorization details; mandates as W3C Verifiable Credentials; signing keys in a KMS; Peppol BIS Ordering over the Peppol network; audit events in an append-only WORM store with anchored chain heads; policies portable to OPA or Cedar. Every assumption is listed with its impact and production replacement in [ASSUMPTIONS.md](ASSUMPTIONS.md) (no upstream order API, invented customers, inferred categories, mock-API quirks we measured); every design choice has an ADR with rejected alternatives in [DECISIONS.md](DECISIONS.md); every number has a reproducing command in [NUMBERS.md](NUMBERS.md); the phased plan (3-customer pilot → SAP adapter → OAuth/VC mandates → KMS → Peppol network) with owners and risks is in [ARCHITECTURE.md](ARCHITECTURE.md) §5. Trust protocol: [TRUST.md](TRUST.md).

## Team
Silas Wang (lead, presenter), Momin (prior Aimo work, MCP review), Ivar (domain and UX), Chintufp (pitch and video). Engineering by a coordinated team of Claude Code agents, one per component, each on its own branch.
