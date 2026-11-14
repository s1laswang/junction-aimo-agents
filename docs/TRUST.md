# Trust protocol (one page)

Owner: trust. Code: `aimo/trust/`. Run the walkthrough with `uv run python -m aimo.trust.demo`.

Purpose: a company can let its AI agent buy from Valio Aimo because (1) the agent can prove what it was offered, (2) the customer's own rules decide what happens automatically, (3) a named human approves the exact quote when the rules require it, and (4) anyone can check afterwards what happened and why.

## 1. What an agent must verify before relying on a quote
Every `Quote` from `POST /v1/quotes` carries `quote_hash`, `signature` and `kid`.
1. **Hash binding.** Recompute `sha256(canonical_json(quote minus quote_hash, signature, kid))` and compare it with `quote_hash`. `canonical_json` means sorted keys, no whitespace, UTF-8, and integral numbers written without `.0`, close to RFC 8785 (JCS).
2. **Signature.** `signature` is a compact JWS (RFC 7515) with `alg: EdDSA` (Ed25519, RFC 8037). Fetch `/.well-known/jwks.json`, pick the key whose `kid` matches (the kid is the RFC 7638 JWK thumbprint), and verify. The JWS payload is `{iss, typ:"quote", quote_id, quote_hash, customer_id, agent_id, total_net, currency, iat, exp}`, and its `quote_hash` must equal your recomputed hash. Refuse `alg: none` and unknown kids.
3. **Expiry.** `expires_at` is the price lock (15 minutes by default). After it passes, ask for a new quote.
4. **Ordering.** `POST /v1/orders {quote_id, quote_hash}`. The gateway re-checks steps 1 to 3 and evaluates the policy. Approvals bind to the same hash.

Python: `verify_quote(q)` checks the hash and the signature. `verify_quote_detailed(q)` adds `expired` and `reasons`.

## 2. Guardrails model
The mandate (`Mandate`: customer, agent, scopes, policy_id) states *who* may act. The policy (versioned YAML in `aimo/trust/policies/<customer>.yaml`) states *what* the agent may do on its own. `evaluate(quote, mandate)` runs every rule on every quote. Each rule returns a `RuleHit` with a plain-language message, including the rules that pass, so the UI and the audit trail show every check that ran. **The final outcome is the strictest hit: reject > needs_approval > auto_approve.** Line-level hits use `rule_id = "<rule>:<product_id>"`, so an agent can drop or replace just that line. Each rule_id appears exactly once, with its final outcome. Defense in depth: the gateway computes tier and flags when it issues the quote, and the policy engine re-derives them as a second lock.

| Rule type | What it checks | Typical outcome |
|---|---|---|
| `quote_integrity` (always on) | Lines add up, quantities are positive, and the signature verifies if one is present | reject |
| `scope` | Mandate customer and agent match the quote, the mandate has not expired, and it holds `order:submit`. Unknown agents get no scopes. | reject |
| `quote_freshness` | Price lock still valid | reject |
| `spend_threshold` | Auto ≤ X; needs approval up to Y; above Y reject | needs_approval / reject |
| `period_spend` | Rolling 7-day spend from `order.placed` audit events | needs_approval |
| `category_scope` | Product category code within the allowed prefixes; unknown category → configurable | needs_approval |
| `substitution` | **The tier is verified server-side; the agent's own label is never trusted.** A line is `preferred` only if its product is in the policy's `preferred_products`. Tiers best, better and good come from the customer-approved `approved_alternatives`, or for best from catalog evidence (same brand, category and unit as a usual product, keeping all its claims). Anything else, including a missing tier, gets "Agent-supplied tier could not be verified; treating as substitution" → `unverified` outcome. A claimed tier worse than the verified one is honoured. Also checks the `outside_preference` flag and that required claims are kept. | allow / needs_approval / reject |
| `allergen_block` | Excluded allergens from the policy plus those in the Need. contains or may_contain → reject. Unknown → reject or needs_approval per policy. **`inferred:*` (Gemini-extracted) counts as unknown.** | reject / needs_approval |
| `delivery` | Delivery not in the past and not after `need.delivery_by`; no line flagged `cutoff_passed`, `unavailable` or `sales_block` | reject |
| `quantity_drift` | Volume against the customer's standing-order baseline ("This is 3.3× your usual weekly volume of milk") | needs_approval |
| `injection` | Supplier text for ordered products screened (regex layer), `injection*` line flags, or `need.preferences.injection_flags` | needs_approval |

Fail-closed design: if the catalog is missing a product, its allergens, claims and category count as unknown, and unknown is never treated as safe. A rule that crashes yields needs_approval, never an approval. Policies are validated when they load (unknown rule types and bad outcomes are refused).

**Injection screening** (`screen_text`): layer 1 is regex heuristics for instruction overrides, limit bypasses, system-prompt probing, agent-directed commands, large-quantity commands, price and approval manipulation, tool-call injection, and hidden markup. URLs, "secret" and zero-width characters are weak signals that only count together with a strong one. Measured on the real data: **0 of 34,159 supplier marketing texts flagged**, while the injected samples are caught. Layer 2 is an optional Gemini classifier (`GEMINI_FAST_MODEL`) with JSON-schema output, enabled with `AIMO_SCREEN_GEMINI=1` or `use_gemini=True`. If it fails, layer 1 still applies. Screening never makes untrusted text trusted: the buyer still wraps it in delimiters and never lets it change quantities or limits.

## 3. Approval matrix (demo customers)
| Customer | Auto-approve | Needs approval (role) | Reject |
|---|---|---|---|
| `hotel-aurora` | ≤ €500, usual product 463038 or a verified best-tier substitute (e.g. the pre-approved 464306), within dairy/bakery/chilled categories, ≤ 3× baseline | €500–5,000; better or good tier; outside preference; unknown category; > 3× baseline (300 L milk/week); 7-day spend > €8,000; injection → `procurement_manager` (override: `general_manager`) | > €5,000; missing required claim; expired; cut-off passed; no `order:submit` |
| `school-tampere` | ≤ €300, usual product (472833, a demo assumption) confirmed free of milk and nuts | €300–3,000; **any** substitution; > 3× baseline → `kitchen_manager` (override: `catering_director`) | contains or may contain milk or nuts; **allergen unknown**; > €3,000 |
| `hospital-meilahti` | ≤ €1,000, usual products confirmed gluten-free | any substitution, **gluten status unknown or only AI-inferred** → `dietitian`; €1,000–10,000 → `procurement_manager` (override: `head_of_nutrition`) | contains or may contain gluten; > €10,000 |

When several roles are needed, the decision names the first one in the policy's `approver_roles` order: a clinical sign-off outranks a budget sign-off. **`check_approval(quote, decision, approver, approver_role, quote_hash)`** accepts an approval only if all of these hold:
- The approver is a person in the customer's approver directory (`approvers` in the YAML). Unknown identities are refused.
- **The approver is never an AI agent.** Any `agent:` actor, the quote's own agent, and any agent id registered in any policy (bare or `customer/agent`) are refused.
- The approver's *directory* role is the role the decision names or an override role. A role claimed in the request never overrides the directory.
- The decision was `needs_approval`, so a hard reject cannot be overridden.
- `quote_hash` equals the signed quote's hash, and the signature verifies.
- The quote has not expired.

Every attempt, accepted or refused (`approval.refused`), is audited.

## 4. Audit format
Table `audit_events` in `data/state.sqlite`. SQLite triggers refuse UPDATE and DELETE, so the table is append-only. Each event has these fields: `seq, ts, actor ("agent:hotel-aurora/buyer-1" | "human:anna@…" | "system:gateway"), action (request.received, quote.created, policy.evaluated, order.pending_approval, approval.verified, approval.refused, order.placed…), subject_id, summary (one human sentence), data, prev_hash, hash, signature, kid`.
- `hash = sha256(prev_hash + canonical_json(event minus hash, signature, kid))`. The genesis `prev_hash` is 64 zeros.
- `signature` is a compact JWS (EdDSA) over `{typ:"audit-event", seq, hash}`.
- `data.related_ids` links subjects (run ↔ quote ↔ order). `events(order_id)` returns the whole connected story.
- `verify()` recomputes every hash, link and signature and returns `{ok, checked, broken_at, reason}`. `render(subject_id)` produces Markdown that reads like a story, followed by the rule table, approver, quote hash and "Chain verified: N events, signatures valid (key kid …)". `evidence_pack(subject_id)` contains the events, the signed quote, the decision, the chain status, the JWKS and verification steps, so a third party can check them offline.
- Demo: `tamper_for_demo(seq)` rewrites one event behind the log's back, and verify() then reports `broken_at`. `untamper_for_demo(seq)` restores it. Both are disabled when `AIMO_ENV=production`.

## 5. Production path (what we would change, and why it is already shaped for it)
- **Identity and authorization:** use OAuth 2.1 bearer tokens per the MCP Authorization spec (2025-06-18; protected resource metadata, RFC 9728), with our scopes `catalog:read quote:create order:submit`. Fine-grained limits travel as OAuth Rich Authorization Requests (RFC 9396) `authorization_details`.
- **Mandates as credentials:** the `Mandate` plus policy summary become a **W3C Verifiable Credential (VC Data Model 2.0)** issued by the customer to its agent, in the style of the open/closed mandates of the Agent Payments Protocol (AP2). The gateway verifies the VC instead of a YAML lookup. Agent identity can be a DID, or a key bound to the OAuth client.
- **AP2-style binding, not AP2 conformance.** Our flow mirrors AP2's pattern: the customer policy is like an open mandate, the human approval of the exact `quote_hash` is like a closed checkout mandate, and the signed quote is like the merchant-signed checkout. We do **not** implement AP2. We have no AP2 mandate objects and no payment mandate (Aimo invoices B2B customers). AP2 v0.2 also requires the merchant's checkout JWT to use a non-deterministic signature such as ECDSA, not Ed25519 ([spec](https://ap2-protocol.org/ap2/specification/)), and we sign quotes with Ed25519. A real AP2 bridge would add an ES256 key (a new `kid` in the JWKS) for AP2 checkout objects and keep Ed25519 for `otap/0.1` quotes and audit. Say "AP2-style" or "aligned with AP2", never "AP2-compliant".
- **Keys:** the Ed25519 private key moves to a KMS or HSM (e.g. Cloud KMS EC_SIGN_ED25519). The demo key in `data/keys/` (gitignored, generated on first use) is for the demo only. Rotation means publishing the new kid in the JWKS while keeping old kids for verification.
- **Audit store:** the same events go to a WORM store (object storage with retention lock) or a transparency log, and the chain head (`verify()["head"]`) is anchored periodically, e.g. by an RFC 3161 timestamp.
- **Orders:** the order response uses **Peppol BIS Order / Order Response** semantics (AP, CA, RE), so the flow can go onto the Peppol network unchanged. GS1 GTINs identify products.
- **Policies:** the YAML maps one to one onto a policy-as-code engine (OPA/Rego or Cedar) if a customer wants to bring their own. Versions and `policy_version` are recorded in every decision.

Standards cited: RFC 7515 (JWS), RFC 7517 (JWK), RFC 7638 (JWK thumbprint), RFC 8037 (EdDSA in JOSE), RFC 8785 (JCS), RFC 9396 (RAR), RFC 9728, MCP Authorization spec, OAuth 2.1 draft, W3C VC Data Model 2.0, AP2 v0.2 (pattern only, not implemented), Peppol BIS Billing/Ordering 3, GS1 GTIN, EU FIC 1169/2011 Annex II (the EU14 allergens).

## Human-only approvals (Sat 23:00)
- `POST /v1/approvals/{id}/decision` requires the human approver credential `X-Approver-Pin` (env `AIMO_APPROVER_PIN`, or generated once into `data/keys/approver_pin`). No API route or MCP tool returns it. Production: the customer's SSO (OIDC) for humans, OAuth client credentials for agents.
- Any decision request carrying agent identity (`X-Agent-Id`) gets **403 agent_cannot_approve**, even if it names a real approver. Missing or wrong credential gets **401**. Both are audited as `approval.refused`.
- On top of that, `check_approval` still requires a human in the order customer's directory with an allowed role, bound to the quote hash.
- Quotes and orders require explicit `X-Customer-Id` and `X-Agent-Id` (401 otherwise, no default identity). `GET /v1/approvals` is scoped to the caller's customer unless the approver credential is present. The demo tamper routes need the approver credential and are disabled when `AIMO_ENV=production`.
- Regression tests: `tests/test_human_auth.py`, scenario s0 check "an agent naming a real human approver still cannot approve (403)", conformance A2, A3 and A6.
- Brute force is capped: 5 wrong approver credentials within 10 min lock every credentialed route for 5 min (HTTP 429 approver_locked, Retry-After). Only the first refusals and the lock itself are audited, so guessing cannot flood the log. A generated credential is a random 12-character passphrase.
- `GET /v1/customers` shows approver ids and emails only to a signed-in approver; agents see names and roles.
- MCP: create_quote and place_order need identity (X-Customer-Id / X-Agent-Id on the MCP HTTP connection, or explicit AIMO_CUSTOMER_ID / AIMO_AGENT_ID for stdio). No default identity.
- **No lockout DoS:** the lock is per client address, not global. A human signs in once at `POST /v1/auth/approver-session` (PIN in, signed 8-hour `X-Approver-Session` token out; agents refused) and that session keeps working while any other client is locked. The UI stores only the session token, never the PIN.
