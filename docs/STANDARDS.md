# Open standards

We use existing standards so any agent or tool can plug in without our SDK.

| Standard | Where | Why |
|---|---|---|
| MCP (streamable HTTP and stdio) | `/mcp`, `python -m aimo.mcp_server` | Agent frameworks already speak it. All 12 MCP tools share the handlers and schemas of their `/v1` REST counterparts. |
| OpenAPI 3.1 | `/openapi.json` | Generated from the FastAPI routes and Pydantic models, so it cannot drift. |
| JSON Schema | `aimo/models.py` | Same Pydantic models validate REST input and describe MCP tools to agents. |
| JWS (Ed25519) and JWKS | `/.well-known/jwks.json` | A buyer or auditor can verify any quote or audit checkpoint offline with the public keys. |
| Agent card | `/.well-known/agent-card.json` | Agents discover endpoints, tools, auth headers, mandates, and verification steps without docs. |
| Hash-chained audit | `/v1/audit`, `python -m aimo.audit verify-checkpoint` | Editing or deleting a past event breaks the SHA-256 chain; the head checkpoint is EdDSA-signed. |

## Verify a quote yourself

Quotes are JWS compact tokens with `alg` pinned to `EdDSA` (Ed25519). The protected header `kid` is the RFC 7638 thumbprint of the signing key listed in `/.well-known/jwks.json`. Pass either the live JWKS URL or a saved `jwks.json` file for offline verification:

```sh
python -m aimo.quotes verify "$JWS" http://localhost:8080/.well-known/jwks.json
```

Captured output from a 1,000 L lactose-free milk quote (`Valio rasvaton maito 1 l laktoositon UHT`, 100 cases of 10 L at 13.32 EUR net = 1,332.00 EUR net, 1,512.00 EUR gross) against a booted server on port 8206:

```text
$ python -m aimo.quotes verify "$JWS" http://localhost:8206/.well-known/jwks.json
OK q_707cdbf56b7c customer=hotel-aurora agent=buyer-aurora lines=1 total_gross=1512.00 EUR delivery=2026-11-16 valid_until=2026-11-15T15:00:00+02:00
```

Format: `OK q_... customer=... agent=... lines=... total_gross=... delivery=... valid_until=...` and exit code 0. On failure it prints the error code (a tampered token prints `BAD_SIGNATURE: Quote signature is invalid.`) and exits 1. An expired quote appends `(EXPIRED)`.

Or from any Python environment with `joserfc` and `httpx`:

```python
import httpx
from joserfc import jws
from joserfc.jwk import KeySet

keys = KeySet.import_key_set(httpx.get("http://localhost:8080/.well-known/jwks.json").json())
obj = jws.deserialize_compact(quote["jws"], keys, algorithms=["EdDSA"])
print(obj.payload)
```

Always pin `algorithms=["EdDSA"]`; `joserfc` rejects `EdDSA` unless explicitly enabled (its RFC 9864 deprecation warning is harmless).

## Verify an audit checkpoint and rotate keys

`GET /v1/audit` recomputes the full SHA-256 chain (`verified: {"ok": true, "bad_seq": null}`) and returns a signed `checkpoint` (`{seq, hash, ts, jws}`) using the same Ed25519 keyset as quotes. Verify a checkpoint against the live JWKS URL or a local JWKS snapshot:

```text
$ python -m aimo.audit verify-checkpoint "$CHECKPOINT_JWS" http://localhost:8206/.well-known/jwks.json
OK seq=8 hash=428f7b82ecd50725bf8c62eabc33a6792c9bc246b5337b21544d24af736c32c9
```

Tampering with the checkpoint token prints `BAD_SIGNATURE` and exits 1.

`python -m aimo.keys rotate` archives the current public key to `retired-<kid>.json` in `AIMO_KEY_DIR` (`var/keys` by default) and atomically writes a new `quote-ed25519.json`. `/.well-known/jwks.json` serves the active public key plus all retired public keys, so quotes and checkpoints issued before rotation still verify and redeem after restarting the server on the rotated key directory:

```text
$ python -m aimo.keys rotate
new kid BAOMucKTviTydh55fu_MExPLDzNzAc9desUrpEWMwTA
$ python -m aimo.quotes verify "$PRE_ROTATION_JWS" http://localhost:8206/.well-known/jwks.json
OK q_3148b77d10ee customer=hotel-aurora agent=buyer-aurora lines=1 total_gross=1512.00 EUR delivery=2026-11-16 valid_until=2026-11-15T15:00:00+02:00
```

See [MCP client setup](../deploy/mcp-clients.md) for HTTP and stdio MCP connection configs and [Trust protocol](TRUST.md) for single-use redemption and red-team checks.
