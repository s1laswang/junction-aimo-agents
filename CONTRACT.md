# Contract

Every team codes against this. Changes only through lead-gateway, announced to all leads.

## Stack

- Python 3.13, FastAPI, uvicorn, pydantic v2, httpx, official `mcp` SDK, `joserfc` (Ed25519 JWS), stdlib sqlite3.
- One venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- One process: `.venv/bin/uvicorn aimo.app:app --port 8080`. REST under `/v1`, MCP (streamable HTTP) at `/mcp`, UI at `/`, discovery at `/.well-known/`.
- MCP stdio entry: `.venv/bin/python -m aimo.mcp_server`.
- Data dir from `AIMO_DATA_DIR` (default `data`, gitignored symlink). Secrets from `.env` (gitignored symlink). SQLite db at `var/aimo.db` (gitignored).
- Plain HTML + JS + CSS in `web/`, no build step, no framework.

## Layout and owners

| Path | Owner |
|---|---|
| `aimo/app.py`, `aimo/models.py`, `requirements.txt` | lead-gateway |
| `aimo/catalog.py` | gw-catalog |
| `aimo/valio.py`, `aimo/offers.py` | gw-offers |
| `aimo/orders.py` | gw-orders |
| `aimo/api.py`, `aimo/mcp_server.py`, `aimo/discovery.py` | gw-mcp |
| `aimo/quotes.py`, `aimo/keys.py` | tr-sign |
| `aimo/policy.py`, `aimo/mandates.py` | tr-policy |
| `aimo/approvals.py` | tr-approve |
| `aimo/audit.py` | tr-audit |
| `aimo/parse.py` | br-parse |
| `aimo/substitute.py` | br-subst |
| `aimo/allocate.py` | br-split |
| `aimo/recipes.py`, `data_extra/recipes.json` | br-recipe |
| `agents/buyer.py`, `agents/llm.py` | ag-buyer |
| `agents/seller.py` | ag-seller |
| `agents/drift.py` | ag-drift |
| `scenarios/` | ag-scenarios |
| `web/console.*` | ui-console |
| `web/inbox.*` | ui-inbox |
| `web/audit.*` | ui-audit |
| `web/admin.*`, `web/index.html`, `web/style.css` | ui-admin |
| `tests/` | sh-qa (each worker adds its own `tests/test_<module>.py`) |
| `Dockerfile`, `deploy/` | sh-deploy |
| `README.md`, `docs/` | sh-docs |
| `pitch/` | sh-pitch |

## Valio mock API (facts, tested 13 Nov)

- Base `VALIO_API_BASE`, header `X-API-Key: $VALIO_API_KEY`.
- `POST /price` and `POST /availability`, body `{"customer": str, "requestedDate": "YYYY-MM-DD", "products": [ids, 1..1000]}`.
- Price per product: `catalogPrice.value` (net per sales unit), `priceWithTax`, `comparisonPrice` + `comparisonUnit` (per KG/L), `tax.taxRate`, `listPrice`, `netPriceFound`, `error`.
- Availability per product: `closingTime` (order cutoff), `salesBlock`, `logisticModel`, `scheduleLines[{availableAmount, availableDate, salesUnit, salesBlock}]`.
- There is NO order endpoint. Orders are ours.
- It is one shared mock for every team at Junction. Cache responses (`var/cache`), batch ids, never loop-hammer it.
- Files: product JSON (17,546 items, names fi/en/sv in `synkkaData.names`, allergens, nutrition, `units`, `synkkaData.unitConversions`), `deliveryReliability` CSV (0..1), `AVG kg CO2-eq/kg` CSV (category average).

## Models (aimo/models.py, pydantic)

```
Constraint   {lactose_free: bool|None, gluten_free: bool|None, exclude_allergens: [str], labels: [str], origin: str|None, max_unit_price: float|None, brands: [str]}
NeedItem     {query: str, quantity: float, unit: str, constraints: Constraint}
Need         {customer: str, deliver_by: date, items: [NeedItem], raw_text: str|None}
Product      {product_id, name, brand, vendor, category, sales_unit, unit_size: {value, unit}|None, allergens: [str], labels: [str], origin: str|None, temperature: int|None}
Offer        {product_id, product: Product, units_needed: float, unit_price_net, total_net, total_gross, currency,
              available_amount, available_date, closing_time, sales_block, reliability: float|None, co2_per_kg: float|None,
              match_score: float, tier: "exact"|"good"|"better"|"best"|None, notes: [str]}
QuoteLine    {product_id, quantity, sales_unit, unit_price_net, line_total_net}
Quote        {quote_id, customer, agent_id, delivery_date, lines: [QuoteLine], total_net, total_gross, currency, issued_at, valid_until, jws: str}
Order        {order_id, quote_id, customer, agent_id, lines, total_net, status, decision: Decision, substitutions: [ {original_id, substitute_id, reason} ], created_at, updated_at}
OrderStatus  draft | pending_approval | approved | placed | confirmed | rejected | blocked | expired
Decision     {outcome: "auto"|"needs_approval"|"blocked", reasons: [{code, text}]}
Mandate      {customer, agent_id, per_order_eur, per_week_eur, exclude_allergens: [str], allowed_categories: [str]|None,
              substitutions: "never"|"within_prefs"|"ask", preferred_brands: [str], approvers: [str]}
AuditEvent   {seq, ts, customer, actor: "agent:<id>"|"human:<name>"|"system", action, subject, detail: dict, why: str, prev_hash, hash}
```

## Agent identity

- Each buyer agent has a key, header `X-Agent-Key`. `aimo/mandates.py` maps key -> (customer, agent_id, Mandate). Seed three customers: `hotel-aurora` (hotel chain), `hospital-tays` (hospital kitchen, strict allergens), `school-hervanta` (school).

## Tools (MCP tool name = REST route, same JSON in and out)

| Tool | REST | In | Out |
|---|---|---|---|
| `search_products` | `GET /v1/products?q=&lactose_free=&...` | query + Constraint fields | `[Product]` |
| `parse_request` | `POST /v1/needs/parse` | `{text, customer}` | `Need` |
| `get_offers` | `POST /v1/offers` | `Need` | `{items: [{need_item, offers: [Offer], shortfall: float}]}` |
| `request_quote` | `POST /v1/quotes` | `{offers: [{product_id, quantity}], delivery_date}` | `Quote` (signed) |
| `place_order` | `POST /v1/orders` | `{quote_id, jws}` | `Order` with Decision |
| `get_order` | `GET /v1/orders/{id}` | | `Order` |
| `list_approvals` | `GET /v1/approvals` | | `[Order]` pending |
| `decide_approval` | `POST /v1/approvals/{order_id}` | `{approve: bool, by, note}` | `Order` |
| `get_audit_trail` | `GET /v1/audit?customer=&order_id=` | | `[AuditEvent]` + `text` summary |
| `get_mandate` | `GET /v1/mandate` | | `Mandate` |

Discovery: `GET /.well-known/agent-card.json` (tools, auth, policies), `GET /.well-known/jwks.json` (public keys to verify quotes), `GET /openapi.json`.

## Flow

parse_request -> get_offers (search + price + availability + ranking + split/substitutes) -> request_quote (signed, valid until closingTime) -> place_order (verify jws, policy decides auto / needs_approval / blocked) -> approval (human) -> placed -> confirmed. Every step writes an AuditEvent with a plain-language `why`.
