"""Nordic Bakery Supply: a second (synthetic) wholesaler implementing the same Trusted Trade profile.

The openness proof: this is NOT a fork. It reuses aimo's gateway (HTTP + MCP), trust layer (policy engine,
Ed25519 signing, hash-chained audit, approvals) and catalog/planner unchanged, and swaps only:
  - the BackendClient (static JSON prices and stock instead of the Valio Aimo mock API),
  - the product data (30 synthetic bread and flour products, same product-JSON shape),
  - the customers' policies (toywholesaler/policies/*.yaml),
  - its own data dir, so it has its OWN Ed25519 key, state DB and audit chain.

    uv run uvicorn toywholesaler.app:app --port 8820
    uv run python -m conformance --base http://127.0.0.1:8820 --query "rye bread"

No network access and no API keys are needed (Gemini is optional, for semantic search only).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi.responses import JSONResponse
from pydantic import ValidationError

HERE = Path(__file__).resolve().parent
NAME = "Nordic Bakery Supply Trusted Trade Gateway"
SERVER_NAME = "nordic-bakery-supply-trusted-trade"

# Must be set before anything imports aimo.config: own data dir -> own key, state DB, catalog.
os.environ.setdefault("AIMO_DATA_DIR", str(HERE.parent / "data" / "toywholesaler"))

from aimo import config  # noqa: E402
from aimo.backend import set_client  # noqa: E402
from aimo.backend.catalog_store import load_catalog  # noqa: E402

from toywholesaler.backend import CATALOG, StaticBackendClient, product_json  # noqa: E402


def _prepare_data() -> None:
    body = json.dumps(product_json(), ensure_ascii=False, indent=1)
    p = config.PRODUCT_JSON
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = p.read_text(encoding="utf-8") if p.is_file() else None
    except (OSError, UnicodeDecodeError):
        current = None
    if current != body:
        p.write_text(body, encoding="utf-8")
    load_catalog()


def _configure() -> None:
    import aimo.gateway.mcp_server as mcp_mod
    import aimo.trust.policy as policy
    from aimo.gateway import service

    policy.POLICY_DIR = HERE / "policies"
    policy._load_policy_cached.cache_clear()  # noqa: SLF001
    set_client(StaticBackendClient())
    mcp_mod.SERVER_NAME = SERVER_NAME
    mcp_mod.INSTRUCTIONS = mcp_mod.INSTRUCTIONS.replace(
        "Valio Aimo Trusted Trade gateway (Finnish foodservice wholesaler, ~17,500 products)",
        f"{NAME} (synthetic Finnish bakery wholesaler, {len(CATALOG['products'])} products)")
    orig = service.manifest

    def manifest(base_url: str) -> dict:
        m = orig(base_url)
        m["name"] = NAME
        m["description"] = ("Synthetic second wholesaler (bread and flour) implementing the same Trusted Trade profile "
                            "(otap/0.1) by reusing the aimo gateway and trust modules with a static backend.")
        m["operator"] = CATALOG["vendor"]
        m["mcp"] = {**m["mcp"], "server_name": SERVER_NAME, "stdio": None}
        m["data_note"] = CATALOG["note"]
        m.pop("fault_injection", None)
        return m

    service.manifest = manifest


_prepare_data()
_configure()

from aimo.gateway.app import app  # noqa: E402  (built after the configuration above)

app.title = NAME
app.description = (f"**{NAME}** (synthetic demo wholesaler). Same Trusted Trade profile as the Valio Aimo gateway: "
                   "OpenAPI here, MCP at `/mcp`, discovery at `/.well-known/otap.json`, keys at `/.well-known/jwks.json`.")
app.openapi_schema = None


@app.exception_handler(ValueError)
async def _toy_value_error(_req, exc: ValueError):
    if isinstance(exc, ValidationError):
        from aimo.gateway.schemas import validation_error_body

        return JSONResponse(validation_error_body(exc.errors()), status_code=422)
    return JSONResponse({"error": "invalid_request", "message": str(exc) or "Invalid request."}, status_code=422)


@app.exception_handler(OverflowError)
async def _toy_overflow_error(_req, exc: OverflowError):
    return JSONResponse({"error": "invalid_request", "message": str(exc) or "Numeric value out of range."}, status_code=422)
