# Open standards

We use existing standards so any agent or tool can plug in without our SDK.

| Standard | Where | Why |
|---|---|---|
| MCP (streamable HTTP and stdio) | `/mcp`, `python -m aimo.mcp_server` | Agent frameworks already speak it. Tool names equal REST routes. |
| OpenAPI 3.1 | `/openapi.json` | Generated from the code, so it cannot drift. Any HTTP client can be generated. |
| JSON Schema | pydantic models | Same models validate input and describe tools to agents. |
| JWS (Ed25519) and JWKS | `/.well-known/jwks.json` | A buyer or auditor can verify a quote offline with the public key. |
| Agent card | `/.well-known/agent-card.json` | Agents discover tools, auth and policies without docs. |
| Hash-chained audit | `/v1/audit` | Editing or deleting a past event breaks the chain. |

## Verify a quote yourself

Quotes are JWS compact, alg `EdDSA` (Ed25519). The `kid` is the RFC 7638 thumbprint of the key listed in `/.well-known/jwks.json`.

One command (needs this repo):

```
python -m aimo.quotes verify "$JWS" http://localhost:8080/.well-known/jwks.json
```

Real output from a quote on a booted server:

```
$ python -m aimo.quotes verify "$JWS" http://localhost:8123/.well-known/jwks.json
OK q_73e19c560ce1 customer=hotel-aurora agent=buyer-aurora lines=1 total_gross=1770.00 EUR delivery=2026-11-16 valid_until=2026-11-15T17:00:00+02:00
```

Format: `OK q_... customer=... total_gross=... valid_until=...` and exits 0. On failure it prints the error code (a tampered token gives `BAD_SIGNATURE: Quote signature is invalid.`) and exits 1. An expired quote adds `(EXPIRED)`.

Or from any Python with `joserfc` and `httpx`:

```python
import httpx
from joserfc import jws
from joserfc.jwk import KeySet

keys = KeySet.import_key_set(httpx.get("http://localhost:8080/.well-known/jwks.json").json())
obj = jws.deserialize_compact(quote["jws"], keys, algorithms=["EdDSA"])
print(obj.payload)
```

Pin the algorithm yourself. joserfc rejects EdDSA unless you pass `algorithms=["EdDSA"]`. A SecurityWarning about RFC 9864 is harmless.
