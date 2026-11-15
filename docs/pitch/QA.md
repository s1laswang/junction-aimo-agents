# Judge Q&A: 23 hard questions

Short answers for the presenter, each with the file that backs it. Facts re-checked against main (fac34f0) on Sat 14 Nov 23:00–23:30 unless marked otherwise. If a judge pushes past an answer, say what is demo-only and point at the production path; never round a number up.

## Permissions and control

**1. Can the agent approve its own order?**
No. MCP has no approval tool (13 tools, none decide). Over REST, any decision request carrying `X-Agent-Id` gets 403 `agent_cannot_approve`, even when it names the real approver with the correct quote hash, and the attempt is audited as `approval.refused`. We show this live: `docs/pitch/permissions_attack.py`. Code: `decide()` in `aimo/gateway/app.py`; tests: `tests/test_human_auth.py`.

**2. Then what stops the agent from just dropping the `X-Agent-Id` header and calling the decision endpoint?**
It then needs the human credential: a signed approver session (`X-Approver-Session`) or the approver PIN. No route or tool ever returns either; without one the answer is 401 and the attempt is audited. Five wrong PINs from one client lock that client for 5 minutes (429), with a global backstop of 30 per 10 minutes. `aimo/gateway/human_auth.py`, docs/TRUST.md "Human-only approvals".

**3. Is a PIN really a person? Couldn't one approver act as another?**
Each approver in the directory has their own generated 12-character passphrase (`data/keys/approver_pin` is Anna's; the others are in `data/keys/approver_pins.json`, mode 600), and a passphrase only signs in or decides as its own approver: Anna's cannot act as Mikko (401, and it counts toward the lockout). Sign-in returns a session token signed for that one person, decisions are recorded under them, and a session that tries to decide as someone else gets 403 `approver_mismatch`. One demo-only shortcut remains: an operator credential set in `AIMO_APPROVER_PIN` is accepted for any approver, for test harnesses. Production replaces passphrases with the customer's own SSO (OIDC). `aimo/gateway/human_auth.py`, docs/TRUST.md.

**4. How do you know which agent is calling?**
In the demo, identity is two headers (`X-Customer-Id`, `X-Agent-Id`) checked against a mandate: an unregistered agent gets 403 `scope_missing`, and quotes and orders refuse to run without identity. The headers are not authenticated, so a caller that knows a registered pair can claim it. That is the biggest demo-only shortcut. Production: OAuth 2.1 client credentials per the MCP authorization spec, with spend limits as RFC 9396 `authorization_details`. docs/ASSUMPTIONS.md (A8), README "Production next steps".

**5. What if the agent lies, for example calls a substitute "preferred" to avoid approval?**
The gateway recomputes each line's substitution tier and flags itself and the policy re-verifies them; the agent's claims are ignored. A substitute sent as "preferred" comes back as "outside preference" and needs approval. The constraints of the parsed need are also locked once the agent calls `plan_need` or `create_quote`. `aimo/gateway/service.py` (`make_quote`), `aimo/buyer/agent.py` docstring.

**6. Who decides what is automatic and what needs a human?**
The customer, in a versioned YAML policy (`aimo/trust/policies/*.yaml`): spend limits, weekly budget, category scope, substitution tiers, allergen hard blocks, cut-off checks, drift, injection screening. Each rule returns a plain-language reason and the strictest outcome wins (reject > needs approval > auto). The €500 / €5,000 thresholds are illustrative and not validated with Valio. docs/ASSUMPTIONS.md.

**7. What exactly does the approver approve, and what if the price changes afterwards?**
The exact signed quote: Ed25519 JWS over canonical JSON, with a 15-minute price lock. The approval is bound to the quote hash; a different hash returns 409 `quote_hash_mismatch`, and an expired quote returns 409 asking for a re-quote. So an approval can never be reused for a different price, quantity or product. `aimo/trust/`, docs/TRUST.md.

## Audit and evidence

**8. Your audit log is signed with your own key. Couldn't you rewrite the whole chain?**
Whoever holds the signing key could re-sign a rewritten chain, yes. The chain stops silent edits by anyone else (our tamper demo: one changed summary turns verify red at that event), and the evidence pack lets an auditor verify offline with the public JWKS. Production: the key in a KMS/HSM, events in WORM storage, and chain heads anchored externally (for example sent to the customer with each order response), so even the operator cannot rewrite history unnoticed. docs/ARCHITECTURE.md §5, README "Production next steps".

**9. How do we know your numbers are real?**
Every number is in docs/NUMBERS.md with the command that produces it and who re-ran it when. The montage cost numbers ($0.0822 before the token trim, $0.0352 per order on the final code) recompute offline from the sanitized Gemini ledgers committed in docs/evidence/ (`uv run python docs/evidence/montage_stats.py`). Numbers we have not measured (gold-set accuracy, photo-intake accuracy) are listed as not claimed.

## Data and confidentiality

**10. What data goes to Gemini, and what about a real customer's confidential prices?**
The buyer agent sends the request text and compacted tool results (product names, prices, stock) to Gemini; trust and catalog calls send product text. The ledger stores token counts and latency only, never prompts or keys (`aimo/gemini.py`). All data here is the challenge's synthetic catalog and invented customers. For production we would run Gemini through Vertex AI in an EU region under enterprise terms (no training on customer data). The gateway itself never needs the model: any agent, including one the customer hosts, can use the same MCP tools. docs/DECISIONS.md, docs/ASSUMPTIONS.md.

**11. Can one customer's agent see another customer's orders or approvers?**
An agent's approval inbox is scoped to its own customer (asking for another returns 403 `customer_mismatch`), and `/v1/customers` shows agents approver names and roles but never their ids or emails. Given the header-only identity in Q4, this isolation becomes real once OAuth tokens replace the headers. `aimo/gateway/app.py` (`approvals`), docs/TRUST.md.

**12. What about prompt injection hidden in supplier product text?**
Two layers, and we report both honestly. The regex screen, which the shipped gateway runs, raises no false alarms on 34,159 real supplier texts but catches only 44% of a separate 36-payload evaluation set. The Gemini classifier reaches 94% on that set but also flags 42 of 60 normal buyer requests, so it is opt-in (`AIMO_SCREEN_GEMINI=1`). The buyer harness redacts flagged text before the model sees it, and a flagged order needs a human; the real protection is that the policy, the signed quote and the human approval do not depend on the screen catching everything. In the live montage, 6 planted texts were flagged and redacted and the order quantity stayed at 300; in the final-code wind tunnel, the regex screen flagged 80 of 80 of the scenarios' injection payloads (supplier text and requests, including the product query) with 1 false positive in 560 clean requests; those payloads are easier than the evaluation set. `aimo/trust/screening.py`, docs/windtunnel-results.md.

## Robustness and failure modes

**13. What happens when the Valio API is slow, rate-limited or down?**
The client batches up to 1,000 ids, caches for 5 minutes and retries with backoff; after that the caller gets a clear 503 with `Retry-After`, never a guessed price. Unavailable quote lines are refused with per-line reasons (409) so the agent re-plans. If the upstream stays down, the gateway serves the bundled recorded snapshot, marked `served_from: replay` (the UI shows a REPLAY pill); with no key at all, a clone runs entirely in replay, and `make accept` passes that way. If stock drops below the quoted quantity while the order waits for a human, approval returns 409 `requote_needed` with a fresh quote, never a partial order. Every exception can be reproduced per request with `X-Aimo-Faults`, which is operator-only (`AIMO_DEMO_FAULTS=1`; ignored for agent requests otherwise). Bad input never crashes the gateway: a fuzz test sends about 2,600 malformed requests to every `/v1` route with 0 server errors, and agent runs that cannot reach the gateway fail within seconds instead of hanging (`tests/test_fuzz.py`, `aimo/buyer/runs.py`). `aimo/backend/client.py`, `aimo/backend/replay.py`, docs/interfaces.md.

**14. What if Gemini fails or hallucinates?**
Need parsing has a deterministic cross-check: relative dates are re-resolved in Helsinki time, and the regex detector can add a claim or allergen but never remove one. Gemini allergen readings can only block a product, never clear it, and unknown allergens are never "safe". The harness allows at most one `place_order` per run and stops after a rejection. The Gemini wrapper retries 429/5xx; if Gemini stays down, need parsing falls back to the deterministic parser. Whatever the model does, an order needs a valid signed quote and, above the customer's limits, a human. `aimo/buyer/need.py`, `aimo/catalog/gemini_allergens.py`.

**15. Your escalation precision is only 68%. Isn't the gateway just asking a human about everything?**
It errs towards asking, on purpose. On 640 Gemini-written scenarios: recall 99.7% and 0 policy and 0 allergen violations, against 372 and 193 for a naive agent. Precision went from 54% to 68% overnight: the gateway now verifies substitution tiers against the catalog itself, treats a first purchase as a purchase rather than a swap, auto-approves verified alternatives under a small amount (hotel: €50), and applies remembered "always allow this swap" decisions. Our 90% target is not met. The largest remaining causes are multi-item recipe quotes (39), stricter gluten handling than the oracle (27), injection-screen hits on request text (21) and our 15% price-jump rule (16). Each one could be relaxed, but each would weaken a guardrail. docs/windtunnel-results.md "S3 need", docs/reports/need.md.

**16. What about splitting one big order into many small ones to stay under the limit?**
The `split_order` rule adds up orders of the same thing (same product, standing-order product, category or request phrase) placed without a human in the last 7 days: once they pass the auto limit, the next one needs approval (3 × €399.96 of milk at the hotel: auto, then needs approval on the second). Quantity drift also counts the last 7 days, the weekly budget sums all orders, and a unit price more than 15% above the last price paid needs approval (`price_jump`). After these changes the wind tunnel shows 0 policy violations, including the split-order family. `aimo/trust/policy.py`, docs/reports/need.md.

## Openness and production

**17. Why MCP and not just a REST API?**
Both, from one service layer: MCP because the agents buyers actually use (Claude, Gemini CLI, Cursor, agent frameworks) discover and call MCP tools with no integration code, and OpenAPI 3.1 for ERPs and classic integrations. Discovery is one manifest (`/.well-known/otap.json`). We showed it is not tied to us: a second, synthetic wholesaler passes the same 44-check conformance suite, and the same Gemini agent bought from it with only the URL changed. docs/OPENNESS.md, `conformance/`.

**18. Is the order real? Valio's API has no order endpoint.**
Prices and availability are live from the challenge's mock API. Orders go to our own mock order service behind the same `BackendClient` interface a SAP adapter would implement, and the answer is a Peppol-style order response (AP/CA/RE), exportable as Peppol BIS T116 UBL XML validated against the UBL 2.3 XSD (not the full schematron). README "Openness", docs/peppol/.

**19. What is the path to production?**
Read-only first, then quotes, then orders with every order human-approved, then autonomy inside limits relaxed one rule at a time from audit evidence (allergen rules never relax for schools and hospitals). In parallel: SAP adapter for contract prices and real available-to-promise, GS1 Synkka as the allergen truth source, OAuth 2.1 and customer SSO, KMS keys, WORM audit, Peppol through Valio's access point. Valio legal must confirm whether a signed quote is a binding offer. README "Production next steps", docs/ARCHITECTURE.md §5.

**21. Why does it ask a human this often (68% precision)?**
Because it is built to stop when unsure. In the final-code wind tunnel (640 Gemini-written scenarios) it escalated 331 times when it had to and 154 times when auto-approval would also have been acceptable, while letting through 0 policy and 0 allergen violations and missing 1 required escalation (recall 99.7%). Of the 154, 37 are deliberate stops: 21 detected injection attempts and 16 price jumps over 15%. Most of the rest is conservatism about products that are not the customer's usual ones (substitutions in school and hospital kitchens, recipe quotes with several ingredients, claim-only "gluten-free"). Each human "always allow this swap" teaches the gateway that swap, so the same question is not asked again, and small verified alternatives under the customer's limit (hotel: €50) already go through alone. docs/windtunnel-results.json (`main`), docs/reports/need.md, `aimo/trust/learned.py`.

**22. Can an agent move the clock or fake stock?**
No. The demo clock (`X-Aimo-Now`) and fault injection (`X-Aimo-Faults`: stock-outs, price shocks, injected text) are operator tools: on any request carrying agent identity, over REST or MCP, the gateway ignores them unless the operator started it with `AIMO_DEMO_FAULTS=1`, and never with `AIMO_ENV=production`. So an agent cannot backdate "now" past a cut-off or invent stock. Prices and stock come from the backend; the quote is signed; and if stock drops while the order waits for approval, the approval returns 409 `requote_needed` instead of a partial order. `tests/test_overrides.py`, `tests/test_gateway.py::test_partial_stock_at_approval_requotes_instead_of_silently_less`, docs/TRUST.md (tail).

**23. What happens if Gemini runs out of quota during the demo?**
It happened for real at 05:00 on Sunday: the shared key hit gemini-3.8-flash's daily limit and every agent run failed with 429. Now `aimo.gemini.generate` falls back at once to the next model in `GEMINI_FALLBACK_MODELS` (default gemini-3.5-flash, then gemini-4-hydrogen), skips the exhausted model for 30 minutes, and the agent's steps and the ledger name the model that actually answered; the hero run then placed its order in 29 s on gemini-3.5-flash. If no model answers, the run stops with an error and nothing is ordered; the keyless scripted model and `qa_milk_flow.py` still show the whole flow. Our committed montage numbers were measured on gemini-3.8-flash. `aimo/gemini.py`, `tests/test_gemini_fallback.py`.

## Gemini

**20. Why Gemini, and what does it cost?**
The catalog is mostly Finnish and Swedish; Gemini reads it directly, and gemini-embedding-2 puts "laktoositon maito" next to "lactose-free milk" in the same API. Thinking levels let us spend reasoning only on the substitution decision: routine steps run on gemini-3.8-flash LOW, and only the quote-decision turn escalates to gemini-4-hydrogen HIGH (5 of 34 steps in the final montage). We measured that choice: hydrogen HIGH was 100% accurate on both parse and substitution tasks with p95 under 7.2 s, while flash HIGH came close (100% parse, 97.9% substitution) but hit a 69.5 s p95 on parsing. Cost: $0.0352 of Gemini per placed order on the final code, down 57% from before a token trim, with 38% of input served from the implicit cache. docs/GEMINI.md, docs/evidence/.
