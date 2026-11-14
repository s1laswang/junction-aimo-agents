# Public URL plan (not deployed)

Nothing here is live. This is the plan for exposing the gateway after the demo, if wanted.

## Option A: Cloudflare Tunnel (quickest for a demo)

- Run the container or `make run` on a laptop or small VM. `cloudflared tunnel --url http://localhost:8080` gives a temporary https URL. A named tunnel gives a stable hostname on a Cloudflare zone.
- No inbound ports. The tunnel dials out.
- Put Cloudflare Access in front of `/v1/admin/*`, `PUT /v1/mandate` and `/` (the console), so only listed people reach the human side.

## Option B: Fly.io (stable, one machine)

- `fly launch` with the `Dockerfile`, one shared-cpu machine, a volume mounted at `/app/var` for `aimo.db` and the signing key.
- Secrets with `fly secrets set VALIO_API_KEY=... VALIO_API_BASE=... AIMO_ADMIN_KEY=... AIMO_AGENT_KEYS='{"...":["customer","agent"]}'`. Never in the image.
- Product data (`data/`) goes on the volume or a read-only mount, not in git or the image.

## What is exposed

| Path | Public | Auth |
|---|---|---|
| `/.well-known/agent-card.json`, `/.well-known/jwks.json`, `/openapi.json` | yes | none |
| `/v1/*` agent routes, `/mcp` | yes | `X-Agent-Key`, scoped to one customer |
| `decide_approval`, `PUT /v1/mandate`, `/v1/admin/*` | restrict | `X-Admin-Key` plus network or Access policy |
| `/` console | restrict | Access policy |

## Before going public

- Replace all demo keys (`demo-*`, `demo-admin`) through `AIMO_AGENT_KEYS` and `AIMO_ADMIN_KEY`. The defaults are public in the repo.
- Rate limit at the edge. The Valio mock is shared by every team, so the server caches and batches; keep it that way.
- Serve https only, so keys never travel in clear.

## Key rotation

- Quotes and audit checkpoints are signed EdDSA, key in `var/keys/quote-ed25519.json`, `kid` is the thumbprint, public keys at `/.well-known/jwks.json`.
- Rotate signing keys with `python -m aimo.keys rotate` (and restart): `aimo/keys.py` saves the previous public key as `retired-<kid>.json` in `AIMO_KEY_DIR` (`var/keys` by default) and serves both active and retired public keys in `/.well-known/jwks.json` so existing quotes and audit checkpoints verify seamlessly across rotation.
- Agent keys: rotate by changing `AIMO_AGENT_KEYS` and restarting.
