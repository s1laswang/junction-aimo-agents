"""Gateway extras ported from Momin's junction26-valio (with credit): recipes, standing-order drift and the
proactive seller agent. Owner: port. HTTP routes + MCP tools over the same functions.

Hook (gateway owner applies; both idempotent):
    # aimo/gateway/app.py, in create_app() after app.include_router(build_router()):
    from aimo.gateway.extras import router as extras_router; app.include_router(extras_router)
    # aimo/gateway/mcp_server.py, in build_mcp() before `return mcp`:
    from aimo.gateway.extras import register_mcp_tools; register_mcp_tools(mcp)

HTTP:  GET /v1/recipes, POST /v1/recipes/plan, GET /v1/drift/{customer_id}, POST /v1/proposals,
       GET /v1/proposals?customer_id=, GET /v1/proposals/{id}, POST /v1/proposals/{id}/accept|decline
MCP:   list_recipes, plan_recipe, check_standing_order_drift, propose_offer
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:  # MCP SDK present in this project; annotations below are evaluated by the SDK at registration
    from mcp.server.mcpserver import Context
except ImportError:  # pragma: no cover
    Context = Any  # type: ignore[misc,assignment]

from aimo.gateway import auth, service

TAG_RECIPES = "recipes"
TAG_DRIFT = "drift"
TAG_PROPOSALS = "proposals"


class RecipePlanRequest(BaseModel):
    recipe_id: Optional[str] = Field(None, description="Recipe id or alias from GET /v1/recipes, e.g. lasagne")
    text: str = Field("", description="Free text instead of / besides recipe_id, e.g. 'Lasagne for 120 on Friday'")
    portions: Optional[int] = Field(None, ge=1, le=100000)
    delivery_by: Optional[str] = Field(None, description="YYYY-MM-DD or a weekday name; default from text")
    customer_id: Optional[str] = None
    excluded_allergens: list[str] = Field(default_factory=list, description="EU14 keys, added to the policy's")
    lactose_free: bool = False
    gluten_free: bool = False
    include_candidates: bool = False


class ProposalRequest(BaseModel):
    customer_id: str = Field(..., description="Customer the seller makes the offer to")
    product_id: Optional[str] = Field(None, description="Product to propose (e.g. a seasonal item); omit to let "
                                                        "the seller find a cheaper equivalent of a usual item")
    qty_sales_units: Optional[float] = Field(None, gt=0)
    replaces_product_id: Optional[str] = None
    delivery_date: Optional[date] = None
    note: str = ""


class ProposalDecision(BaseModel):
    reason: str = ""


def _ident(x_agent_id: Optional[str], x_customer_id: Optional[str]) -> service.Identity:
    return service.Identity(agent_id=x_agent_id or service.DEFAULT_AGENT,
                            customer_id=x_customer_id or service.DEFAULT_CUSTOMER)


def _err(e: Exception) -> JSONResponse:
    st = getattr(e, "status", 400)
    d = {"error": getattr(e, "code", "error"), "message": getattr(e, "message", str(e))}
    if getattr(e, "detail", None) is not None:
        d["detail"] = e.detail  # type: ignore[attr-defined]
    return JSONResponse(d, status_code=st)


def _upstream(e: Exception) -> service.ServiceError:
    return service._upstream(e)  # noqa: SLF001  same 503 + Retry-After mapping as the core routes


# ------------------------------------------------------------------ shared functions (HTTP + MCP)
def do_plan_recipe(body: RecipePlanRequest, ident: service.Identity) -> dict[str, Any]:
    from aimo.backend.client import UpstreamError
    from aimo.recipes import RecipeError, plan_recipe

    if getattr(ident, "authenticated", {}).get("customer") and body.customer_id:
        who = service.claim_identity(ident, body.customer_id, None)
    elif body.customer_id and body.customer_id.strip():
        who = service.Identity(agent_id=ident.agent_id, customer_id=body.customer_id.strip())
    else:
        who = ident
    cid = auth.require_known_customer(who.customer_id)
    try:
        return plan_recipe(body.recipe_id, body.portions, body.delivery_by, cid, text=body.text,
                           excluded_allergens=body.excluded_allergens, lactose_free=body.lactose_free,
                           gluten_free=body.gluten_free, include_candidates=body.include_candidates)
    except RecipeError as e:
        raise service.ServiceError(e.status, e.code, e.message) from e
    except UpstreamError as e:
        raise _upstream(e) from e


def do_drift(customer_id: str, rebaseline: bool = False) -> dict[str, Any]:
    from aimo.agents.drift import SEEDS, check_drift
    from aimo.backend.client import UpstreamError

    if customer_id not in SEEDS:
        raise service.ServiceError(404, "no_standing_order", f"no standing order on file for {customer_id!r}",
                                   {"customers": list(SEEDS)})
    try:
        return check_drift(customer_id, rebaseline=rebaseline)
    except UpstreamError as e:
        raise _upstream(e) from e


def do_propose(body: ProposalRequest, agent_id: Optional[str]) -> dict[str, Any]:
    from aimo.agents.seller import ProposalError, propose_offer
    from aimo.backend.client import UpstreamError

    cid = auth.require_known_customer(body.customer_id)
    try:
        return propose_offer(cid, product_id=body.product_id, qty_sales_units=body.qty_sales_units,
                             replaces_product_id=body.replaces_product_id, delivery_date=body.delivery_date,
                             note=body.note, agent_id=agent_id)
    except ProposalError as e:
        raise service.ServiceError(e.status, e.code, e.message, e.detail) from e
    except UpstreamError as e:
        raise _upstream(e) from e


def _svc(fn, *a, **kw):
    from aimo.agents.seller import ProposalError

    try:
        return fn(*a, **kw)
    except ProposalError as e:
        raise service.ServiceError(e.status, e.code, e.message, e.detail) from e


# ------------------------------------------------------------------ HTTP
router = APIRouter()


@router.get("/v1/recipes", tags=[TAG_RECIPES], summary="Recipes (dish -> per-portion ingredients)")
def list_recipes() -> list[dict[str, Any]]:
    """Dishes with illustrative per-portion ingredient quantities (recipe data by Momin, junction26-valio;
    not Valio figures)."""
    from aimo.recipes import list_recipes as lr

    return lr()


@router.post("/v1/recipes/plan", tags=[TAG_RECIPES], summary="Plan a recipe: scale, map to products, quote lines")
def plan_recipe(
    body: RecipePlanRequest,
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Scales the dish to the portions, applies allergen swaps from the customer's policy (a milk-allergy
    kitchen gets the dairy-free option), plans every ingredient with live offers (one batched mock-API call)
    and returns per-ingredient plans plus `quote_request` for POST /v1/quotes."""
    who = auth.resolve_customer_identity(
        x_customer_id=x_customer_id,
        body_customer_id=body.customer_id,
        x_agent_id=x_agent_id,
        x_agent_key=x_agent_key,
    )
    return do_plan_recipe(body, who)


@router.get("/v1/drift/{customer_id}", tags=[TAG_DRIFT], summary="Standing-order drift (price / volume creep)")
def drift(
    customer_id: str,
    rebaseline: bool = Query(False, description="Reset the price baseline to today's prices"),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Checks the customer's standing order (seeded 8-week history) against live offers for the next delivery:
    `flags[] {product_id, kind: price|volume|availability|allergen, change_pct, message, route}` in plain
    language. Price baseline = first live price this gateway observed. Honours `X-Aimo-Faults`."""
    if rebaseline:
        if not x_customer_id or not x_customer_id.strip():
            raise service.ServiceError(401, "identity_required", "Rebaselining standing-order drift requires X-Customer-Id.")
        xcid = auth.require_known_customer(x_customer_id)
        cid = auth.require_known_customer(customer_id)
        if xcid != cid:
            raise service.ServiceError(
                403,
                "customer_mismatch",
                f"request names customer {customer_id!r} but the caller is authenticated as {xcid!r}",
            )
        auth.check_agent_key(cid, x_agent_key)
        return do_drift(cid, rebaseline=True)
    return do_drift(customer_id, False)


@router.post("/v1/proposals", tags=[TAG_PROPOSALS], summary="Seller agent proposes an offer (cannot order)")
def create_proposal(
    body: ProposalRequest,
    x_agent_id: Optional[str] = Header(None, description="valio-aimo/<agent>"),
) -> dict[str, Any]:
    """A Valio Aimo seller agent proposes an offer to a customer. Returns the proposal with a non-binding
    preview of the customer's policy verdict. The seller can never place the order: only the customer's own
    agent can accept (POST /v1/proposals/{id}/accept), and then the normal signed quote + policy path decides."""
    from aimo.agents.seller import is_seller

    aid = (x_agent_id or "").strip()
    if not aid:
        raise service.ServiceError(401, "identity_required", "POST /v1/proposals requires X-Agent-Id (valio-aimo/<agent>).")
    if not is_seller(aid):
        raise service.ServiceError(403, "seller_required", "Only a seller agent (valio-aimo/*) may create proposals.")
    auth.require_known_customer(body.customer_id)
    return do_propose(body, aid)


@router.get("/v1/proposals", tags=[TAG_PROPOSALS], summary="List proposals")
def proposals(customer_id: Optional[str] = Query(None), status: Optional[str] = Query(None, description=
              "proposed | accepted | declined | all")) -> list[dict[str, Any]]:
    from aimo.agents.seller import list_proposals

    return list_proposals(customer_id, status)


@router.get("/v1/proposals/{proposal_id}", tags=[TAG_PROPOSALS], summary="Get a proposal")
def proposal(proposal_id: str) -> dict[str, Any]:
    from aimo.agents.seller import get_proposal

    return _svc(get_proposal, proposal_id)


@router.post("/v1/proposals/{proposal_id}/accept", tags=[TAG_PROPOSALS], summary="Customer's agent accepts")
def accept(
    proposal_id: str,
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Customer side only (403 `seller_cannot_order` for valio-aimo/* agents). Creates a signed quote under the
    buyer's identity and places the order: the customer's policy decides (placed / pending_approval / rejected)."""
    from aimo.agents.seller import accept_proposal, is_seller

    if not (x_agent_id and x_agent_id.strip()) or not (x_customer_id and x_customer_id.strip()):
        raise service.ServiceError(401, "identity_required", "Accepting a proposal requires X-Customer-Id and X-Agent-Id.")
    if not is_seller(x_agent_id):
        cid = auth.require_known_customer(x_customer_id)
        auth.check_agent_key(cid, x_agent_key)
    return _svc(accept_proposal, proposal_id, _ident(x_agent_id, x_customer_id))


@router.post("/v1/proposals/{proposal_id}/decline", tags=[TAG_PROPOSALS], summary="Customer's agent declines")
def decline(
    proposal_id: str,
    body: ProposalDecision = Body(default_factory=ProposalDecision),
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    from aimo.agents.seller import decline_proposal, is_seller

    if not (x_agent_id and x_agent_id.strip()) or not (x_customer_id and x_customer_id.strip()):
        raise service.ServiceError(401, "identity_required", "Declining a proposal requires X-Customer-Id and X-Agent-Id.")
    if is_seller(x_agent_id):
        raise service.ServiceError(403, "seller_cannot_order",
                                   "a seller agent can only propose; the customer's own agent must decide")
    cid = auth.require_known_customer(x_customer_id)
    auth.check_agent_key(cid, x_agent_key)
    return _svc(decline_proposal, proposal_id, _ident(x_agent_id, cid), body.reason)


# ------------------------------------------------------------------ MCP
EXTRA_TOOLS = ("list_recipes", "plan_recipe", "check_standing_order_drift", "propose_offer")


def register_mcp_tools(mcp: Any) -> Any:
    """Add the extra tools to an MCPServer (skips tools that are already registered)."""
    from aimo.gateway.mcp_server import _call, _headers, _identity

    try:
        have = {t.name for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
    except Exception:  # noqa: BLE001
        have = set()

    def add(fn):
        if fn.__name__ not in have:
            mcp.tool()(fn)

    def list_recipes(ctx: Context = None) -> dict[str, Any]:
        """Dishes the recipe planner knows (lasagne, salmon soup, pancakes, ...) with per-portion ingredient
        quantities (illustrative, not Valio figures). Use plan_recipe to turn one into quote lines."""
        from aimo.recipes import list_recipes as lr

        return {"recipes": lr()}

    def plan_recipe(recipe_id: Optional[str] = None, portions: Optional[int] = None, delivery_by: Optional[str] = None,
                    text: str = "", excluded_allergens: Optional[list[str]] = None, ctx: Context = None) -> dict[str, Any]:
        """Menu -> recipe -> order. Scale a dish to `portions` (or parse free text like "Lasagne for 120 on
        Friday"), apply the customer's allergen swaps, plan every ingredient against the catalog with live
        offers, and return per-ingredient plans, what is not covered, and `quote_request` = {delivery_date,
        lines} to pass to create_quote. Then place_order; the customer's policy decides."""
        body = RecipePlanRequest(recipe_id=recipe_id, portions=portions, delivery_by=delivery_by, text=text,
                                 excluded_allergens=excluded_allergens or [])
        return _call(ctx, do_plan_recipe, body, _identity(ctx))

    def check_standing_order_drift(customer_id: Optional[str] = None, ctx: Context = None) -> dict[str, Any]:
        """Check the customer's standing order for drift before the next delivery: price creep (vs the first
        price we observed), volume creep over the 8-week history, items that cannot be delivered, allergen
        conflicts. Returns flags with plain-language messages and route auto|approval."""
        ident = _identity(ctx)
        return _call(ctx, lambda: do_drift(auth.require_known_customer(customer_id) if customer_id else ident.customer_id))

    def propose_offer(customer_id: str, product_id: Optional[str] = None, qty_sales_units: Optional[float] = None,
                      replaces_product_id: Optional[str] = None, delivery_date: Optional[str] = None, note: str = "",
                      ctx: Context = None) -> dict[str, Any]:
        """SELLER tool (Valio Aimo side): propose an offer to a customer, e.g. a cheaper equivalent of a usual
        item (omit product_id) or a seasonal item (product_id). Returns a proposal with a non-binding preview
        of the customer's policy verdict. A seller can NEVER place an order: the customer's own agent accepts
        (create_quote with proposal.quote_request, then place_order) and the customer's policy decides."""
        body = ProposalRequest(customer_id=customer_id, product_id=product_id, qty_sales_units=qty_sales_units,
                               replaces_product_id=replaces_product_id,
                               delivery_date=date.fromisoformat(delivery_date[:10]) if delivery_date else None,
                               note=note)
        return _call(ctx, do_propose, body, _headers(ctx).get("x-agent-id"))

    for fn in (list_recipes, plan_recipe, check_standing_order_drift, propose_offer):
        add(fn)
    return mcp


def install(app: Any) -> Any:
    """Mount the extras router on an existing FastAPI app unless already mounted (idempotent)."""
    def mounted(routes) -> bool:
        for r in routes:
            if getattr(r, "path", None) == "/v1/recipes" or getattr(r, "original_router", None) is router:
                return True
            sub = getattr(getattr(r, "original_router", None), "routes", None)
            if sub and mounted(sub):
                return True
        return False

    if not mounted(app.router.routes):
        app.include_router(router)
    return app
