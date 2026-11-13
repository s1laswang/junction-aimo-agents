import contextlib
import json
import os
from datetime import date

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from aimo import api
from aimo.models import (ApprovalDecision, Constraint, Need, NeedItem, ParseRequest,
                         PlaceOrderRequest, QuoteRequest, QuoteRequestLine)

mcp = MCPServer("aimo")


def _headers(ctx):
    try:
        return ctx.headers
    except Exception:
        return None


def _keys(ctx):
    h = _headers(ctx)
    if h is None:
        return os.environ.get("AIMO_AGENT_KEY"), None
    return h.get("x-agent-key"), h.get("x-admin-key")


def _run(ctx, auth, fn, *a):
    try:
        agent_key, admin_key = _keys(ctx)
        if auth == "none":
            return fn(*a)
        if auth == "agent":
            p = api.resolve_agent(agent_key)
        elif auth == "admin":
            p = api.resolve_admin(admin_key)
        else:
            p = api.resolve_any(agent_key, admin_key)
        return fn(p, *a) if auth != "admin" else fn(*a)
    except api.Err as e:
        raise ToolError(json.dumps(e.body))


@mcp.tool()
def search_products(ctx: Context, q: str = "", lactose_free: bool | None = None, gluten_free: bool | None = None,
                    exclude_allergens: list[str] | None = None, labels: list[str] | None = None,
                    origin: str | None = None, max_unit_price: float | None = None,
                    brands: list[str] | None = None, limit: int = 50) -> list[dict]:
    """Search the catalog. Returns a list of Product."""
    c = Constraint(lactose_free=lactose_free, gluten_free=gluten_free, exclude_allergens=exclude_allergens or [],
                   labels=labels or [], origin=origin, max_unit_price=max_unit_price, brands=brands or [])
    return _run(ctx, "none", api.search_products, q, c, limit)


@mcp.tool()
def get_product(ctx: Context, product_id: str) -> dict:
    """One Product by id."""
    return _run(ctx, "none", api.get_product, product_id)


@mcp.tool()
def parse_request(ctx: Context, text: str, customer: str) -> dict:
    """Parse free text into a Need."""
    return _run(ctx, "none", api.parse_request, ParseRequest(text=text, customer=customer))


@mcp.tool()
def get_offers(ctx: Context, customer: str, deliver_by: date, items: list[NeedItem], raw_text: str | None = None) -> dict:
    """Ranked offers per need item: {items: [{need_item, offers, shortfall}]}."""
    return _run(ctx, "agent", api.get_offers, Need(customer=customer, deliver_by=deliver_by, items=items, raw_text=raw_text))


@mcp.tool()
def request_quote(ctx: Context, offers: list[QuoteRequestLine], delivery_date: date) -> dict:
    """Signed Quote. offers is [{product_id, quantity, substitute_for?}]."""
    return _run(ctx, "agent", api.request_quote, QuoteRequest(offers=offers, delivery_date=delivery_date))


@mcp.tool()
def place_order(ctx: Context, quote_id: str, jws: str, ask_human: bool = False, note: str | None = None) -> dict:
    """Place an order from a signed quote. Returns Order with Decision. ask_human forces human approval."""
    return _run(ctx, "agent", api.place_order, PlaceOrderRequest(quote_id=quote_id, jws=jws, ask_human=ask_human, note=note))


@mcp.tool()
def get_order(ctx: Context, order_id: str) -> dict:
    """Get one Order."""
    return _run(ctx, "any", api.get_order, order_id)


@mcp.tool()
def list_orders(ctx: Context, customer: str | None = None, status: str | None = None) -> list[dict]:
    """List Orders, filtered by customer and status."""
    return _run(ctx, "any", api.list_orders, customer, status)


@mcp.tool()
def list_approvals(ctx: Context, customer: str | None = None) -> list[dict]:
    """Orders waiting for human approval."""
    return _run(ctx, "any", api.list_approvals, customer)


@mcp.tool()
def decide_approval(ctx: Context, order_id: str, approve: bool, by: str, note: str | None = None) -> dict:
    """Approve or reject a pending order. Needs the admin key."""
    return _run(ctx, "admin", api.decide_approval, order_id, ApprovalDecision(approve=approve, by=by, note=note))


@mcp.tool()
def get_audit_trail(ctx: Context, customer: str | None = None, order_id: str | None = None) -> dict:
    """Audit events and a plain-language summary text."""
    return _run(ctx, "any", api.get_audit_trail, customer, order_id)


@mcp.tool()
def get_mandate(ctx: Context, customer: str | None = None) -> dict:
    """The Mandate of this agent."""
    return _run(ctx, "any", api.get_mandate, customer)


def mount(app):
    holder = {}
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    def build():
        sub = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security)
        holder["asgi"] = sub.routes[0].endpoint

    class Asgi:
        async def __call__(self, scope, receive, send):
            await holder["asgi"](scope, receive, send)

    asgi = Asgi()

    build()
    app.router.routes.append(Route("/mcp", endpoint=asgi))
    outer = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        build()
        async with outer(a), mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan


if __name__ == "__main__":
    mcp.run("stdio")
