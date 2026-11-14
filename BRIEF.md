# Valio Aimo: Trusted Trade Agents (Junction 2026)
Page: https://hackjunction.app/participate/45/challenges/327. Deadline: Sun 15 Nov 2026 10:00 Helsinki.

**Question.** How can a foodservice wholesaler let autonomous purchasing agents find products, compare offers, get the right approval and place orders quickly, without exposing real systems or confidential data? Prototype the interface and operating model for an external agent talking to Valio Aimo's (mocked) webshop and backend, built on open, verifiable standards.

**Core requirements.**
1. Product, price and availability lookup, in structured form.
2. Inquiry, then an order at fixed prices.
3. Permissions and human approval: what the agent decides alone and what needs sign-off.

**Optional.**
- Delivery-time and sustainability comparison (certificates and labels; category-level CO2 is a stretch goal).
- Substitution and exception handling.
- Traceability and explainability (a human-readable log).

**Example request.** "I need 1,000 litres of lactose-free milk by Monday", sent by a hotel chain's agent.

**Insights they want.**
- A trust protocol.
- A guardrails model (spend thresholds, substitutions outside preferences).
- Good-better-best substitution ranking.
- An audit trail.

**Scenarios.**
- Menu, then recipe, then order.
- A last-minute event.
- A supplier stock-out with a recipe-compatible substitute.
- A proactive selling agent.
- Allergen-safe substitution (schools, hospitals).
- Detecting drift in a standing order.

**Judging.**
- Working end-to-end demo, request to lookup to order or approval: 30%.
- Solves the user need with minimal correction: 20%.
- Autonomy and controls, with auditability: 20%.
- Robustness to exceptions: 15%.
- Openness and a credible path to production: 15%.

**Prizes.** €4,000, €2,000 and €1,000.

**Resources.**
- Synthetic catalog, simulated suppliers and mock APIs. The portal https://junction26.valioaimo.fi/ has datasets, API docs and mock-backend access.
- The portal password and API key are in `.env`, which is gitignored. Never commit them.
- Preferred tech: agentic/tool-use frameworks, open interop standards (MCP, OpenAPI, verifiable credentials). x402 and Nevermined are relevant context but optional.

**Mentors.** Hanna Forsström, Pirjo Kaartinen (commercial), Minni Ruhtinas (online sales), Tommi Nurmi-Aro, Ville Takanen, Riku Tapper (IT and data governance).

**Prior work.** Momin (GitHub Momin010) has already been working on this. Coordinate with him and build on his work rather than duplicating it. The old Valio scaffold is in s1laswang/agent-arena git history, before commit a589d5e.
