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

```python
import httpx
from joserfc import jws
from joserfc.jwk import KeySet

base = "http://localhost:8080"
keys = KeySet.import_key_set(httpx.get(f"{base}/.well-known/jwks.json").json())
quote = {"jws": "<quote.jws from request_quote>"}
obj = jws.deserialize_compact(quote["jws"], keys)
print(obj.payload)
```

TODO-VERIFY: run this snippet against real dev and fix imports and key set loading.
