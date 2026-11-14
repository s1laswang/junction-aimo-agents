# Connecting agents to AIMO

All outputs below were captured on 2026-11-14 from a local server (`AIMO_DB` and `AIMO_KEY_DIR` pointing at temp paths, `AIMO_CACHE_DIR=/tmp/opencode/aimo-cache`, port 8207). Product names, prices, quote ids, order ids, and checkpoint hashes come from that live run; command snippets use default port `8080` to match `make run`. The MCP checks used the official `mcp` Python client (2.3.0) over streamable HTTP and stdio. Claude Desktop and Claude Code were not opened, so their config snippets follow the documented formats and `claude mcp add --help` usage.

Demo keys: `demo-hotel-aurora`, `demo-hospital-tays`, `demo-school-hervanta` (header `X-Agent-Key`). Admin key: `demo-admin` (header `X-Admin-Key`).

## Start the server

```sh
ln -s ~/aimo-agents/data data
ln -s ~/aimo-agents/.env .env
make run PORT=8080
```

## Claude Code

HTTP:

```sh
claude mcp add --transport http aimo http://localhost:8080/mcp --header "X-Agent-Key: demo-hotel-aurora"
```

stdio (run from the repo root, the server reads `data/` and `var/` relative to it):

```sh
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

## Verified: tools/list and get_mandate over HTTP and stdio

Script (`mcp` 2.3.0):

```python
import asyncio, json, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

async def check_http():
    async with create_mcp_http_client(headers={"X-Agent-Key": "demo-hotel-aurora"}) as http:
        async with streamable_http_client("http://localhost:8080/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                res = await s.call_tool("get_mandate", {})
                mandate = res.structured_content or json.loads(res.content[0].text)
                print(len(tools), tools)
                print(json.dumps(mandate, separators=(",", ":")))

async def check_stdio():
    params = StdioServerParameters(
        command=".venv/bin/python",
        args=["-m", "aimo.mcp_server"],
        env={**os.environ, "AIMO_AGENT_KEY": "demo-hotel-aurora"},
    )
    async with stdio_client(params) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            res = await s.call_tool("get_mandate", {})
            mandate = res.structured_content or json.loads(res.content[0].text)
            print(len(tools), tools)
            print(json.dumps(mandate, separators=(",", ":")))

asyncio.run(check_http())
asyncio.run(check_stdio())
```

Output, identical over streamable HTTP (`/mcp`) and stdio (`python -m aimo.mcp_server`):

```
12 ['decide_approval', 'get_audit_trail', 'get_mandate', 'get_offers', 'get_order', 'get_product', 'list_approvals', 'list_orders', 'parse_request', 'place_order', 'request_quote', 'search_products']
{"customer":"hotel-aurora","agent_id":"buyer-aurora","per_order_eur":2500.0,"per_week_eur":15000.0,"exclude_allergens":[],"allowed_categories":null,"substitutions":"within_prefs","preferred_brands":["Valio"],"approvers":["Laura Virtanen","Mikko Heikkinen"]}
```

## REST walkthrough: the milk request

```sh
B=http://localhost:8080; K='X-Agent-Key: demo-hotel-aurora'; H='content-type: application/json'
```

1. Mandate

```sh
curl -s $B/v1/mandate -H "$K"
{"customer":"hotel-aurora","agent_id":"buyer-aurora","per_order_eur":2500.0,"per_week_eur":15000.0,"exclude_allergens":[],"allowed_categories":null,"substitutions":"within_prefs","preferred_brands":["Valio"],"approvers":["Laura Virtanen","Mikko Heikkinen"]}
```

2. Parse

```sh
curl -s $B/v1/needs/parse -H "$K" -H "$H" -d '{"text":"I need 1000 litres of lactose-free milk by Monday","customer":"hotel-aurora"}'
{"customer":"hotel-aurora","deliver_by":"2026-11-16","items":[{"query":"milk","quantity":1000.0,"unit":"L","constraints":{"lactose_free":true,"gluten_free":null,"exclude_allergens":[],"labels":[],"origin":null,"max_unit_price":null,"brands":[]}}],"raw_text":"I need 1000 litres of lactose-free milk by Monday","summary":"1000 L milk, lactose-free, deliver by Mon 16 Nov 2026"}
```

3. Offers (send the Need back, without `summary`)

```sh
curl -s $B/v1/offers -H "$K" -H "$H" -d @need.json
items[0]: shortfall 0.0, offer 464119 "Valio rasvaton maito 1 l laktoositon UHT" (sales_unit "KI", 10.0 L), units_needed 100.0, unit_price_net 13.32, total_net 1332.0, total_gross 1512.0
```

4. Quote

```sh
curl -s $B/v1/quotes -H "$K" -H "$H" -d '{"offers":[{"product_id":"464119","quantity":100}],"delivery_date":"2026-11-16"}'
{"quote_id":"q_8e82adf5aebb","customer":"hotel-aurora","agent_id":"buyer-aurora","delivery_date":"2026-11-16","lines":[{"product_id":"464119","name":"Valio rasvaton maito 1 l laktoositon UHT","quantity":100.0,"sales_unit":"KI","unit_price_net":13.32,"line_total_net":1332.0,"substitute_for":null}],"total_net":1332.0,"total_gross":1512.0,"currency":"EUR","issued_at":"2026-11-14T17:14:55.065169Z","valid_until":"2026-11-15T15:00:00+02:00","jws":"eyJhbGciOiJFZERTQSIsImtpZCI6Im5IaHdFVUxPMUhULWZQMVlGNUV1UklKblpiSXhPX3JHSURzajhOV1J0QnMifQ..."}
```

Verify the signed quote independently against JWKS:

```sh
python -m aimo.quotes verify <jws> http://localhost:8080/.well-known/jwks.json
OK q_8e82adf5aebb customer=hotel-aurora agent=buyer-aurora lines=1 total_gross=1512.00 EUR delivery=2026-11-16 valid_until=2026-11-15T15:00:00+02:00
```

5. Order (redeems the quote, single use)

```sh
curl -s $B/v1/orders -H "$K" -H "$H" -d '{"quote_id":"q_8e82adf5aebb","jws":"<jws>"}'
{"order_id":"ord_28ddfb3eaf6d","quote_id":"q_8e82adf5aebb","customer":"hotel-aurora","agent_id":"buyer-aurora","lines":[{"product_id":"464119","name":"Valio rasvaton maito 1 l laktoositon UHT","quantity":100.0,"sales_unit":"KI","unit_price_net":13.32,"line_total_net":1332.0,"substitute_for":null}],"total_net":1332.0,"status":"confirmed","decision":{"outcome":"auto","reasons":[{"code":"WITHIN_MANDATE","text":"Order total 1,332.00 EUR is under the 2,500.00 EUR per-order limit and 1,332.00 EUR of the 15,000.00 EUR weekly budget"}]},"substitutions":[],"delivery_date":"2026-11-16","valid_until":"2026-11-15T15:00:00+02:00","created_at":"2026-11-14T17:14:55.414243Z","updated_at":"2026-11-14T17:14:55.417880Z"}
```

6. Audit

```sh
curl -s "$B/v1/audit?order_id=ord_28ddfb3eaf6d" -H "$K"
```

`text`:

```
19:14 Hotel Aurora's buyer agent was issued a signed quote. Quote q_8e82adf5aebb for 1,332.00 EUR net (1,512.00 EUR with VAT), valid until 15.11. 15:00 Helsinki time.
19:14 Hotel Aurora's buyer agent redeemed a signed quote. Quote q_8e82adf5aebb redeemed for 1,332.00 EUR net (1,512.00 EUR with VAT).
19:14 Hotel Aurora's buyer agent placed an order for 1,332.00 EUR. Agent submitted an order against a signed quote.
19:14 Mandate check passed: Within mandate: Order total 1,332.00 EUR is under the 2,500.00 EUR per-order limit and 1,332.00 EUR of the 15,000.00 EUR weekly budget.
19:14 The system placed an order for 1,332.00 EUR. Placed automatically within mandate.
19:14 Order confirmed by Valio (mock). Confirmed for delivery Mon 16 Nov (simulated fulfilment, Valio has no order API).
```

`verified`: `{"ok": true, "bad_seq": null}`. `checkpoint`: `{"seq": 8, "hash": "7b964bd908e6359dcb892a1763fcbd22c326acbdb7534b07f8478b545524b3d1", "ts": "2026-11-14T17:14:55Z", "jws": "eyJhbGciOiJFZERTQSIs..."}`.

Check the checkpoint independently:

```sh
python -m aimo.audit verify-checkpoint <jws> http://localhost:8080/.well-known/jwks.json
OK seq=8 hash=7b964bd908e6359dcb892a1763fcbd22c326acbdb7534b07f8478b545524b3d1
```

## Docker

Build and run the gateway container:

```sh
docker build -t aimo-gateway .
docker run --rm -p 8080:8080 --env-file .env -v $(pwd)/data:/app/data:ro aimo-gateway
```

Mount `data/` read-only and pass `.env` at run time; never bake secrets (`.env`, API keys, signing keys) or catalog data (`data/`) into the image (`.dockerignore` excludes `.env`, `data`, and `var`). Add `-v aimo-var:/app/var` when you want SQLite (`aimo.db`), upstream caches, and Ed25519 signing keys (`/app/var/keys`) to persist across restarts.
