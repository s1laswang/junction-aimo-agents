# Frontend report (team/frontend)

Last updated: Sat 14 Nov 2026, 14:45 Helsinki. Owner: aimo-frontend. Paths: `web/` only.

## Status
- **Mock mode is complete.** `?mock=1` plays the whole demo story with no backend: run, live timeline, plan, needs approval, approve, order response (AP), audit trail, chain verified green, and "Simulate tampering" turns it red.
- **Live mode works against the real gateway, trust and catalog code** (tested on a local integration of origin/main + team/gateway + team/catalog + team/buyer). Verified live: customers and mandates, approval inbox with the signed quote, **Ed25519 signature verified in the browser against `/.well-known/jwks.json`**, approve, order placed (AP), audit trail for the flow, `GET /v1/audit/verify` green.
- **Not yet live: the agent console.** `POST /v1/agent/runs` and its SSE stream don't exist yet (the gateway mounts `aimo.buyer.runs` when it lands, and team/buyer doesn't have it yet). The UI is built against the AgentStep shapes that `aimo/buyer/agent.py` emits today (see the contract below).

## How to open
- Mock, no backend: `cd web && python -m http.server 8765`, then open http://127.0.0.1:8765/index.html?mock=1
- Live: `uv run aimo serve`, then open http://localhost:8000/ui/ (the gateway serves `web/` at `/ui`).
- Separate preview server against a gateway: `...index.html?api=http://localhost:8000`
- Demo and recording helpers: `&autorun=1` starts the run, `&autoapprove=1` approves when the agent waits, `&tab=plan|approvals|audit` opens a tab, `&customer=school-tampere` picks the customer.

## What's on screen
1. **Agent console (left):** customer selector with a mandate summary (from `/v1/customers`), request box, model and thinking selectors, and Run. A live timeline shows parsed-need chips, each tool call (name, args, latency, expandable JSON), and exceptions as callouts: cut-off passed, partial stock, allergen unknown or contains, prompt injection, volume drift, and harness retries. When a run starts, the request card collapses so the timeline gets the room.
2. **Plan tab:** need chips, covered amount and total, the agent's explanation, the proposed order lines, then good-better-best cards (€/L, pack, unit price, available, quantity and line total in the plan or "if chosen", reliability, CO2, reasons, flags). Rejected candidates collapse with their reason.
3. **Approvals tab:** the inbox (`GET /v1/approvals?status=pending`, polled every 5 s in live mode) and the verdict banner with every RuleHit in plain language. The signed quote shows lines, totals, a price-lock countdown, the hash and a signature badge (really verified in the browser). The approver comes from the policy's approver directory, preset to the required role. Approve/Reject sends `{decision, approver, approver_role, note, quote_hash}`. The Peppol order response (AP/CA/RE per line) then shows first.
4. **Audit tab:** a hash-chain rail (each node is agent, human or system, and the link turns blue when `prev_hash` matches the previous event). Each event shows its summary, with hash, prev_hash, signature and kid behind a disclosure. Also on the tab: Verify chain, a readable markdown report (`/v1/audit/report/{order_id}`), and "Download evidence pack" (JSON: request, need, plan, quote, decision, order, agent steps, audit events, verification result and JWKS). You can switch the scope between "This flow" and "Whole ledger".
5. **Openness strip (header):** links to OpenAPI `/docs`, `/.well-known/otap.json`, JWKS and `/mcp`. The "Connect your agent" dialog has MCP configs for HTTP and stdio and a curl example.

Screenshots: [mock plan](frontend/mock-plan.png), [live approval with verified signature](frontend/live-approval.png), [live audit verified](frontend/live-audit-verified.png).

## Contract the UI consumes (please keep it, or tell me)
- SSE `GET /v1/agent/runs/{id}/events`: each message's `data` is one AgentStep JSON. Any event name works (default `message`, a kind name, or `step`). Duplicate `seq` values are ignored, so reconnect replays are safe. **Only `kind: "final"` ends the run.** `error` steps are shown as non-fatal warnings, which matches the buyer's retry and fallback emits. A run counts as failed only when `final.data.status == "error"`.
- Step data the UI reads, all optional: `thought` {text, need, drift, excerpt, reasons}; `tool_call` {name, args, call_id?}; `tool_result` {name, latency_s or latency_ms, result, summary?, exceptions?: [{code, message, product_id}]}; `plan` {plan}; `quote` {quote}; `decision` {decision, order_id, status, order?}; `approval_wait` {order_id, approver_role}; `order` {order}; `final` {status, summary}. When `tool_result.result` is a list of Offers, the UI derives the exceptions itself from `cutoff_passed`/`issues`.
- `POST /v1/agent/runs` body: `{text, customer_id, model, thinking}`. **`thinking` is UNVERIFIED** (it isn't in interfaces.md). The UI also sends the `X-Customer-Id` header.
- Errors: reads `{error, message, detail}` (gateway ServiceError). 409 on decision is shown as "quote changed or expired, ask for a new quote".
- An `available_amount` ≥ 1e8 (the mock API's unlimited sentinel, e.g. 9999999999) is shown as "in stock".

## Found while integrating (for other owners)
- **gateway:** audit actor doubles the customer, e.g. `agent:hotel-aurora/hotel-aurora/buyer-1` (`Identity.actor` prefixes the customer even when `agent_id` already contains it). Seen in the live audit trail.
- **buyer:** `aimo.buyer.runs` (router with `POST /v1/agent/runs` + SSE) isn't on team/buyer yet. The gateway already tries to mount it.
- **buyer/catalog:** a `tool_result` with a short `summary` and an `exceptions` list (cut-off passed, partial stock) would make the timeline read better than the generic `"get_offers: 4 result(s)"` title. This is optional, because the UI already derives exceptions from Offer lists.

## Verified vs untested
- Verified (headless Chrome via CDP, 1280×800): the mock hotel flow end to end, the mock school flow (auto-approve, allergen rejections, injection flagged), mock tamper detection, and live approval → placed → audit verify green with the real gateway, trust and catalog.
- Untested: the live SSE agent run (endpoint missing), and hospital-meilahti in mock (not scripted; it shows an honest "no mock script" end state). The phone layout was not checked visually (CSS has breakpoints at 1100 and 900 px).

## Mock data honesty
Mock product ids and names are real catalog entries. The 464306 / 469363 / 462775 / 463038 hotel prices, stock and cut-off come from the demo script in PLAN.md. Values marked ILLUSTRATIVE in `web/mock.js` (the school oat drink prices and stock, 462775's price, 464306's 4,200 units) are invented for playback only. The header shows "MOCK DATA · no backend" whenever mock mode is on.

## Needs a decision (Silas / CEO)
- Should the "Simulate tampering" button exist in live mode? Trust has `tamper_for_demo`, but no HTTP endpoint exposes it. If the gateway adds `POST /v1/audit/_demo/tamper` (disabled in production), the live demo can show the chain turning red.
