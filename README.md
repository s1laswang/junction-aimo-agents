# Valio Aimo: Trusted Trade Agents

An open, documented gateway that lets any company's AI purchasing agent **find, compare, quote and order** from Valio Aimo, inside guardrails the customer controls. Every decision is **signed and explained**. Built for Junction 2026 (Valio Aimo challenge, plus Google DeepMind Best Use of Gemini API).

- **Open interface:** an MCP server (`/mcp`) and an OpenAPI 3.1 REST API (`/docs`) over the real Valio Aimo mock price and availability API, plus a discovery manifest at `/.well-known/otap.json`.
- **Guardrails:** a YAML policy engine with spend thresholds, category scopes, substitution rules and allergen hard blocks. It returns auto-approve, needs-approval or reject, with a plain-language reason for each rule.
- **Good-better-best substitution:** allergen-safe. Missing allergen data counts as *unknown*, never safe.
- **Signed audit trail:** a hash-chained log of Ed25519/JWS-signed events with a human-readable report and chain verification.
- **Gemini buyer agent:** runs "1,000 L lactose-free milk by Monday" end to end, with a human-approval UI.

## Run it (one command)
```bash
cp .env.example .env   # fill AIMO_API_KEY, AIMO_PORTAL_PASSWORD, GEMINI_API_KEY
uv run aimo serve      # downloads data on first run, then open http://localhost:8000/ui
```
Terminal-only demo: `uv run aimo demo`.

Docs: [docs/PLAN.md](docs/PLAN.md) · [docs/TEAM.md](docs/TEAM.md) · [docs/interfaces.md](docs/interfaces.md)
