# Connecting agents to AIMO

All outputs below were captured on 2026-11-13 from a local server (`AIMO_DB` pointing at a temp file, port 8115). Product names, prices and quote ids come from the live run. The MCP checks used the official `mcp` Python client over stdio and HTTP. Claude Desktop and Claude Code were not opened, so their config snippets follow the documented formats and the `claude mcp add --help` usage of this machine.

Demo keys: `demo-hotel-aurora`, `demo-hospital-tays`, `demo-school-hervanta` (header `X-Agent-Key`). Admin key: `demo-admin` (header `X-Admin-Key`).

## Start the server

```
ln -s ~/aimo-agents/data data
ln -s ~/aimo-agents/.env .env
make run PORT=8080
```

## Claude Code

HTTP:

```
claude mcp add --transport http aimo http://localhost:8080/mcp --header "X-Agent-Key: demo-hotel-aurora"
```

stdio (run from the repo root, the server reads `data/` and `var/` relative to it):

```
claude mcp add aimo -e AIMO_AGENT_KEY=demo-hotel-aurora -- /bin/sh -c "cd /ABS/PATH/junction26-valio && .venv/bin/python -m aimo.mcp_server"
```

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`, stdio:

```json
{
  "mcpServers": {
    "aimo": {
      "command": "/bin/sh",
      "args": ["-c", "cd /ABS/PATH/junction26-valio && .venv/bin/python -m aimo.mcp_server"],
      "env": {"AIMO_AGENT_KEY": "demo-hotel-aurora"}
    }
  }
}
```

For HTTP, Claude Desktop only takes remote servers through its connector settings, which need a public https URL (see `PUBLIC_URL.md`). Locally, use the stdio entry or the `mcp-remote` bridge:

```json
{
  "mcpServers": {
    "aimo": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:8080/mcp", "--header", "X-Agent-Key: demo-hotel-aurora"]
    }
  }
}
```

`mcp-remote` was not run here.

## Verified: tools/list over stdio and HTTP

Script (mcp 2.3.0):

```python
import asyncio, httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    hc = httpx2.AsyncClient(headers={"X-Agent-Key": "demo-hotel-aurora"})
    async with streamable_http_client("http://localhost:8080/mcp", http_client=hc) as (r, w, *_):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print([t.name for t in (await s.list_tools()).tools])

asyncio.run(main())
```

Output, identical over stdio (`StdioServerParameters(command=".venv/bin/python", args=["-m","aimo.mcp_server"], env={"AIMO_AGENT_KEY": ...})`) and HTTP:

```
12 ['decide_approval', 'get_audit_trail', 'get_mandate', 'get_offers', 'get_order', 'get_product', 'list_approvals', 'list_orders', 'parse_request', 'place_order', 'request_quote', 'search_products']
```

`get_mandate` returned the hotel-aurora mandate through both transports.

## REST walkthrough: the milk request

```
B=localhost:8080; K='X-Agent-Key: demo-hotel-aurora'; H='content-type: application/json'
```

1. Mandate

```
curl -s $B/v1/mandate -H "$K"
{"customer":"hotel-aurora","agent_id":"buyer-aurora","per_order_eur":2500.0,"per_week_eur":15000.0,"exclude_allergens":[],"allowed_categories":null,"substitutions":"within_prefs","preferred_brands":["Valio"],"approvers":["Laura Virtanen","Mikko Heikkinen"]}
```

2. Parse

```
curl -s $B/v1/needs/parse -H "$K" -H "$H" -d '{"text":"I need 1000 litres of lactose-free milk by Monday","customer":"hotel-aurora"}'
{"customer":"hotel-aurora","deliver_by":"2026-11-16","items":[{"query":"milk","quantity":1000.0,"unit":"L","constraints":{"lactose_free":true,...}}],"raw_text":"...","summary":"1000 L milk, lactose-free, deliver by Mon 16 Nov 2026"}
```

3. Offers (send the Need back, without `summary`)

```
curl -s $B/v1/offers -H "$K" -H "$H" -d @need.json
items[0]: shortfall 0.0, offer 464201 "Valio kevytmaito 1 l laktoositon UHT", units_needed 1000.0, unit_price_net 1.56, total_net 1560.0
```

4. Quote

```
curl -s $B/v1/quotes -H "$K" -H "$H" -d '{"offers":[{"product_id":"464201","quantity":1000}],"delivery_date":"2026-11-16"}'
{"quote_id":"q_9fd7aafdb360",...,"total_net":1560.0,"total_gross":1770.0,"currency":"EUR","valid_until":"2026-11-15T17:00:00+02:00","jws":"eyJhbGciOiJFZERTQSIs..."}
```

5. Order (redeems the quote, single use)

```
curl -s $B/v1/orders -H "$K" -H "$H" -d '{"quote_id":"q_9fd7aafdb360","jws":"<jws>"}'
ord_12bd216f3d51 confirmed 1560.0 {"outcome":"auto","reasons":[{"code":"WITHIN_MANDATE","text":"Order total 1,560.00 EUR is under the 2,500.00 EUR per-order limit and 1,560.00 EUR of the 15,000.00 EUR weekly budget"}]}
```

6. Audit

```
curl -s "$B/v1/audit?order_id=ord_12bd216f3d51" -H "$K"
```

`text`:

```
23:46 Hotel Aurora's buyer agent was issued a signed quote. Quote q_9fd7aafdb360 for 1,560.00 EUR net (1,770.00 EUR with VAT), valid until 15.11. 17:00 Helsinki time.
23:46 Hotel Aurora's buyer agent redeemed a signed quote. Quote q_9fd7aafdb360 redeemed for 1,560.00 EUR net (1,770.00 EUR with VAT).
23:46 Hotel Aurora's buyer agent placed an order for 1,560.00 EUR. Agent submitted an order against a signed quote.
23:46 Mandate check passed: Within mandate: Order total 1,560.00 EUR is under the 2,500.00 EUR per-order limit and 1,560.00 EUR of the 15,000.00 EUR weekly budget.
23:46 The system placed an order for 1,560.00 EUR. Placed automatically within mandate.
23:46 Order confirmed by Valio (mock). Confirmed for delivery Mon 16 Nov (simulated fulfilment, Valio has no order API).
```

`verified`: `{"ok": true, "bad_seq": null}`. `checkpoint`: `{"seq": 8, "hash": "1f16...e0", "ts": "2026-11-13T21:46:56Z", "jws": "..."}`.

Check the checkpoint independently:

```
python -m aimo.audit verify-checkpoint <jws> http://localhost:8080/.well-known/jwks.json
```

## Docker

`Dockerfile` is owned by sh-docs. Mount `data/` and `.env` at run time, never bake them in.
