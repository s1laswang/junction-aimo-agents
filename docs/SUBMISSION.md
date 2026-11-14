# Junction 2026 submission text

Challenge: Valio Aimo, "Trusted Trade Agents". Also entered: Google DeepMind Best Use of Gemini API (see [GEMINI.md](GEMINI.md)).
Deadline: Sun 15 Nov 2026 10:00 Helsinki. Links to fill: repo (public?) **TODO**, video **TODO**, live URL **TODO (or "runs locally with one command")**.

## Short version (under 500 characters)

> Trusted Trade Agents is an open front door for AI purchasing agents at Valio Aimo. Any agent (Gemini, Claude, an ERP) connects over MCP or OpenAPI, searches 17.5k real products, gets a signed fixed-price quote and orders, while the customer's own policy decides what runs automatically and what needs a named human. Approvals bind to the quote hash, and every step lands in a signed, hash-chained audit log anyone can verify.

(425 characters)

## Title
**Trusted Trade Agents: an open, signed front door for AI purchasing agents**

## Tagline
Let any company's AI agent buy from Valio Aimo, inside rules the customer controls, with every decision signed and explained.

## Problem
Foodservice customers want their AI agents to restock milk, plan event menus and swap out-of-stock items, but a wholesaler can't give an agent its ERP login. Today there is no standard way for an outside agent to look up products, prices and stock, get a binding price, and order, while the customer decides what the agent may do alone and what needs a human. Without that, every integration is bespoke and nobody can prove afterwards what was agreed.

## Solution
One gateway with two open surfaces: an **MCP server** (9 tools) and an **OpenAPI 3.1 REST API**, discoverable from `/.well-known/otap.json`. Behind it:
- **Live lookup** over the real Valio Aimo mock price and availability API and the 17,546-product catalog, with Finnish/Swedish/English hybrid search (full-text plus Gemini embeddings) and €/L comparison.
- **Good-better-best substitution** that explains each rank. Allergens are tri-state; unknown is never safe.
- **Customer-owned guardrails**: a versioned YAML policy per customer (hotel, school kitchen, hospital) with spend limits, weekly budget, category scope, substitution tiers, allergen hard blocks, cut-off checks, standing-order drift and prompt-injection screening, each returning a plain-language reason.
- **Signed quotes** (Ed25519 JWS, 15-minute price lock) and **human approval bound to the quote hash**. Agents have no approve tool.
- **A signed, hash-chained audit trail** rendered as readable sentences, with chain verification and an offline evidence pack.
- A **Gemini buyer agent** as the reference client and an approval UI for the human.

Demo: *"We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."* The usual 10 L box missed its cut-off (real data), so the agent plans Valio Eila 1 L at €1.01/L, €1,010 in total. The hotel's policy says **needs approval** (over €500, outside the standing preference, 3.3× the usual weekly volume). The procurement manager approves the exact signed quote, the order is placed with a Peppol-style "AP accepted" response, and the audit chain verifies.

## How we meet the core requirements
1. **Product, price and availability lookup in structured form.** `search_products`, `get_product`, `get_offers` (MCP) and `/v1/products/search`, `/v1/products/{id}`, `/v1/offers` (REST) return typed JSON: GTIN, sales unit, content per unit, claims, tri-state allergens, delivery reliability, CO2, live price and stock, cut-off time, and an `issues` list (`cutoff_passed`, `partial_stock`, `out_of_stock`, ...) with provenance (live or cached, fetched at).
2. **Inquiry, then an order at fixed prices.** `plan_need` turns a need into a split plan; `create_quote` re-checks price and stock and returns a signed quote with a 15-minute price lock; `place_order` must present the quote id and hash. Unavailable lines are refused with per-line reasons (409), so the agent re-plans instead of silently getting less.
3. **Permissions and human approval.** A mandate says who may act (scopes `catalog:read quote:create order:submit`); the customer's policy says what runs automatically. Outcomes are auto-approve, needs approval by a named role, or reject, and the strictest rule wins. Approvals are accepted only from the right role, for the exact quote hash, before the lock expires, and every attempt (including refused ones) is audited.

## Optional requirements
- **Delivery time and sustainability.** Each product shows the delivery reliability and category CO2 (kg CO2e/kg) from the challenge CSVs; cut-off times and requested dates come from the live API. Organic and origin labels (e.g. Finnish Blue Swan) are parsed from the data.
- **Substitution and exceptions.** Good-better-best ranking with reasons; cut-off passed, partial stock, stock-out, sales block, price shock, upstream 429/503 (retry, then a clear 503 with `Retry-After`). Fault injection per request (`X-Aimo-Faults`) makes every exception demonstrable.
- **Traceability and explainability.** Hash-chained, Ed25519-signed audit events with a readable report per order, `GET /v1/audit/verify`, and an evidence pack (events, signed quote, decision, JWKS, verification steps) for offline checking.
- **Scenarios covered:** last-minute event, supplier stock-out with a compatible substitute (Gemini recipe check), allergen-safe substitution for a school (milk and nuts) and a hospital (gluten), prompt injection in supplier text, standing-order drift (3× usual volume). Menu-to-order and the proactive selling agent are covered as wind-tunnel scenario families. **TODO:** confirm against the final wind tunnel run.

## Robustness (measured)
**TODO** from the wind tunnel: N scenarios, escalation precision/recall, policy violations (target 0), allergen violations (target 0), injection block rate, plan coverage, € error vs the cheapest compliant plan, and the same metrics for a naive agent with direct API access. Already measured: the regex injection screen flags 0 of 34,159 real supplier marketing texts; the catalog unit parser agrees with live comparison prices for 93% of a 752-product sample; 63 offline tests pass.

## Tech
Python 3.11, FastAPI, Pydantic v2, SQLite (FTS5 trigram), official `mcp` SDK (streamable HTTP and stdio), `google-genai`, `cryptography` (Ed25519), PyYAML; no-build HTML/JS approval UI. Gemini: `gemini-3.8-flash` (buyer agent, need parsing, scenario generation), `gemini-4-hydrogen` with high thinking (substitution decisions), `gemini-3.1-flash-lite` (allergen inference over 4,076 products, recipe check, injection screen), `gemini-embedding-2` (17,546-product hybrid search). One command: `uv run aimo serve`.

## Openness and production path
Open standards throughout: MCP, OpenAPI 3.1, JWS/EdDSA (RFC 7515, 8037) with a JWKS, RFC 7638 key ids, canonical JSON close to RFC 8785, GS1 GTINs, Peppol BIS order-response codes (AP/CA/RE), EU14 allergens (EU FIC 1169/2011). Production path: a SAP/webshop adapter behind the existing `BackendClient` interface; OAuth 2.1 per the MCP authorization spec with RFC 9396 authorization details; mandates as W3C Verifiable Credentials; signing keys in a KMS; Peppol BIS Ordering over the Peppol network; audit events in an append-only WORM store with anchored chain heads; policies portable to OPA or Cedar. Details: [ARCHITECTURE.md](ARCHITECTURE.md), [TRUST.md](TRUST.md).

## Team
Silas Wang (lead, presenter), Momin (prior Aimo work, MCP review), Ivar (domain and UX), Chintufp (pitch and video). Engineering by a coordinated team of Claude Code agents, one per component, each on its own branch.
