import functools
import os
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from aimo.models import (ApprovalDecision, Constraint, Mandate, Need, ParseRequest,
                         PlaceOrderRequest, QuoteRequest, SimulateStock)

class GuardRoute(APIRoute):
    def get_route_handler(self):
        inner = super().get_route_handler()

        async def handler(request):
            try:
                return await inner(request)
            except Err as e:
                return JSONResponse(e.body, status_code=e.status)

        return handler


router = APIRouter(route_class=GuardRoute)


class ParseResponse(Need):
    summary: str | None = None


class Err(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body


@dataclass
class Principal:
    customer: str | None
    agent_id: str | None
    mandate: object | None
    admin: bool = False


def admin_key():
    return os.environ.get("AIMO_ADMIN_KEY", "demo-admin")


def agent_from_key(key):
    from aimo import mandates
    return mandates.agent_for_key(key) if key else None


def is_admin(key):
    return bool(key) and key == admin_key()


def resolve_agent(key):
    hit = agent_from_key(key)
    if not hit:
        raise Err(401, {"code": "unauthorized", "text": "Missing or invalid X-Agent-Key."})
    return Principal(hit[0], hit[1], hit[2])


def resolve_admin(key, agent_key=None):
    if not is_admin(key):
        if not key and agent_from_key(agent_key):
            raise Err(403, {"code": "forbidden", "text": "This route needs an admin key."})
        raise Err(401, {"code": "unauthorized", "text": "Missing or invalid X-Admin-Key."})
    return Principal(None, None, None, True)


def resolve_any(agent_key, admin_key_value):
    if admin_key_value:
        return resolve_admin(admin_key_value)
    return resolve_agent(agent_key)


def _guard(fn):
    @functools.wraps(fn)
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except Err as e:
            return JSONResponse(e.body, status_code=e.status)
    return run


def _auth(resolver, *headers):
    def dep(x_agent_key: str | None = Header(None), x_admin_key: str | None = Header(None)):
        return resolver(*[{"agent": x_agent_key, "admin": x_admin_key}[h] for h in headers])
    return dep


agent = _auth(resolve_agent, "agent")
admin = _auth(resolve_admin, "admin", "agent")
anyone = _auth(resolve_any, "agent", "admin")


def domain(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        if type(e).__name__ in ("QuoteError", "ApprovalError") and hasattr(e, "code"):
            status = {"NOT_FOUND": 404, "NOT_APPROVER": 403}.get(e.code, 409)
            raise Err(status, {"code": e.code, "text": e.text})
        if type(e).__name__ == "UpstreamUnavailable":
            raise Err(503, {"code": getattr(e, "code", "UPSTREAM_UNAVAILABLE"), "text": getattr(e, "text", None) or str(e) or "The supplier system is unavailable, try again shortly."})
        raise


def own(p, customer):
    if not p.admin and customer != p.customer:
        raise Err(403, {"code": "forbidden", "text": f"This key is scoped to {p.customer}."})


def scope(p, customer):
    if p.admin:
        return customer
    if customer and customer != p.customer:
        raise Err(403, {"code": "forbidden", "text": f"This key is scoped to {p.customer}."})
    return p.customer


def search_products(q="", constraints=None, limit=50):
    from aimo import catalog
    return jsonable_encoder(catalog.search(q, constraints or Constraint(), limit))


def get_product(product_id):
    from aimo import catalog
    pr = catalog.get(product_id)
    if pr is None:
        raise Err(404, {"code": "not_found", "text": f"No product {product_id}."})
    return jsonable_encoder(pr)


def parse_request(req: ParseRequest, agent_key=None):
    from aimo import audit, parse
    need = parse.parse_need(req.text, req.customer)
    hit = agent_from_key(agent_key)
    actor = f"agent:{hit[1]}" if hit and hit[0] == req.customer else "system"
    audit.record(req.customer, actor, "parse.request", req.customer, parse.describe_need(need, "en"),
                 {"text": req.text})
    return jsonable_encoder(ParseResponse(**need.model_dump(), summary=parse.describe_need(need, "en")))


def _audit_offers(p, need, res):
    from aimo import audit, parse
    n = sum(len(g.offers) for g in res.items)
    short = sum(g.shortfall for g in res.items)
    best = [min(g.offers, key=lambda o: o.total_net) for g in res.items if g.offers]
    tail = ", ".join(f"{o.product.name} {o.total_net:.2f} EUR net" for o in best) or "no offer"
    audit.record(need.customer, f"agent:{p.agent_id}" if p.agent_id else "system", "offers.compared", need.customer,
                 f"Compared {n} offers for {parse.describe_need(need, 'en')}, best: {tail}, shortfall {short:g}.",
                 {"offers": n, "shortfall": short})


def get_offers(p, need: Need):
    from aimo import mandates, offers
    own(p, need.customer)
    mandate = p.mandate if p.mandate else mandates.get(need.customer)
    res = domain(offers.get_offers, need, mandate)
    _audit_offers(p, need, res)
    return jsonable_encoder(res)


def request_quote(p, req: QuoteRequest):
    from aimo import orders
    return jsonable_encoder(domain(orders.create_quote, p.customer, p.agent_id, req))


def place_order(p, req: PlaceOrderRequest):
    from aimo import orders
    return jsonable_encoder(domain(orders.place, p.customer, p.agent_id, req))


def get_order(p, order_id):
    from aimo import orders
    o = orders.get(order_id)
    if o is None:
        raise Err(404, {"code": "not_found", "text": f"No order {order_id}."})
    own(p, o.customer)
    return jsonable_encoder(o)


def list_orders(p, customer=None, status=None):
    from aimo import orders
    return jsonable_encoder(orders.list(status=status, customer=scope(p, customer)))


def list_approvals(p, customer=None):
    from aimo import approvals
    return jsonable_encoder(approvals.pending(scope(p, customer)))


def decide_approval(order_id, body: ApprovalDecision):
    from aimo import approvals
    return jsonable_encoder(domain(approvals.decide, order_id, body.approve, body.by, body.note or ""))


def get_audit_trail(p, customer=None, order_id=None):
    from aimo import audit
    events = audit.events(customer=scope(p, customer), order_id=order_id)
    ok, bad_seq = audit.verify()
    return {
        "events": jsonable_encoder(events),
        "text": audit.narrate(events),
        "verified": {"ok": ok, "bad_seq": bad_seq},
        "checkpoint": audit.checkpoint(),
    }


def get_mandate(p, customer=None):
    from aimo import mandates
    c = scope(p, customer)
    m = mandates.get(c, p.agent_id) if not p.admin else mandates.get(c)
    if m is None:
        raise Err(404, {"code": "not_found", "text": f"No mandate for {c}."})
    return jsonable_encoder(m)


def put_mandate(m: Mandate):
    from aimo import mandates
    out = mandates.put(m)
    return jsonable_encoder(out)


def simulate(body: SimulateStock):
    from aimo import offers
    domain(offers.simulate, body)
    return {"ok": True}


def clear_simulation():
    from aimo import offers
    domain(offers.clear_simulation)
    return {"ok": True}


@router.get("/products")
@_guard
def r_search_products(q: str = "", lactose_free: bool | None = None, gluten_free: bool | None = None,
                      exclude_allergens: list[str] = Query(default=[]), labels: list[str] = Query(default=[]),
                      origin: str | None = None, max_unit_price: float | None = None,
                      brands: list[str] = Query(default=[]), limit: int = 50):
    c = Constraint(lactose_free=lactose_free, gluten_free=gluten_free, exclude_allergens=exclude_allergens,
                   labels=labels, origin=origin, max_unit_price=max_unit_price, brands=brands)
    return search_products(q, c, limit)


@router.get("/products/{product_id}")
@_guard
def r_product(product_id: str):
    return get_product(product_id)


@router.post("/needs/parse", response_model=ParseResponse)
@_guard
def r_parse(req: ParseRequest, x_agent_key: str | None = Header(None)):
    return parse_request(req, x_agent_key)


@router.post("/offers")
@_guard
def r_offers(need: Need, p=Depends(agent)):
    return get_offers(p, need)


@router.post("/quotes")
@_guard
def r_quotes(req: QuoteRequest, p=Depends(agent)):
    return request_quote(p, req)


@router.post("/orders")
@_guard
def r_place(req: PlaceOrderRequest, p=Depends(agent)):
    return place_order(p, req)


@router.get("/orders")
@_guard
def r_orders(customer: str | None = None, status: str | None = None, p=Depends(anyone)):
    return list_orders(p, customer, status)


@router.get("/orders/{order_id}")
@_guard
def r_order(order_id: str, p=Depends(anyone)):
    return get_order(p, order_id)


@router.get("/approvals")
@_guard
def r_approvals(customer: str | None = None, p=Depends(anyone)):
    return list_approvals(p, customer)


@router.post("/approvals/{order_id}")
@_guard
def r_decide(order_id: str, body: ApprovalDecision, p=Depends(admin)):
    return decide_approval(order_id, body)


@router.get("/audit")
@_guard
def r_audit(customer: str | None = None, order_id: str | None = None, p=Depends(anyone)):
    return get_audit_trail(p, customer, order_id)


@router.get("/mandate")
@_guard
def r_mandate(customer: str | None = None, p=Depends(anyone)):
    return get_mandate(p, customer)


@router.put("/mandate")
@_guard
def r_put_mandate(m: Mandate, p=Depends(admin)):
    return put_mandate(m)


@router.post("/admin/simulate")
@_guard
def r_simulate(body: SimulateStock, p=Depends(admin)):
    return simulate(body)


@router.delete("/admin/simulate")
@_guard
def r_clear(p=Depends(admin)):
    return clear_simulation()
