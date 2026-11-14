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
        return os.environ.get("AIMO_AGENT_KEY"), os.environ.get("AIMO_STDIO_ADMIN_KEY")
    return h.get("x-agent-key"), h.get("x-admin-key")


def _run(ctx, auth, fn, *a):
    try:
        agent_key, admin_key = _keys(ctx)
        if auth == "none":
            return fn(*a, agent_key) if fn is api.parse_request else fn(*a)
        if auth == "agent":
            p = api.resolve_agent(agent_key)
        elif auth == "admin":
            p = api.resolve_admin(admin_key, agent_key)
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
    """Find products in the catalog. Call first when you need product ids or want to check what exists. Pass q (text such as "milk") and optional constraints. Returns a list of Product. No key needed."""
    c = Constraint(lactose_free=lactose_free, gluten_free=gluten_free, exclude_allergens=exclude_allergens or [],
                   labels=labels or [], origin=origin, max_unit_price=max_unit_price, brands=brands or [])
    return _run(ctx, "none", api.search_products, q, c, limit)


@mcp.tool()
def get_product(ctx: Context, product_id: str) -> dict:
    """Read one product (allergens, labels, unit size) by product_id taken from search_products or an offer. No key needed."""
    return _run(ctx, "none", api.get_product, product_id)


@mcp.tool()
def parse_request(ctx: Context, text: str, customer: str) -> dict:
    """Turn the buyer's free-text request into a structured Need plus a one-line summary. Call this first for any natural-language request; pass the text and the customer id. Feed the Need to get_offers. No key needed."""
    return _run(ctx, "none", api.parse_request, ParseRequest(text=text, customer=customer))


@mcp.tool()
def get_offers(ctx: Context, customer: str, deliver_by: date, items: list[NeedItem], raw_text: str | None = None) -> dict:
    """Compare offers for a Need: ranked options with price, stock, delivery date, reliability, CO2, shortfall and allergen-safe substitutes. Pass the fields of the Need from parse_request. Needs the agent key."""
    return _run(ctx, "agent", api.get_offers, Need(customer=customer, deliver_by=deliver_by, items=items, raw_text=raw_text))


@mcp.tool()
def request_quote(ctx: Context, offers: list[QuoteRequestLine], delivery_date: date) -> dict:
    """Lock chosen offers into a signed, single-use Quote valid until the supplier cutoff. Pass offers as [{product_id, quantity}] (add substitute_for when replacing an asked product) and delivery_date. Needs the agent key. Returns quote_id and jws for place_order."""
    return _run(ctx, "agent", api.request_quote, QuoteRequest(offers=offers, delivery_date=delivery_date))


@mcp.tool()
def place_order(ctx: Context, quote_id: str, jws: str, ask_human: bool = False, note: str | None = None) -> dict:
    """Place an order from a quote. Pass quote_id and jws exactly as returned by request_quote. The mandate decides: auto means placed, needs_approval waits for a human, blocked is refused. Set ask_human true to force a human check, with a note. Needs the agent key."""
    return _run(ctx, "agent", api.place_order, PlaceOrderRequest(quote_id=quote_id, jws=jws, ask_human=ask_human, note=note))


@mcp.tool()
def get_order(ctx: Context, order_id: str) -> dict:
    """Check the status, lines and decision reasons of one order by order_id. Call after place_order to see whether a human approved it."""
    return _run(ctx, "any", api.get_order, order_id)


@mcp.tool()
def list_orders(ctx: Context, customer: str | None = None, status: str | None = None) -> list[dict]:
    """List orders of your customer, optionally filtered by status (pending_approval, placed, confirmed, rejected, blocked, expired)."""
    return _run(ctx, "any", api.list_orders, customer, status)


@mcp.tool()
def list_approvals(ctx: Context, customer: str | None = None) -> list[dict]:
    """List orders waiting for a human approver. Call to see what is blocked on a person."""
    return _run(ctx, "any", api.list_approvals, customer)


@mcp.tool()
def decide_approval(ctx: Context, order_id: str, approve: bool, by: str, note: str | None = None) -> dict:
    """Human side only: approve or reject a pending order. Pass order_id, approve, by (the approver's name from the mandate) and an optional note. Needs the admin key."""
    return _run(ctx, "admin", api.decide_approval, order_id, ApprovalDecision(approve=approve, by=by, note=note))


@mcp.tool()
def get_audit_trail(ctx: Context, customer: str | None = None, order_id: str | None = None) -> dict:
    """Explain what happened: hash-chained audit events with a plain-language text, a verification result and a signed checkpoint. Filter by order_id to explain one order."""
    return _run(ctx, "any", api.get_audit_trail, customer, order_id)


@mcp.tool()
def get_mandate(ctx: Context, customer: str | None = None) -> dict:
    """Read your own limits before ordering: per-order and per-week EUR, excluded allergens and substitution rule. Call to know what you may buy without a human."""
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
        prev = holder.get("asgi")
        build()
        try:
            async with outer(a), mcp.session_manager.run():
                yield
        finally:
            holder["asgi"] = prev

    app.router.lifespan_context = lifespan


if __name__ == "__main__":
    mcp.run("stdio")
