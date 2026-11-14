# TEAM: roles, owners, interfaces, milestones

Shared contracts: `aimo/models.py` (CEO) and `docs/interfaces.md`. Each Claude engineer works on its own branch `team/<role>` in its own git worktree. It commits small, pushes often, and never force-pushes. The CEO merges to `main`. You may edit files outside your area only through the CEO.

## Claude engineering team
| Role | Owner | Owns (paths) | Delivers |
|---|---|---|---|
| CEO | Claude (aimo-ceo) | `docs/PLAN.md`, `docs/TEAM.md`, `docs/interfaces.md`, `aimo/models.py`, `aimo/config.py`, merges | Plan, contracts, integration, submission |
| Gateway | aimo-gateway | `aimo/backend/client.py`, `aimo/backend/orders.py`, `aimo/gateway/`, `scripts/fetch_data.py`, `aimo/cli.py` | Live mock API client (cache, retry, rate-limit, fault injection), FastAPI `/v1/*` + OpenAPI, MCP server (`/mcp` + stdio), `/.well-known/otap.json`, mock order backend, `uv run aimo serve` |
| Catalog | aimo-catalog | `aimo/catalog/`, `aimo/backend/catalog_store.py` | Product normalizer (names, units → litres or kg per sales unit, allergens tri-state from `allergen`/`nonAllergen`/`nutritionalClaim`, labels), SQLite FTS search, reliability and CO2 join, `plan_need()` split planner, good-better-best `rank_substitutes()`, Gemini embeddings |
| Trust | aimo-trust | `aimo/trust/` | YAML policy engine (spend thresholds, category scopes, substitution rules, allergen hard blocks, cut-off and freshness checks), Ed25519 key, JWS-signed quotes, approvals bound to quote hash, hash-chained signed audit log, human-readable audit report, chain verification, injection screening helper |
| Buyer | aimo-buyer | `aimo/buyer/` | Gemini buyer agent (google-genai function calling over the MCP tools), need parsing with structured output, streaming of steps to the UI, `uv run aimo demo` running the 1,000 L milk example end to end, Gemini usage ledger |
| Frontend | aimo-frontend | `web/` | No-build approval UI at `/ui`: request box, live agent trace, good-better-best cards, policy verdict, approval inbox (approve/reject), order response, audit timeline plus "Verify chain" |
| Wind tunnel + QA | aimo-windtunnel | `windtunnel/`, `tests/` | Gemini-generated synthetic customers and requests (schools, hospitals, hotels, events, stock-outs, injections), runner against the gateway, scoring (escalation correctness, violations, latency), FR8 jobs, pytest smoke tests |
| Pitch | aimo-pitch (spawned Sat evening) | `docs/pitch/`, `README.md` polish, `docs/SUBMISSION.md`, `docs/GEMINI.md` | Deck outline, demo script rehearsal, submission text, DeepMind write-up |

## Interfaces (summary; full detail in docs/interfaces.md)
- Catalog → Gateway: `search(query, filters) -> list[ProductSummary]`, `get_product(id) -> Product`, `plan_need(need, offers_fn) -> Plan`.
- Backend → everyone: `BackendClient.price(ids, date)`, `.availability(ids, date)` → `dict[id, Offer]` via `get_offers()`.
- Trust → Gateway: `evaluate(quote, mandate) -> PolicyDecision`, `sign_quote(quote) -> Quote`, `verify_quote(quote)`, `audit.append(event)`, `audit.verify()`, `audit.render(subject_id)`.
- Gateway → Buyer and Frontend: HTTP `/v1/*` and MCP tools (names fixed in interfaces.md).
- Buyer → Frontend: `POST /v1/agent/runs` starts a run. `GET /v1/agent/runs/{id}/events` (SSE) streams `AgentStep` events.

## Milestones
- **M1 (Sat 18:00):** terminal slice end to end. Gateway, catalog, trust and buyer must each have their function stubs real by then.
- **M2 (Sat 22:00):** browser demo. Frontend plus MCP.
- **M3 (Sun 02:00):** robustness plus wind tunnel numbers.
- **Freeze (Sun 06:00):** main submission-ready.

## Human teammates
| Who | Tasks | By |
|---|---|---|
| **Silas** (s1laswang) | Answer the "Questions for Silas" in PLAN.md, starting with where Momin's code is. Book 15 minutes with the Valio mentors (Minni Ruhtinas for online sales, Riku Tapper or Ville Takanen for IT and data governance) to sanity-check the guardrails model and the "mock order backend" assumption. Rehearse and present. Click submit. | Mentors by Sat 19:00, submit Sun 09:30 |
| **Momin** (Momin010) | Push your existing Aimo work to a `momin/*` branch of this repo, or send the link. Then pair with the CEO for 30 minutes to map it onto our seams (`BackendClient`, `catalog.search`, gateway/MCP). Own the merge of your pieces. Review the MCP server against the spec. | Link ASAP, merge by Sat 22:00 |
| **Ivar** (ivarakerholm-wq) | Domain and UX owner. Write 3 customer profiles with real-sounding policies (hotel chain, school kitchen with milk-allergy pupils, hospital with gluten-free and texture-modified diets) in `aimo/trust/policies/` style YAML or plain text. Click through the approval UI at M2 and file issues. Validate the good-better-best wording with a mentor. | Profiles by Sat 20:00, UI review at 22:30 |
| **Chintufp** (Chintufp) | Pitch and submission owner. Build the deck from the PLAN demo script, record the backup demo video at the Sun 06:00 freeze, and draft the Junction submission text and the Google DeepMind "Best Use of Gemini" text from `docs/GEMINI.md`. Drive the live demo. | Deck draft Sat 23:00, video Sun 07:30 |
