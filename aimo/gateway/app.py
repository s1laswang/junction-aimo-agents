"""FastAPI gateway: the open door for any company's purchasing agent. Owner: gateway.

Run: `uv run aimo serve` (or `uvicorn aimo.gateway.app:app`). Docs at /docs, OpenAPI at /openapi.json,
MCP (streamable HTTP) at /mcp, discovery manifest at /.well-known/otap.json.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from aimo import config
from aimo.backend.client import reset_request_faults, set_request_faults
from aimo.backend import clock
from aimo.gateway import service
from aimo.gateway.schemas import DecisionRequest, OffersRequest, OrderRequest, QuoteRequest
from aimo.models import Need, Offer, Order, Plan, Product, ProductSummary, Quote

log = logging.getLogger("aimo.gateway")

DESCRIPTION = """
**Valio Aimo Trusted Trade Gateway**: an open interface that lets any company's AI purchasing agent
find, compare, quote and order from Valio Aimo, inside guardrails the customer controls, with every
decision signed and explained.

* **Open**: OpenAPI 3.1 (this page) and the same tools over **MCP** at `/mcp` (server `valio-aimo-trusted-trade`).
  Discovery manifest at [`/.well-known/otap.json`](/.well-known/otap.json), signing keys at
  [`/.well-known/jwks.json`](/.well-known/jwks.json).
* **Live**: offers come from the Valio Aimo price and availability API (cached ~5 min, retried with backoff).
* **Trusted**: quotes are hashed and signed (JWS/EdDSA) with a price lock; orders are checked against the
  customer's policy (auto-approve, needs human approval, or reject); approvals are bound to the quote hash;
  every step lands in a hash-chained, signed audit log.
* **Order responses** follow Peppol BIS Order Response semantics: AP accepted, CA accepted with changes, RE rejected.

Identity (demo): headers `X-Agent-Id` and `X-Customer-Id`. Production: OAuth 2.1 bearer tokens with scopes.
Robustness demos: header `X-Aimo-Faults`, e.g. `stockout:463038;price_shock:464306:1.4;error_rate:0.2;latency_ms:800`.
"""

TAGS = [
    {"name": "discovery", "description": "Manifest and public keys for agents and verifiers."},
    {"name": "catalog", "description": "Search the ~17,500-product Valio Aimo catalog."},
    {"name": "offers", "description": "Live price and availability for a delivery date."},
    {"name": "plan", "description": "Need -> good/better/best candidates and a split plan."},
    {"name": "quotes", "description": "Signed, time-limited quotes (price lock + availability snapshot)."},
    {"name": "orders", "description": "Order placement against a signed quote; policy-gated."},
    {"name": "approvals", "description": "Human approval inbox. Decisions are bound to the quote hash."},
    {"name": "audit", "description": "Hash-chained, signed audit trail and verification."},
    {"name": "customers", "description": "Demo customers, mandates and policy summaries."},
    {"name": "demo", "description": "Demo-only helpers (disabled with AIMO_ENV=production): tamper with one audit event "
                                    "to show that /v1/audit/verify catches it, then restore it."},
]

ERR = {
    400: {"description": "Bad request"}, 403: {"description": "Identity or mandate problem"},
    404: {"description": "Not found"},
    409: {"description": "Conflict: unavailable lines, quote hash mismatch, expired quote (re-quote needed)"},
    503: {"description": "Upstream Valio Aimo API unavailable (retry after detail.retry_after seconds)"},
}


def identity(
    x_agent_id: Optional[str] = Header(None, description="Calling agent id, e.g. buyer-1"),
    x_customer_id: Optional[str] = Header(None, description="Customer the agent acts for, e.g. hotel-aurora"),
) -> service.Identity:
    ident = service.Identity(agent_id=x_agent_id or service.DEFAULT_AGENT,
                             customer_id=x_customer_id or service.DEFAULT_CUSTOMER)
    ident.authenticated = {"customer": bool(x_customer_id), "agent": bool(x_agent_id)}  # type: ignore[attr-defined]
    return ident


def _with_body_claims(ident: service.Identity, customer_id: Optional[str], agent_id: Optional[str]) -> service.Identity:
    """Headers win: a body customer_id/agent_id that disagrees with a header is 403. Without the header
    (demo convenience) the body value is used."""
    auth = getattr(ident, "authenticated", {"customer": True, "agent": True})
    cust = ident.customer_id if auth["customer"] else (customer_id or ident.customer_id)
    agent = ident.agent_id if auth["agent"] else (agent_id or ident.agent_id)
    return service.claim_identity(service.Identity(agent_id=agent, customer_id=cust), customer_id, agent_id)


class FaultsMiddleware:
    """Pure ASGI middleware: headers X-Aimo-Faults and X-Aimo-Now (demo mode) -> per-request context vars."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        spec = now_hdr = None
        for k, v in scope.get("headers", []):
            if k == b"x-aimo-faults":
                spec = v.decode("latin-1")
            elif k == b"x-aimo-now":
                now_hdr = v.decode("latin-1")
        token = set_request_faults(spec)
        ntoken = clock.set_request_now(now_hdr)
        try:
            await self.app(scope, receive, send)
        finally:
            clock.reset_request_now(ntoken)
            reset_request_faults(token)


def _replay_info() -> dict[str, Any]:
    from aimo.backend import replay

    return replay.info()


def _breakers() -> dict[str, Any]:
    from aimo.gateway import resilience

    return resilience.states()


def _upstream_state() -> dict[str, Any]:
    import time as _t

    from aimo.backend.client import get_client

    c = get_client()
    down = getattr(c, "down_until", 0.0) or 0.0
    return {"client": type(c).__name__, "circuit_open": down > _t.time(),
            "retry_in_s": max(0, round(down - _t.time())), "last_error": getattr(c, "last_error", None)}


def _prov_headers(meta: dict[str, Any]) -> dict[str, str]:
    h = {"X-Aimo-Source": str(meta.get("source")), "X-Aimo-Served-From": str(meta.get("served_from")),
         "X-Aimo-Fetched-At": str(meta.get("fetched_at")), "X-Aimo-Valid-For": str(meta.get("valid_for_s"))}
    if meta.get("faults"):
        h["X-Aimo-Faults-Applied"] = str(meta["faults"])
    return h


def build_router() -> APIRouter:
    r = APIRouter()

    # ---------------------------------------------------------- discovery
    @r.get("/.well-known/otap.json", tags=["discovery"], summary="Discovery manifest")
    def otap(request: Request) -> dict[str, Any]:
        """Machine-readable description of this gateway: endpoints, MCP URL, JWKS URL, standards and policies."""
        return service.manifest(str(request.base_url))

    @r.get("/.well-known/jwks.json", tags=["discovery"], summary="Public signing keys (JWKS, Ed25519 OKP)")
    def jwks() -> dict[str, Any]:
        return service.jwks()

    @r.get("/healthz", tags=["discovery"], summary="Health and integration status")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": service.VERSION, "integrations": service.integration_status(),
                "offline": config.OFFLINE, "clock": clock.describe(), "replay": _replay_info(),
                "upstream": _upstream_state(), "breakers": _breakers()}

    # ---------------------------------------------------------- catalog
    @r.get("/v1/products/search", tags=["catalog"], summary="Search products", response_model=list[ProductSummary])
    def search_products(
        q: str = Query(..., description="Free text (English or Finnish)", examples=["lactose free milk"]),
        claims: Optional[str] = Query(None, description="Comma-separated required claims", examples=["FREE_FROM_LACTOSE"]),
        category: Optional[str] = Query(None, description="Category code, e.g. 10400"),
        content_unit: Optional[str] = Query(None, description="L | KG | PCS"),
        limit: int = Query(20, ge=1, le=100),
    ):
        cl = [c.strip() for c in claims.split(",") if c.strip()] if claims else []
        res = service.find_products(q, cl, category, content_unit, limit)
        # dump each item as its own type: injected-text faults return Product (with marketing_text)
        return JSONResponse([p.model_dump(mode="json") for p in res])

    @r.get("/v1/products/{product_id}", tags=["catalog"], summary="Product details", response_model=Product, responses=ERR)
    def get_product(product_id: str):
        """Supplier fields such as `marketing_text` are UNTRUSTED: screen before showing them to an LLM."""
        return service.get_product(product_id)

    # ---------------------------------------------------------- offers / plan
    @r.post("/v1/offers", tags=["offers"], summary="Live price + availability", response_model=list[Offer], responses=ERR)
    def offers(body: OffersRequest, ident: service.Identity = Depends(identity)):
        """Merged `/api/price` + `/api/availability` from the Valio Aimo API. `cutoff_passed` is true when the
        product's order closing time is already past (it cannot make the requested date). Provenance is in
        each offer's `provenance` and in the `X-Aimo-*` response headers."""
        who = _with_body_claims(ident, body.customer_id, None)
        res, meta = service.fetch_offers(body.product_ids, body.requested_date, who.customer_id)
        return JSONResponse([o.model_dump(mode="json") for o in res], headers=_prov_headers(meta))

    @r.post("/v1/plan", tags=["plan"], summary="Plan a need (good/better/best + split)", response_model=Plan, responses=ERR)
    def plan(need: Need):
        return service.plan_need(need)

    # ---------------------------------------------------------- quotes
    @r.post("/v1/quotes", tags=["quotes"], summary="Create a signed quote", response_model=Quote, responses=ERR)
    def create_quote(body: QuoteRequest, ident: service.Identity = Depends(identity)):
        """Re-fetches live offers, refuses lines that are unavailable / past cut-off / short of stock (409 with
        per-line reasons), computes totals, signs the quote (quote_hash + JWS) and fixes the price until
        `expires_at`."""
        who = _with_body_claims(ident, body.customer_id, body.agent_id)
        return service.make_quote(who, body.delivery_date, [l.model_dump() for l in body.lines], body.need)

    @r.get("/v1/quotes/{quote_id}", tags=["quotes"], summary="Get a quote", response_model=Quote, responses=ERR)
    def get_quote(quote_id: str):
        return service.get_quote(quote_id)

    # ---------------------------------------------------------- orders
    @r.post("/v1/orders", tags=["orders"], summary="Place an order for a signed quote", response_model=Order, responses=ERR)
    def place_order(body: OrderRequest, ident: service.Identity = Depends(identity)):
        """Verifies quote hash, signature and expiry, then evaluates the customer's policy:
        `placed` (auto-approved, with Peppol-style response), `pending_approval` or `rejected`. Idempotent per quote."""
        return service.submit_order(ident, body.quote_id, body.quote_hash)

    @r.get("/v1/orders/{order_id}", tags=["orders"], summary="Order status", response_model=Order, responses=ERR)
    def get_order(order_id: str):
        return service.get_order(order_id)

    # ---------------------------------------------------------- approvals
    @r.get("/v1/approvals", tags=["approvals"], summary="Approval inbox")
    def approvals(
        status: Optional[str] = Query("pending", description="pending | placed | rejected | expired | all"),
        customer_id: Optional[str] = Query(None),
    ) -> list[dict[str, Any]]:
        """Orders with their exact quote and policy decision: `[{order, quote, decision, expired, approvals}]`."""
        return service.list_approvals(status, customer_id)

    @r.post("/v1/approvals/{order_id}/decision", tags=["approvals"], summary="Approve or reject", response_model=Order, responses=ERR)
    def decide(order_id: str, body: DecisionRequest):
        """Human decision. `quote_hash` must match the order's quote (409 otherwise); an expired quote returns 409
        "re-quote needed". Approve places the order and returns the order response."""
        return service.decide(order_id, body.decision, body.approver, body.quote_hash, body.note, body.approver_role,
                              remember=body.remember, remember_scope=body.remember_scope)

    # ---------------------------------------------------------- audit
    @r.get("/v1/audit", tags=["audit"], summary="Audit events")
    def audit(subject_id: Optional[str] = Query(None, description="quote_id or order_id")) -> list[dict[str, Any]]:
        return service.audit_events(subject_id)

    @r.get("/v1/audit/verify", tags=["audit"], summary="Verify the audit hash chain and signatures")
    def audit_verify() -> dict[str, Any]:
        return service.audit_verify()

    @r.get("/v1/audit/report/{subject_id}", tags=["audit"], summary="Human-readable audit trail (markdown)",
           response_class=PlainTextResponse)
    def audit_report(subject_id: str):
        return PlainTextResponse(service.audit_report(subject_id), media_type="text/markdown")

    # ---------------------------------------------------------- demo-only
    @r.post("/v1/demo/tamper/{seq}", tags=["demo"], summary="DEMO ONLY: tamper with one audit event", responses=ERR)
    def demo_tamper(seq: int, summary: Optional[str] = Query(None, description="New summary text (default: flips "
                                                                                 "'approved' to 'rejected')")):
        """Rewrites audit event `seq` in place, bypassing the append-only guard, so `GET /v1/audit/verify` shows the
        broken chain. Restore with `POST /v1/demo/untamper/{seq}`. 404 when AIMO_ENV=production."""
        return service.demo_tamper(seq, summary)

    @r.post("/v1/demo/untamper/{seq}", tags=["demo"], summary="DEMO ONLY: restore a tampered audit event", responses=ERR)
    def demo_untamper(seq: int):
        return service.demo_untamper(seq)

    # ---------------------------------------------------------- customers
    @r.get("/v1/customers/{customer_id}/learned", tags=["customers"], summary="Learned approvals (remembered swaps)")
    def learned(customer_id: str, x_customer_id: Optional[str] = Header(None)) -> dict[str, Any]:
        """Swaps a human approved with `remember: true`. Visible only to the customer itself (403 otherwise)."""
        if x_customer_id and x_customer_id != customer_id:
            raise service.ServiceError(403, "customer_mismatch", "you can only read your own customer's learned approvals")
        return service.learned_for(customer_id)

    @r.get("/v1/customers", tags=["customers"], summary="Demo customers and policy summaries")
    def customers() -> list[dict[str, Any]]:
        return service.list_customers()

    return r


def create_app(with_mcp: bool = True) -> FastAPI:
    mcp_server = None
    if with_mcp:
        try:
            from aimo.gateway.mcp_server import build_mcp

            mcp_server = build_mcp()
        except Exception:  # never let MCP break the HTTP API
            log.exception("MCP server unavailable")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_server is not None:
            async with mcp_server.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(
        title="Valio Aimo Trusted Trade Gateway",
        version=service.VERSION,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        contact={"name": "Trusted Trade Agents (Junction 2026)"},
        license_info={"name": "MIT"},
    )
    app.add_middleware(FaultsMiddleware)

    @app.exception_handler(service.ServiceError)
    async def _svc_err(_: Request, e: service.ServiceError):
        headers = {}
        if e.status == 503 and isinstance(e.detail, dict) and e.detail.get("retry_after"):
            headers["Retry-After"] = str(int(e.detail["retry_after"]))
        return JSONResponse(e.to_dict(), status_code=e.status, headers=headers)

    app.include_router(build_router())
    try:  # recipes, drift, proactive seller (ported from Momin's junction26-valio)
        from aimo.gateway.extras import router as extras_router
        app.include_router(extras_router)
    except ImportError as e:  # pragma: no cover
        log.warning("extras router not mounted: %s", e)

    # Buyer's agent runs (/v1/agent/runs) if present.
    try:
        from aimo.buyer.runs import router as runs_router  # type: ignore

        app.include_router(runs_router)
    except Exception as e:  # ImportError until buyer lands
        log.info("buyer runs router not mounted: %s", e)

    if mcp_server is not None:
        allowed = os.environ.get("AIMO_MCP_ALLOWED_HOSTS")
        ts = None
        if allowed:
            from mcp.server.transport_security import TransportSecuritySettings

            ts = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                           allowed_hosts=[h.strip() for h in allowed.split(",")])
        mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0", transport_security=ts)
        for route in mcp_app.routes:
            if isinstance(route, Route):
                app.router.routes.append(route)

    web_dir = config.ROOT / "web"
    if web_dir.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=str(web_dir), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def _root():
            return RedirectResponse("/ui/")
    else:
        @app.get("/", include_in_schema=False)
        def _root():
            return RedirectResponse("/docs")

    return app


app = create_app()
