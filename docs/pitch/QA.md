# Judge Q&A: 20 hard questions

Short answers for the presenter, each with the file that backs it. Facts re-checked against main (fac34f0) on Sat 14 Nov 23:00–23:30 unless marked otherwise. If a judge pushes past an answer, say what is demo-only and point at the production path; never round a number up.

## Permissions and control

**1. Can the agent approve its own order?**
No. MCP has no approval tool (13 tools, none decide). Over REST, any decision request carrying `X-Agent-Id` gets 403 `agent_cannot_approve`, even when it names the real approver with the correct quote hash, and the attempt is audited as `approval.refused`. We show this live: `docs/pitch/permissions_attack.py`. Code: `decide()` in `aimo/gateway/app.py`; tests: `tests/test_human_auth.py`.

**2. Then what stops the agent from just dropping the `X-Agent-Id` header and calling the decision endpoint?**
It then needs the human credential: a signed per-approver session (`X-Approver-Session`) or that approver's 12-character passphrase (`X-Approver-Pin`). No route or tool ever returns either; without one the answer is 401 and the attempt is audited. Five wrong guesses from one client for an approver lock that `(approver_id, client)` pair for 5 minutes (429 `approver_locked`), with a per-approver backstop of 30 per 10 minutes across all clients so attacking one approver never locks out other approvers or active sessions. `aimo/gateway/human_auth.py`, docs/TRUST.md "Human-only approvals".

**3. How are approver credentials isolated per person in the demo vs production?**
Each directory approver gets a distinct 12-character passphrase generated into `data/keys/approver_pins.json` (mode `0600`, overridable via `AIMO_APPROVER_PINS`, with `data/keys/approver_pin` synced to Anna's passphrase for local CLI scripts and `AIMO_APPROVER_PIN` kept only as an optional single-credential test override). One approver's passphrase cannot sign in or decide as another approver (401), a sign-in returns an 8-hour `X-Approver-Session` HMAC-bound to that person and their credential, and a session trying to decide as someone else gets 403 `approver_mismatch`. Production replaces local passphrases with the customer's own SSO (OIDC). `aimo/gateway/human_auth.py`, docs/TRUST.md, docs/ASSUMPTIONS.md.

**4. How do you know which agent is calling?**
In the demo, identity is two headers (`X-Customer-Id`, `X-Agent-Id`) checked against the known-customer directory and mandate (`aimo/gateway/auth.py`): an unknown customer gets 403 `unknown_customer` across `/v1`, catalog search, MCP and `/v1/agent/runs`, an unregistered agent gets 403 `scope_missing`, and quotes and orders refuse to run without identity. Unless `X-Agent-Key` is configured per customer, the demo headers are not cryptographically authenticated, so a caller that knows a registered pair can claim it. That is the biggest demo-only shortcut. Production: OAuth 2.1 client credentials per the MCP authorization spec, with spend limits as RFC 9396 `authorization_details`. docs/ASSUMPTIONS.md (A8), README "Production next steps".

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
Every number is in docs/NUMBERS.md with the command that produces it and who re-ran it when. The montage cost numbers ($0.0822 → $0.0339 per order) recompute offline from the sanitized Gemini ledgers committed in docs/evidence/ (`uv run python docs/evidence/montage_stats.py`). Numbers we have not measured (gold-set accuracy, photo-intake accuracy) are listed as not claimed.

## Data and confidentiality

**10. What data goes to Gemini, and what about a real customer's confidential prices?**
The buyer agent sends the request text and compacted tool results (product names, prices, stock) to Gemini; trust and catalog calls send product text. The ledger stores token counts and latency only, never prompts or keys (`aimo/gemini.py`). All data here is the challenge's synthetic catalog and invented customers. For production we would run Gemini through Vertex AI in an EU region under enterprise terms (no training on customer data). The gateway itself never needs the model: any agent, including one the customer hosts, can use the same MCP tools. docs/DECISIONS.md, docs/ASSUMPTIONS.md.

**11. Can one customer's agent see another customer's orders or approvers?**
An agent's approval inbox is scoped to its own customer (asking for another returns 403 `customer_mismatch`), and `/v1/customers` shows agents approver names and roles but never their ids or emails. Given the header-only identity in Q4, this isolation becomes real once OAuth tokens replace the headers. `aimo/gateway/app.py` (`approvals`), docs/TRUST.md.

**12. What about prompt injection hidden in supplier product text?**
Two layers: a regex screen (0 false positives on 34,159 real supplier texts) and a Gemini classifier (94.4% recall on our payload set). Every `PlanLine` and `Candidate` surfaces `injection_screen` during planning (`aimo/catalog/screening_bridge.py`), the buyer harness redacts flagged text before the model sees it, and a flagged order needs a human. In the live montage, 4 planted texts were flagged and redacted and the order quantity stayed at 300. Gemini's false-positive rate on buyer requests is high (42 of 60), which only costs an extra approval, never a wrong order. `aimo/trust/screening.py`, docs/windtunnel-results.md.

## Robustness and failure modes

**13. What happens when the Valio API is slow, rate-limited or down?**
The client batches up to 1,000 ids, caches for 5 minutes and retries with backoff; after that the caller gets a clear 503 with `Retry-After`, never a guessed price. Unavailable quote lines are refused with per-line reasons (409) so the agent re-plans. With `AIMO_REPLAY_FALLBACK` set, the gateway serves recorded responses marked `served_from: replay`; `AIMO_REPLAY` runs fully offline. Every exception can be reproduced per request with `X-Aimo-Faults`. `aimo/backend/client.py`, `aimo/backend/replay.py`, docs/interfaces.md.

**14. What if Gemini fails or hallucinates?**
Need parsing has a deterministic cross-check: relative dates are re-resolved in Helsinki time, and the regex detector can add a claim or allergen but never remove one. Gemini allergen readings can only block a product, never clear it, and unknown allergens are never "safe". The harness allows at most one `place_order` per run and stops after a rejection. The Gemini wrapper retries 429/5xx; if Gemini stays down, need parsing falls back to the deterministic parser. Whatever the model does, an order needs a valid signed quote and, above the customer's limits, a human. `aimo/buyer/need.py`, `aimo/catalog/gemini_allergens.py`.

**15. Your escalation precision is 54%. Isn't the gateway just asking a human about everything?**
It errs towards asking: recall is 98.8%, so almost nothing that needs a human slips through, and the gateway had 1 policy violation against the naive agent's 372 on 640 scenarios. 54% precision means about one unnecessary approval per necessary one, which is the next thing to tune (for example auto-approving verified alternatives below a small amount, and remembered "always allow" swaps). docs/windtunnel-results.md. *(Re-check this answer against the latest windtunnel-results.md before going on stage: the need session is re-measuring it.)*

**16. What about splitting one big order into many small ones to stay under the limit?**
The weekly-budget rule sums every order the customer placed in the last 7 days, so repeated small orders hit it, and standing-order drift compares volume against the usual weekly baseline (the montage's 13× order was held). In the wind tunnel, split-order aggregation beyond the written policy is labelled `beyond_policy` and counted against recall, not hidden. `aimo/trust/policy.py`, docs/windtunnel-results.md "Definitions and caveats". *(Re-check against main before stage: the need session is adding a rolling per-product window.)*

## Openness and production

**17. Why MCP and not just a REST API?**
Both, from one service layer: MCP because the agents buyers actually use (Claude, Gemini CLI, Cursor, agent frameworks) discover and call MCP tools with no integration code, and OpenAPI 3.1 for ERPs and classic integrations. Discovery is one manifest (`/.well-known/otap.json`). We showed it is not tied to us: a second, synthetic wholesaler passes the same 44-check conformance suite, and the same Gemini agent bought from it with only the URL changed. docs/OPENNESS.md, `conformance/`.

**18. Is the order real? Valio's API has no order endpoint.**
Prices and availability are live from the challenge's mock API. Orders go to our own mock order service behind the same `BackendClient` interface a SAP adapter would implement, and the answer is a Peppol-style order response (AP/CA/RE), exportable as Peppol BIS T116 UBL XML validated against the UBL 2.3 XSD (not the full schematron). README "Openness", docs/peppol/.

**19. What is the path to production?**
Read-only first, then quotes, then orders with every order human-approved, then autonomy inside limits relaxed one rule at a time from audit evidence (allergen rules never relax for schools and hospitals). In parallel: SAP adapter for contract prices and real available-to-promise, GS1 Synkka as the allergen truth source, OAuth 2.1 and customer SSO, KMS keys, WORM audit, Peppol through Valio's access point. Valio legal must confirm whether a signed quote is a binding offer. README "Production next steps", docs/ARCHITECTURE.md §5.

## Gemini

**20. Why Gemini, and what does it cost?**
The catalog is mostly Finnish and Swedish; Gemini reads it directly, and gemini-embedding-2 puts "laktoositon maito" next to "lactose-free milk" in the same API. Thinking levels let us spend reasoning only on the substitution decision: routine steps run on gemini-3.8-flash LOW, and only the quote-decision turn escalates to gemini-4-hydrogen HIGH (6 of 31 steps in the montage). We measured that choice: hydrogen HIGH was 100% accurate on both parse and substitution tasks with p95 under 7.2 s, while flash HIGH came close (100% parse, 97.9% substitution) but hit a 69.5 s p95 on parsing. Cost: $0.0339 of Gemini per placed order, down 59% after a token trim, with 31% of input served from the implicit cache. docs/GEMINI.md, docs/evidence/.
