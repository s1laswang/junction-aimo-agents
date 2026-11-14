# Openness: the Trusted Trade profile, its conformance suite and a second wholesaler

**Claim:** any wholesaler can implement the same open profile. Any purchasing agent that speaks it can then buy from that wholesaler, inside the customer's guardrails, with no code changes.

**Proof:**
1. A conformance suite that checks any gateway from the outside. It checks the gateway's quote signatures and audit chain with its own crypto code instead of trusting the gateway's answers.
2. A second, synthetic wholesaler, *Nordic Bakery Supply*. It passes the same suite (43/43, the same as the Valio Aimo gateway).
3. Our Gemini buyer agent ordering from Nordic Bakery Supply with zero code changes. Only the URL changes.

## 1. The profile (`otap/0.1`)
The normative list is in [`conformance/profile.py`](../conformance/profile.py). In short:

| Area | A conforming wholesaler gateway must… |
|---|---|
| Discovery | Serve `/.well-known/otap.json` with `name, version, protocol: "otap/0.1", openapi_url, mcp{url, transport: streamable-http}, jwks_url, identity, endpoints, supported_standards, policies`. |
| Keys | Serve `/.well-known/jwks.json` with Ed25519 OKP keys only. Each `kid` is the RFC 7638 thumbprint, and the set holds no private material. |
| OpenAPI | Serve an OpenAPI 3.x document with `/v1/products/search`, `/v1/products/{id}`, `/v1/offers`, `/v1/quotes(/{id})`, `/v1/orders(/{id})`, `/v1/approvals`, `/v1/approvals/{order_id}/decision`, `/v1/audit`, `/v1/audit/verify`. |
| MCP | Spec revision **2026-07-28** (python-sdk `mcp` 2.3.0 from `uv.lock`; `initialize`-handshake clients negotiate up to 2025-11-25). The manifest states it in `mcp.protocol_version`. Offer streamable HTTP with tools `search_products, get_product, get_offers, plan_need, create_quote, place_order, get_order_status, get_audit_trail, list_policies`. Each tool needs a description and an input schema that requires its arguments. There must be **no approval tool**. |
| Identity | Demo: `X-Customer-Id` / `X-Agent-Id` headers. Production: OAuth 2.1 bearer tokens with scopes `catalog:read quote:create order:submit` (MCP authorization spec). |
| Quotes | Each quote is signed. `quote_hash = sha256(canonical_json(quote − {quote_hash, signature, kid}))`. `signature` is a compact JWS (EdDSA) whose payload binds `quote_id` and `quote_hash`. `expires_at` is the price lock. Canonical JSON means sorted keys, no whitespace, UTF-8, and integral numbers without `.0`. |
| Orders | `POST /v1/orders {quote_id, quote_hash}`. A wrong hash gets **409**. Only the agent that requested the quote may submit it (**403** for any other agent or customer). Placement is idempotent per quote. The customer's policy decides `placed`, `pending_approval` or `rejected`, with plain-language `decision.hits`. |
| Approvals | `POST /v1/approvals/{order_id}/decision {decision, approver, quote_hash}` is bound to the quote hash (a wrong hash gets **409**). An agent can never approve (**403**). The approver must be a named human with the right role. |
| Order responses | Peppol BIS Order Response semantics: `response_code` and every `response_lines[].code` is one of `AP`, `CA`, `RE`. |
| Audit | `GET /v1/audit/verify` returns `{ok: true}`. Each event has `hash = sha256(prev_hash + canonical_json(event core))`, chained from 64 zeros, and is signed (JWS payload `{typ, seq, hash}`). |

Optional extensions that our gateway also serves: `/v1/plan`, `/v1/recipes(/plan)`, `/v1/drift/{customer}`, `/v1/proposals`, and the MCP tools `plan_recipe`, `check_standing_order_drift`, `propose_offer`.

## 2. Run the conformance suite
```bash
uv run python -m conformance --base http://127.0.0.1:8000                       # our Valio Aimo gateway (aimo serve)
uv run python -m conformance --base http://127.0.0.1:8820 --query "rye bread"   # Nordic Bakery Supply
#   --customer hotel-aurora --agent buyer-1 --product <id> --date YYYY-MM-DD --approver <id> --json report.json
```
- **Dependencies:** the suite uses only `httpx`, `cryptography` and the `mcp` SDK. It never imports `aimo`, and `conformance/verify.py` re-implements canonical JSON, RFC 7638 thumbprints, EdDSA JWS checks and audit hashing from the spec.
- **What it does:**
  1. Checks the discovery documents, the JWKS, OpenAPI and MCP.
  2. Finds an orderable product with `--query`.
  3. Quotes it and verifies the quote signature independently, including a tampered copy that must fail.
  4. Tries a wrong hash, an intruder agent and another customer.
  5. Lets the policy decide the real order.
  6. Escalates the quantity until the policy asks for approval, then tries approval by an agent and with a wrong hash.
  7. Approves as a named human (found from `/v1/customers`) and checks the Peppol codes.
  8. Re-verifies the order's audit trail and the full chain from genesis.
- **Output:** it prints a PASS/FAIL table, writes JSON, and exits with the number of FAILs.
- **Side effects:** it places **test orders**, so run it against a demo or staging gateway.

**Recorded results (Sat 14 Nov, 23:20, main at fac34f0; the suite now has 44 checks, including the human-only approval checks A2, A3 and A6):**

| Wholesaler | Result | Report |
|---|---|---|
| Valio Aimo Trusted Trade Gateway (replay of recorded mock-API responses, `AIMO_REPLAY=tests/fixtures/snapshot`) | **44/44 PASS** | [docs/openness/conformance-valio-aimo.json](openness/conformance-valio-aimo.json) |
| Nordic Bakery Supply (synthetic, static backend) | **44/44 PASS** | [docs/openness/conformance-nordic-bakery.json](openness/conformance-nordic-bakery.json) |

The earlier run (Sat 19:10, 43 checks, Valio against the live mock API) also passed in full. Both gateways and the suite need the same `AIMO_APPROVER_PIN` in their environment, because the suite's approval checks act as a human.

`uv run pytest -q tests/test_openness.py` boots Nordic Bakery Supply and runs the suite fully offline (no network, no keys).

## 3. The second wholesaler: Nordic Bakery Supply (`toywholesaler/`)
Nordic Bakery Supply has 30 synthetic bread, flour and baking products ([catalog.json](../toywholesaler/catalog.json); made up, not Valio data). It has its **own Ed25519 key, state DB and audit chain** (its own data dir), and **its own policies** ([toywholesaler/policies](../toywholesaler/policies)): Hotel Aurora auto-approves up to €300 there, while at Valio the limit is €500.

It reuses the `aimo` gateway (HTTP + MCP), trust (policy engine, signing, approvals, audit) and catalog/planner **unchanged**. Only four things are swapped:
1. A `BackendClient` with static prices and stock, in the same raw shape as the Valio Aimo price and availability API ([backend.py](../toywholesaler/backend.py)).
2. Product data in the same product-JSON shape, which the unchanged `catalog_store` indexes.
3. The policy directory.
4. The name, MCP server name and manifest text ([app.py](../toywholesaler/app.py)).

```bash
uv run uvicorn toywholesaler.app:app --port 8820          # data in data/toywholesaler (or AIMO_DATA_DIR)
```

## 4. Plug in an agent (zero code changes)
The same Gemini buyer agent, the same customer (`hotel-aurora/buyer-1`) and the same code. Only the URL changes:
```bash
uv run python -m aimo.buyer.run_demo --base-url http://127.0.0.1:8820 --transport mcp \
  --text "We need 40 kg of rye bread for Hotel Aurora's breakfast buffet by Wednesday." \
  --auto-approve-as anna@hotel-aurora.example
# server-side runs: AIMO_MCP_URL=http://127.0.0.1:8820/mcp
```

The full run log is in [docs/openness/buyer-run-nordic-bakery.log](openness/buyer-run-nordic-bakery.log). What happened:
1. The agent listed the 13 MCP tools of Nordic Bakery Supply, parsed "40 KG rye bread by 2026-11-18" with Gemini, then searched, got offers and planned.
2. The plan was 40 × Rye bread 1 kg (NB1002), tier best, €156.00. The agent escalated to gemini-4-hydrogen for the substitution decision.
3. The signed quote led to `place_order`. **Nordic's** policy gave needs_approval: NB1002 is a customer-approved alternative to the usual NB1001, but it is outside the standing preference.
4. anna@hotel-aurora.example approved, and the order was **placed**.
5. The audit report URL points at Nordic's own chain.

Any MCP client works the same way. Claude Desktop or another agent framework only needs the `mcp.url` from `otap.json`.

## 5. Verify offline (cryptography only)
```bash
python -I scripts/verify_quote.py quote.json https://<gateway>/.well-known/jwks.json      # a quote
python -I scripts/verify_quote.py events.json jwks.json                                    # GET /v1/audit or an evidence pack
```
The script is about 30 lines. Keys come **only** from the JWKS you pass, never from the document being checked.
- A Nordic quote verifies against Nordic's JWKS.
- The same quote **fails** against Valio's JWKS (`signature BAD`): the keys are separate, so a signature from one wholesaler can't pass as another's.
- A quote with an edited `total_net` fails (`hash MISMATCH`).

## Known portability gaps (for the gateway and trust owners)
- `aimo/backend/client.py` puts `"source": "valio-aimo-mock-api"` in offer provenance. This should come from the client, e.g. `getattr(client, "source", …)`. Nordic's offers show its `upstream` (`static://nordic-bakery-supply`) correctly, but carry that source label.
- `aimo/trust/approvals.py` and `policy.py` texts say "signed by Valio Aimo" / "placed with Valio Aimo". A `supplier_name` setting would make the audit wording supplier-neutral.
- The buyer profile (`aimo/buyer/profiles.py`) seeds Valio product ids (463038). At Nordic the agent asked for them, got "unavailable" and carried on. This is harmless, but a per-supplier profile would be cleaner.
