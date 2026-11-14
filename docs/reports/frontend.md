# Frontend report (team/frontend)

Last updated: Sat 14 Nov 2026, 14:50 Helsinki. Owner: aimo-frontend. Paths: `web/` only.

## Status: M2 acceptance met (mock and live)
- **Mock (`?mock=1`)** plays the whole demo with no backend: run, live timeline, plan, needs approval, approve, Peppol response AP, audit trail, Verify chain green. "Simulate tampering" turns the chain red and highlights the altered event.
- **Live, end to end, verified at 14:45** on a local integration of origin/main + team/gateway + team/buyer (the buyer's `aimo/gemini.py` included): a real Gemini run streams over SSE (parsed need, MCP tool calls, plan with 15 candidates, signed quote) and returns **needs approval by Procurement Manager** (spend, substitution, 3.3× volume). Approve in the inbox: the agent resumes, the order is placed with **AP**, the final summary shows, and **Verify chain is green (15 events)**. Every step was run through the real UI by clicking (headless Chrome over CDP).
- The quote signature is **verified in the browser** (WebCrypto Ed25519 against `/.well-known/jwks.json`). It works live with the trust key and in mock with a browser-generated key.

Screenshots: [live plan](frontend/live-plan.png), [live run approved + order response](frontend/live-run-approved.png), [live audit verified](frontend/live-audit-verified.png), [live approval inbox](frontend/live-approval.png), [mock plan, full page](frontend/mock-plan.png).

## How to open
- Live: `uv run aimo serve`, then http://localhost:8000/ui/ (the gateway serves `web/` at `/ui`).
- Mock, no backend: `cd web && python -m http.server 8765`, then http://127.0.0.1:8765/index.html?mock=1 (also works at `/ui/?mock=1`).
- Gateway on another origin: `?api=http://localhost:8000`.
- Demo and recording helpers: `&autorun=1` (start the run), `&autoapprove=1` (approve when the agent waits), `&tab=plan|approvals|audit`, `&customer=school-tampere`.

## What's on screen
1. **Agent console (left):** customer selector and mandate (from `/v1/customers`), request, model (gemini-3.8-flash / gemini-4-hydrogen / gemini-3.1-flash-lite), thinking (minimal to high), and Run. The live timeline shows need chips, each tool call (args, latency, JSON), and exception callouts: cut-off passed, partial stock, allergen, prompt injection, volume drift, and harness retries as warnings. The request card collapses during a run.
2. **Plan tab:** need, covered amount and total, the explanation, the proposed lines, then good-better-best cards (€/L, pack, unit price, available, in-plan or if-chosen quantity and cost, reliability, CO2, reasons, flags). Rejected candidates collapse with their reasons.
3. **Approvals tab:** the inbox (polled every 5 s), a verdict banner with every RuleHit, and the signed quote (lines, totals, price-lock countdown, hash, signature badge). The approver comes from the policy's directory. Approve/Reject then shows the order response first.
4. **Audit tab:** the hash-chain rail (agent/human/system nodes, links verified by prev_hash), per-event hash, signature and kid, Verify chain, the readable report, the evidence pack JSON, and a choice of "This flow" or "Whole ledger".
5. **Header:** links to OpenAPI, otap.json, JWKS and MCP. The "Connect your agent" dialog shows MCP HTTP and stdio configs and curl.

## Contract the UI consumes
- SSE: each message's `data` is one AgentStep (the event name is ignored), deduped by `seq`. **Only `final` ends a run.** `error` steps are non-fatal warnings. The run counts as failed when `final.data.status == "error"`.
- Step data read (all optional): `thought` {text, need, drift, excerpt, reasons}; `tool_call` {name, args}; `tool_result` {name, latency_s|latency_ms, result, summary?, exceptions?}; `plan` {plan}; `quote` {quote}; `decision` {decision, order_id, status}; `approval_wait` {order_id, approver_role}; `order` {order}; `final` {status, summary}. Exceptions are derived from Offer lists (`cutoff_passed`, `issues`).
- `POST /v1/agent/runs` {text, customer_id, model, thinking}. `thinking` is sent uppercase (MINIMAL|LOW|MEDIUM|HIGH), per `aimo/buyer/runs.py`.
- Decision: `{decision, approver, approver_role, note, quote_hash}`. Errors are read from `{error, message, detail}`.
- `available_amount ≥ 1e8` (the mock API's unlimited sentinel) shows as "in stock".

## Integration notes for the CEO and other owners
- **Merge order matters:** team/buyer changes `aimo/gemini.py` (`generate(meta=...)`). With main's version, live runs fail with `generate() got an unexpected keyword argument 'meta'`. The UI shows this cleanly as a failed run.
- **gateway:** the audit actor sometimes doubles the customer (`agent:hotel-aurora/hotel-aurora/buyer-1`, seen when the agent id is already qualified).
- **gateway/trust:** `approved_by` comes back as `"anna@hotel-aurora.example (procurement_manager)"`, so the UI can't map it to the directory name. A bare id plus a separate role field would display better (cosmetic).
- The UI only needs `web/` served at `/ui` (already in gateway `create_app`).

## Verified vs untested
- Verified: the mock hotel and school flows, mock tamper detection, the live full flow (above), the live REST-created order approved via UI, and the live JWS verification.
- Untested: live school and hospital runs, a live reject path (the mock reject path works), and phone-width layout (CSS breakpoints exist, not checked visually).

## Mock data honesty
Product ids and names are real catalog entries. Hotel prices, stock and cut-off come from PLAN.md and match the live plan (464306 €1.01, 469363 €2.48 with 384 units, 463038 cut-off Thu 12 Nov 15:00). Values marked ILLUSTRATIVE in `web/mock.js` (school prices and stock, 462775's price) are for playback only. Mock mode always shows "MOCK DATA · no backend".

## Needs a decision (Silas / CEO)
- Live "Simulate tampering": trust has `tamper_for_demo`, but no HTTP route exposes it. If the gateway adds a demo-only route, the live demo can show the chain turning red. Mock already does.
