# Public URL plan (not deployed)

Nothing here is live. This is the plan for exposing the gateway after the demo, if wanted.

## Option A: Cloudflare Tunnel (quickest for a demo)

- Run the container or `make run` on a laptop or small VM. `cloudflared tunnel --url http://localhost:8080` gives a temporary https URL. A named tunnel gives a stable hostname on a Cloudflare zone.
- No inbound ports. The tunnel dials out.
- Put Cloudflare Access in front of the human UI routes (`/`, `/console.html`, `/inbox.html`, `/audit.html`, `/admin.html`) and admin routes (`POST /v1/approvals/*`, `PUT /v1/mandate`, `/v1/admin/*`), while leaving `/health`, `/.well-known/*`, `/openapi.json`, `/mcp`, and buyer `/v1/*` routes reachable for authenticated external agents.

## Option B: Fly.io (stable, one machine)

- `fly launch` with the `Dockerfile`, one shared-cpu machine, and a persistent volume mounted at `/app/var` for `AIMO_DB` (`/app/var/aimo.db`), `AIMO_KEY_DIR` (`/app/var/keys`), and upstream Valio cache files (`/app/var/cache`).
- Secrets with `fly secrets set VALIO_API_KEY=... VALIO_API_BASE=... AIMO_ADMIN_KEY=... AIMO_AGENT_KEYS='{"...":["customer","agent_id"]}'`. Never in the image.
- Product data (`data/`) goes on a read-only mount at `/app/data` (`AIMO_DATA_DIR=/app/data`), not in git or the image.

## What is exposed

| Path | Public | Auth |
|---|---|---|
| `GET /health` | yes | none (liveness check returning `{"ok": true}`) |
| `GET /.well-known/agent-card.json`, `GET /.well-known/jwks.json`, `GET /openapi.json` | yes | none (agent discovery, EdDSA public keys, OpenAPI 3.1 schema) |
| `GET /v1/products`, `GET /v1/products/{product_id}`, `POST /v1/needs/parse` | yes | none required (`POST /v1/needs/parse` accepts optional `X-Agent-Key` for audit attribution) |
| `POST /v1/offers`, `POST /v1/quotes`, `POST /v1/orders` | yes | `X-Agent-Key`, scoped to one `(customer, agent_id)` mandate |
| `GET /v1/orders`, `GET /v1/orders/{order_id}`, `GET /v1/approvals`, `GET /v1/audit`, `GET /v1/mandate` | yes | `X-Agent-Key` (scoped to own customer) or `X-Admin-Key` (all customers) |
| `/mcp` (streamable HTTP, 12 tools) | yes | per-tool auth matching `/v1/*` (`X-Agent-Key` for buyer tools, `X-Admin-Key` for `decide_approval`, none for catalog/parse) |
| `POST /v1/approvals/{order_id}`, `PUT /v1/mandate`, `POST /v1/admin/simulate`, `DELETE /v1/admin/simulate` | restrict | `X-Admin-Key` plus Cloudflare Access or network policy |
| `/`, `/console.html`, `/inbox.html`, `/audit.html`, `/admin.html` (and static `web/*` assets) | restrict | Cloudflare Access or network policy (human operator UI) |

## Before going public

- [ ] Replace all demo keys (`demo-hotel-aurora`, `demo-hospital-tays`, `demo-school-hervanta`, `demo-admin`) through `AIMO_AGENT_KEYS` and `AIMO_ADMIN_KEY`. The defaults are public in the repo and used as fallbacks in `web/`.
- [ ] Put Cloudflare Access or an authenticating reverse proxy in front of `/`, `/console.html`, `/inbox.html`, `/audit.html`, `/admin.html`, `POST /v1/approvals/*`, `PUT /v1/mandate`, and `/v1/admin/*`.
- [ ] Block `/v1/admin/simulate` (`POST` and `DELETE`) at the edge outside demo sessions so stock-out overrides cannot affect live offers or quotes.
- [ ] Serve https only so `X-Agent-Key` and `X-Admin-Key` headers never travel in cleartext.
- [ ] Rate-limit `/v1/*` and `/mcp` at the edge, and keep `AIMO_CACHE_DIR` (`var/cache`) persistent so `aimo/valio.py` continues batching (1,000 products per call) and caching shared Valio API responses.
- [ ] Persist `AIMO_DB` and `AIMO_KEY_DIR` on a volume so single-use quote state, the hash-chained audit ledger, and Ed25519 keys survive container restarts.
- [ ] Run `.venv/bin/python scripts/smoke_demo.py --url https://<public-host>` before opening traffic to verify `/health`, `/.well-known/*`, `/openapi.json`, all 5 UI views, `/mcp`, and signed checkpoint verification.

## Key rotation

- Quotes and audit checkpoints are signed EdDSA, key in `var/keys/quote-ed25519.json`, `kid` is the thumbprint, public keys at `/.well-known/jwks.json`.
- Rotate signing keys with `python -m aimo.keys rotate` (and restart): `aimo/keys.py` saves the previous public key as `retired-<kid>.json` in `AIMO_KEY_DIR` (`var/keys` by default) and serves both active and retired public keys in `/.well-known/jwks.json` so existing quotes and audit checkpoints verify seamlessly across rotation.
- Agent keys: rotate by changing `AIMO_AGENT_KEYS` and restarting.
