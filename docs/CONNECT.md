# Connect your own agent

MCP and REST, with client configs and the full curl walkthrough. Back to the [README](../README.md).

The gateway is agent-agnostic. External agents connect via MCP (`/mcp` or stdio) or REST (`/v1`) using two demo identity headers (`X-Customer-Id`, `X-Agent-Id`) or, over stdio, the env vars `AIMO_CUSTOMER_ID` / `AIMO_AGENT_ID` (plus optional per-customer `X-Agent-Key` / `AIMO_AGENT_KEY` when `AIMO_AGENT_KEYS` is configured). Mutating `/v1` routes, catalog search (`/v1/products/search`) and MCP tools require a known customer (`X-Customer-Id` / `AIMO_CUSTOMER_ID`, or body/query `customer_id` where accepted; `401 identity_required` when missing, `403 unknown_customer` when unknown; quotes and orders also require `X-Agent-Id` / `AIMO_AGENT_ID`, and `GET /v1/approvals` is scoped to the caller's customer unless a human approver credential is present). Hosted agent runs (`/v1/agent/runs`: starting, answering or listing runs from `/ui`) are human-only and require a signed-in human (`X-Approver-Session` or `X-Approver-Pin`, 401 otherwise, for a known customer). In production agent identity becomes an OAuth 2.1 bearer token per the MCP authorization spec and human identity uses the customer's SSO.

MCP tools (13, from `build_mcp().list_tools()`, 9 core `otap/0.1` profile tools + 4 extensions, 0 approval tools): `search_products`, `get_product`, `get_offers`, `plan_need`, `create_quote`, `place_order`, `get_order_status`, `get_audit_trail`, `list_policies`, `list_recipes`, `plan_recipe`, `check_standing_order_drift`, `propose_offer`. There is deliberately no approval tool: an agent cannot approve its own order.

**Claude Desktop** (`claude_desktop_config.json`, stdio):
```json
{
  "mcpServers": {
    "valio-aimo": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/junction-aimo-agents", "run", "aimo", "mcp"],
      "env": { "AIMO_CUSTOMER_ID": "hotel-aurora", "AIMO_AGENT_ID": "buyer-1" }
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`, streamable HTTP, with `uv run aimo serve` running):
```json
{
  "mcpServers": {
    "valio-aimo": {
      "url": "http://localhost:8000/mcp",
      "headers": { "X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1" }
    }
  }
}
```

**Gemini CLI** (`~/.gemini/settings.json`, streamable HTTP):
```json
{
  "mcpServers": {
    "valio-aimo": {
      "httpUrl": "http://localhost:8000/mcp",
      "headers": { "X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1" }
    }
  }
}
```

Check the server from the terminal: `uv run aimo mcp-check` (HTTP, or `make mcp-check`) or `uv run aimo mcp-check stdio`. The server advertises 13 tools (the 9 core `otap/0.1` trade tools plus the 4 extensions `list_recipes`, `plan_recipe`, `check_standing_order_drift` and `propose_offer`, with 0 approval tools). `mcp-check` also calls `search_products` and `get_offers` when the catalog file is present. The three client config snippets follow each client's documented format and have not been tried in those clients yet.

**REST with curl** (the 1,000 L milk flow; every command below was run against a live gateway):
```bash
H=(-H "X-Customer-Id: hotel-aurora" -H "X-Agent-Id: buyer-1" -H "Content-Type: application/json")

# 1. Search (Finnish works too: "laktoositon maito")
curl -s "http://localhost:8000/v1/products/search?q=lactose-free%20milk&limit=5" "${H[@]}"

# 2. Live price and availability: 463038 has cutoff_passed=true, 464306 is in stock at €1.01
curl -s -X POST http://localhost:8000/v1/offers "${H[@]}" \
  -d '{"product_ids":["463038","464306","469363"],"requested_date":"2026-11-16"}'

# 3. Plan: good-better-best plus the order split (best = 464306 x 1000, €1,010)
curl -s -X POST http://localhost:8000/v1/plan "${H[@]}" \
  -d '{"customer_id":"hotel-aurora","product_query":"lactose-free milk","required_claims":["FREE_FROM_LACTOSE"],
       "quantity":1000,"unit":"L","delivery_by":"2026-11-16","preferred_product_ids":["463038"]}'

# 4. Signed quote (live re-priced, fixed for 15 min; the gateway sets tier and flags): note quote_id and quote_hash
curl -s -X POST http://localhost:8000/v1/quotes "${H[@]}" \
  -d '{"delivery_date":"2026-11-16","lines":[{"product_id":"464306","qty_sales_units":1000}]}'

# 5. Order: returns status pending_approval with every rule's reason
curl -s -X POST http://localhost:8000/v1/orders "${H[@]}" -d '{"quote_id":"Q-...","quote_hash":"..."}'

# 6. The agent tries to approve its own order: 403 agent_cannot_approve (audited as approval.refused)
curl -s -X POST http://localhost:8000/v1/approvals/ORD-.../decision "${H[@]}" \
  -d '{"decision":"approve","approver":"anna@hotel-aurora.example","quote_hash":"..."}'

# 7. A human signs in once (Anna's credential from AIMO_APPROVER_PINS / AIMO_APPROVER_PIN or data/keys/approver_pin),
#    then approves the exact hash (a wrong hash returns 409 quote_hash_mismatch; expired lock or partial stock returns 409 requote_needed)
S=$(curl -s -X POST http://localhost:8000/v1/auth/approver-session -H "Content-Type: application/json" \
  -H "X-Approver-Pin: $(cat data/keys/approver_pin)" -d '{"approver":"anna@hotel-aurora.example"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session'])")
curl -s -X POST http://localhost:8000/v1/approvals/ORD-.../decision -H "Content-Type: application/json" \
  -H "X-Approver-Session: $S" -d '{"decision":"approve","quote_hash":"..."}'

# 8. Audit: readable story (including the refused agent attempt), and chain verification
curl -s http://localhost:8000/v1/audit/report/ORD-...
curl -s http://localhost:8000/v1/audit/verify
```
Steps 4-8 were re-run on Sat 14 Nov 23:10 against a replay gateway (the earlier 92-product snapshot; keyless `make accept` now runs the same flow on the current 669-product replay snapshot): quote €1,010, `pending_approval`, agent 403, session approval -> `placed`, report shows `#11 approval.refused`, verify `ok: true`.
The same flow as one script with checks: `uv run python docs/pitch/qa_milk_flow.py --base http://localhost:8000`.

Demo fault injection is operator-only: the header `X-Aimo-Faults` (and the clock header `X-Aimo-Now`) on any request carrying agent identity, and body `faults` or caller-chosen `inproc`/`fake` transports on `POST /v1/agent/runs`, are ignored unless the gateway was started with `AIMO_DEMO_FAULTS=1`, and never honoured with `AIMO_ENV=production`; the env `AIMO_FAULTS` applies server-wide. Faults: `stockout:<id|*>`, `partial:<id|*>:<n>`, `price_shock:<id|*>:<factor>`, `cutoff:<id|*>`, `error_rate:<p>`, `rate_limit:<p>`, `latency_ms:<n>`. See [docs/interfaces.md](interfaces.md).
