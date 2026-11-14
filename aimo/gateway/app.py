"""FastAPI gateway: the open door for any company's purchasing agent. Owner: gateway.

Run: `uv run aimo serve` (or `uvicorn aimo.gateway.app:app`). Docs at /docs, OpenAPI at /openapi.json,
MCP (streamable HTTP) at /mcp, discovery manifest at /.well-known/otap.json.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from aimo import config
from aimo.backend.client import reset_request_faults, set_request_faults
from aimo.backend import clock
from aimo.gateway import auth, human_auth, service
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
    400: {"description": "Bad request"},
    401: {"description": "Identity or credential required"},
    403: {"description": "Identity or mandate problem"},
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


class HumanCred:
    def __init__(self, pin: Optional[str], session: Optional[str], source: str):
        self.pin, self.session, self.source = pin, session, source


def human_cred(request: Request,
               x_approver_pin: Optional[str] = Header(None, description="Human approver credential (never given to agents)"),
               x_approver_session: Optional[str] = Header(None, description="Signed approver session from "
                                                                            "POST /v1/auth/approver-session")) -> HumanCred:
    return HumanCred(x_approver_pin, x_approver_session, (request.client.host if request.client else "?"))


def _is_human(cred: HumanCred) -> bool:
    return human_auth.check_session(cred.session) or human_auth.check_pin(cred.pin, cred.source)


def _locked_error(cred: HumanCred) -> service.ServiceError:
    return service.ServiceError(429, "approver_locked", "Too many wrong approver credentials from this client; try "
                                "again in a few minutes (a signed-in approver session is not affected).",
                                {"retry_after": int(human_auth.locked_for(cred.source)) + 1})


def _require_human(cred: HumanCred, subject_id: str, actor: str) -> None:
    """Session token, or approver credential with a per-source brute-force lockout. Audits at most MAX_FAILURES
    refusals per source and window plus the lock itself, so guessing cannot flood the audit log or lock out the
    real (signed-in) approver."""
    if human_auth.check_session(cred.session):
        return
    if human_auth.locked_for(cred.source) > 0:
        raise _locked_error(cred)
    if human_auth.check_pin(cred.pin, cred.source):
        return
    if human_auth.locked_for(cred.source) > 0:  # this attempt triggered the lock
        service.audit_refusal(subject_id, actor, f"{human_auth.MAX_FAILURES} wrong approver credentials from one "
                                                 f"client; that client is locked for {int(human_auth.LOCK_S)} s")
        raise _locked_error(cred)
    service.audit_refusal(subject_id, actor, "wrong approver credential" if cred.pin else "missing approver credential")
    raise service.ServiceError(401, "approver_auth_required",
                               "Approval needs a signed-in human approver (X-Approver-Session or X-Approver-Pin).")


def strict_identity(
    x_agent_id: Optional[str] = Header(None, description="Calling agent id, e.g. buyer-1 (required)"),
    x_customer_id: Optional[str] = Header(None, description="Customer the agent acts for, e.g. hotel-aurora (required)"),
) -> service.Identity:
    """Routes that create quotes or orders need an explicit, known caller: no default identity."""
    if not x_agent_id or not x_customer_id:
        raise service.ServiceError(401, "identity_required",
                                   "Quotes and orders need X-Customer-Id and X-Agent-Id (production: OAuth 2.1 bearer).")
    return identity(x_agent_id, x_customer_id)


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

    @r.get("/v1/products/search", tags=["catalog"], summary="Search products", response_model=list[ProductSummary], responses=ERR)
    def search_products(
        q: str = Query(..., description="Free text (English or Finnish)", examples=["lactose free milk"]),
        claims: Optional[str] = Query(None, description="Comma-separated required claims", examples=["FREE_FROM_LACTOSE"]),
        category: Optional[str] = Query(None, description="Category code, e.g. 10400"),
        content_unit: Optional[str] = Query(None, description="L | KG | PCS"),
        limit: int = Query(20, ge=1, le=100),
        customer_id: Optional[str] = Query(None, description="Customer id (defaults to X-Customer-Id)"),
        x_customer_id: Optional[str] = Header(None),
        x_agent_key: Optional[str] = Header(None),
    ):
        auth.resolve_customer_identity(x_customer_id=x_customer_id, body_customer_id=customer_id, x_agent_key=x_agent_key)
        cl = [c.strip() for c in claims.split(",") if c.strip()] if claims else []
        res = service.find_products(q, cl, category, content_unit, limit)
        return JSONResponse([p.model_dump(mode="json") for p in res])

    @r.get("/v1/products/{product_id}", tags=["catalog"], summary="Product details", response_model=Product, responses=ERR)
    def get_product(
        product_id: str,
        x_customer_id: Optional[str] = Header(None),
        x_agent_key: Optional[str] = Header(None),
    ):
        """Supplier fields such as `marketing_text` are UNTRUSTED: screen before showing them to an LLM."""
        if x_customer_id and x_customer_id.strip():
            cid = auth.require_known_customer(x_customer_id)
            auth.check_agent_key(cid, x_agent_key)
        return service.get_product(product_id)

    @r.post("/v1/offers", tags=["offers"], summary="Live price + availability", response_model=list[Offer], responses=ERR)
    def offers(
        body: OffersRequest,
        x_agent_id: Optional[str] = Header(None, description="Calling agent id, e.g. buyer-1"),
        x_customer_id: Optional[str] = Header(None, description="Customer the agent acts for, e.g. hotel-aurora"),
        x_agent_key: Optional[str] = Header(None),
    ):
        """Merged `/api/price` + `/api/availability` from the Valio Aimo API. `cutoff_passed` is true when the
        product's order closing time is already past (it cannot make the requested date). Provenance is in
        each offer's `provenance` and in the `X-Aimo-*` response headers."""
        who = auth.resolve_customer_identity(
            x_customer_id=x_customer_id,
            body_customer_id=body.customer_id,
            x_agent_id=x_agent_id,
            x_agent_key=x_agent_key,
        )
        res, meta = service.fetch_offers(body.product_ids, body.requested_date, who.customer_id)
        return JSONResponse([o.model_dump(mode="json") for o in res], headers=_prov_headers(meta))

    @r.post("/v1/plan", tags=["plan"], summary="Plan a need (good/better/best + split)", response_model=Plan, responses=ERR)
    def plan(
        need: Need,
        x_agent_id: Optional[str] = Header(None, description="Calling agent id, e.g. buyer-1"),
        x_customer_id: Optional[str] = Header(None, description="Customer the agent acts for, e.g. hotel-aurora"),
        x_agent_key: Optional[str] = Header(None),
    ):
        who = auth.resolve_customer_identity(
            x_customer_id=x_customer_id,
            body_customer_id=need.customer_id,
            x_agent_id=x_agent_id,
            x_agent_key=x_agent_key,
        )
        return service.plan_need(need.model_copy(update={"customer_id": who.customer_id}), identity=who)

    @r.post("/v1/quotes", tags=["quotes"], summary="Create a signed quote", response_model=Quote, responses=ERR)
    def create_quote(
        body: QuoteRequest,
        x_agent_id: Optional[str] = Header(None, description="Calling agent id, e.g. buyer-1 (required)"),
        x_customer_id: Optional[str] = Header(None, description="Customer the agent acts for, e.g. hotel-aurora (required)"),
        x_agent_key: Optional[str] = Header(None),
    ):
        """Re-fetches live offers, refuses lines that are unavailable / past cut-off / short of stock (409 with
        per-line reasons), computes totals, signs the quote (quote_hash + JWS) and fixes the price until
        `expires_at`."""
        who = auth.resolve_customer_identity(
            x_customer_id=x_customer_id,
            body_customer_id=body.customer_id,
            x_agent_id=x_agent_id,
            body_agent_id=body.agent_id,
            x_agent_key=x_agent_key,
            require_headers=True,
        )
        return service.make_quote(who, body.delivery_date, [l.model_dump() for l in body.lines], body.need)

    @r.get("/v1/quotes/{quote_id}", tags=["quotes"], summary="Get a quote", response_model=Quote, responses=ERR)
    def get_quote(quote_id: str):
        return service.get_quote(quote_id)

    @r.post("/v1/orders", tags=["orders"], summary="Place an order for a signed quote", response_model=Order, responses=ERR)
    def place_order(
        body: OrderRequest,
        x_agent_id: Optional[str] = Header(None, description="Calling agent id, e.g. buyer-1 (required)"),
        x_customer_id: Optional[str] = Header(None, description="Customer the agent acts for, e.g. hotel-aurora (required)"),
        x_agent_key: Optional[str] = Header(None),
    ):
        """Verifies quote hash, signature and expiry, then evaluates the customer's policy:
        `placed` (auto-approved, with Peppol-style response), `pending_approval` or `rejected`. Idempotent per quote."""
        who = auth.resolve_customer_identity(
            x_customer_id=x_customer_id,
            body_customer_id=getattr(body, "customer_id", None),
            x_agent_id=x_agent_id,
            body_agent_id=getattr(body, "agent_id", None),
            x_agent_key=x_agent_key,
            require_headers=True,
            defer_unknown_customer=True,
        )
        return service.submit_order(who, body.quote_id, body.quote_hash)

    @r.get("/v1/orders/{order_id}", tags=["orders"], summary="Order status", response_model=Order, responses=ERR)
    def get_order(order_id: str):
        return service.get_order(order_id)

    @r.get("/v1/orders/{order_id}/peppol-order-response.xml", tags=["orders"], responses=ERR,
           summary="Peppol BIS Advanced Ordering 3.0 Order Response Advanced (T116, UBL 2.3) for the order",
           response_class=PlainTextResponse)
    def get_order_peppol(order_id: str):
        """AP/CA/RE from placement; AB while the order waits for approval. Party ids are placeholders.
        Checked against the UBL 2.3 XSD and the PEPPOL-T116 rules in tests, not the full Peppol schematron."""
        from aimo.backend.peppol import order_response_xml
        o = service.get_order(order_id)
        return PlainTextResponse(order_response_xml(o, service.get_quote(o.quote_id)), media_type="application/xml")

    @r.get("/v1/approvals", tags=["approvals"], summary="Approval inbox", responses=ERR)
    def approvals(
        status: Optional[str] = Query("pending", description="pending | placed | rejected | expired | all"),
        customer_id: Optional[str] = Query(None),
        x_customer_id: Optional[str] = Header(None),
        x_agent_key: Optional[str] = Header(None),
        cred: HumanCred = Depends(human_cred),
    ) -> list[dict[str, Any]]:
        """Orders with their exact quote and policy decision: `[{order, quote, decision, expired, approvals}]`.
        A signed-in human approver sees the inbox; an agent sees only its own customer's orders."""
        if _is_human(cred):
            return service.list_approvals(status, customer_id)
        if not x_customer_id or not x_customer_id.strip():
            raise service.ServiceError(401, "identity_required",
                                       "Send X-Customer-Id (agents) or X-Approver-Pin (human approvers).")
        cid = auth.require_known_customer(x_customer_id)
        auth.check_agent_key(cid, x_agent_key)
        if customer_id and customer_id.strip() and customer_id.strip() != cid:
            raise service.ServiceError(403, "customer_mismatch", "you can only list your own customer's orders")
        return service.list_approvals(status, cid)

    @r.post("/v1/approvals/{order_id}/decision", tags=["approvals"], summary="Approve or reject (humans only)",
            response_model=Order, responses=ERR)
    def decide(order_id: str, body: DecisionRequest,
               x_agent_id: Optional[str] = Header(None),
               cred: HumanCred = Depends(human_cred)):
        """Human decision. Requires a signed-in approver (`X-Approver-Session` from POST /v1/auth/approver-session,
        or `X-Approver-Pin`); any request carrying agent identity (`X-Agent-Id`) is refused,
        and the approver must be a human in the order customer's directory with an allowed role.
        `quote_hash` must match the order's quote (409 otherwise); an expired quote returns 409 "re-quote needed".
        Approve places the order and returns the order response."""
        if x_agent_id:
            service.audit_refusal(order_id, f"agent:{x_agent_id}", "agent identity on the approval endpoint")
            raise service.ServiceError(403, "agent_cannot_approve",
                                       "Agents can never approve orders; a human approver must decide in the inbox.")
        approver = body.approver
        bound = human_auth.session_approver(cred.session)
        if bound:  # a session is bound to one person: decisions are recorded under that person only
            if approver and service.directory_approver_id(approver) != bound:
                service.audit_refusal(order_id, f"human:{bound}", f"session of {bound} tried to decide as {approver}")
                raise service.ServiceError(403, "approver_mismatch",
                                           f"This session belongs to {bound}; it cannot decide as {approver}.")
            approver = bound
        else:
            # demo path: shared PIN + self-declared name (production: per-person SSO; see docs/TRUST.md)
            _require_human(cred, order_id, f"human:{body.approver}")
            if not approver:
                raise service.ServiceError(422, "approver_required", "Name the approver, or sign in with "
                                           "POST /v1/auth/approver-session to bind decisions to you.")
        return service.decide(order_id, body.decision, approver, body.quote_hash, body.note, body.approver_role,
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
                                                                                 "'approved' to 'rejected')"),
                    cred: HumanCred = Depends(human_cred)):
        """Rewrites audit event `seq` in place, bypassing the append-only guard, so `GET /v1/audit/verify` shows the
        broken chain. Restore with `POST /v1/demo/untamper/{seq}`. Needs a signed-in approver; 404 when
        AIMO_ENV=production."""
        _require_human(cred, f"audit:{seq}", "human:demo-tamper")
        return service.demo_tamper(seq, summary)

    @r.post("/v1/demo/untamper/{seq}", tags=["demo"], summary="DEMO ONLY: restore a tampered audit event", responses=ERR)
    def demo_untamper(seq: int, cred: HumanCred = Depends(human_cred)):
        _require_human(cred, f"audit:{seq}", "human:demo-untamper")
        return service.demo_untamper(seq)

    # ---------------------------------------------------------- human sign-in
    @r.post("/v1/auth/approver-session", tags=["approvals"], summary="Human approver sign-in (returns a session token)",
            responses=ERR)
    def approver_session(body: dict[str, Any] = Body(default={}, examples=[{"approver": "anna@hotel-aurora.example"}]),
                         x_agent_id: Optional[str] = Header(None), cred: HumanCred = Depends(human_cred)):
        """Sign in as ONE named human approver: `{"approver": "<directory id or name>"}` + `X-Approver-Pin` returns
        an 8-hour signed session token bound to that person, to send as `X-Approver-Session`. Decisions made with
        it are recorded under that approver only. Agents are refused. A session keeps working even if someone else
        triggers a brute-force lock."""
        if x_agent_id:
            raise service.ServiceError(403, "agent_cannot_approve", "Agents cannot sign in as approvers.")
        who = str((body or {}).get("approver") or "").strip()
        _require_human(HumanCred(cred.pin, None, cred.source), "auth:approver-session", f"human:{who or '?'}")
        canonical = service.directory_approver_id(who) if who else None
        if not canonical:
            raise service.ServiceError(404, "unknown_approver",
                                       "Name a human from a customer's approver directory, e.g. anna@hotel-aurora.example.")
        return human_auth.issue_session(canonical)

    @r.get("/v1/customers/{customer_id}/learned", tags=["customers"], summary="Learned approvals (remembered swaps)",
           responses=ERR)
    def learned(
        customer_id: str,
        x_customer_id: Optional[str] = Header(None),
        x_agent_key: Optional[str] = Header(None),
    ) -> dict[str, Any]:
        """Swaps a human approved with `remember: true`. Visible only to the customer itself (403 otherwise)."""
        cid = auth.require_known_customer(customer_id)
        if x_customer_id and x_customer_id.strip():
            xcid = auth.require_known_customer(x_customer_id)
            if xcid != cid:
                raise service.ServiceError(403, "customer_mismatch", "you can only read your own customer's learned approvals")
        auth.check_agent_key(cid, x_agent_key)
        return service.learned_for(cid)

    @r.get("/v1/customers", tags=["customers"], summary="Demo customers and policy summaries")
    def customers(cred: HumanCred = Depends(human_cred)) -> list[dict[str, Any]]:
        """Customers, mandates and plain-language policy summaries. Approver identities (ids/emails) are shown only
        to a signed-in human approver; agents see names and roles."""
        out = service.list_customers()
        if _is_human(cred):
            return out
        masked = []
        for c in out:
            c = dict(c)
            c["approvers"] = [{"name": a.get("name"), "role": a.get("role")} for a in (c.get("approvers") or [])]
            masked.append(c)
        return masked

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
    try: from aimo.voice.routes import router as _voice; app.include_router(_voice)  # noqa: E702 S5 voice approval
    except Exception as e: log.warning("voice router not mounted: %s", e)  # noqa: BLE001
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
