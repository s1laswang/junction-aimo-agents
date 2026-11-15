"""Gateway extras ported from Momin's junction26-valio (with credit): recipes, standing-order drift and the
proactive seller agent. Owner: port. HTTP routes + MCP tools over the same functions.

HTTP:  GET /v1/recipes, POST /v1/recipes/plan, GET /v1/drift/{customer_id}, POST /v1/proposals,
       GET /v1/proposals?customer_id=, GET /v1/proposals/{id}, POST /v1/proposals/{id}/accept|decline
MCP:   list_recipes, plan_recipe, check_standing_order_drift, propose_offer
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import math
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from mcp.server.mcpserver import Context
except ImportError:
    Context = Any

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
    product_id: Optional[str] = Field(
        None,
        description="Product to propose (e.g. a seasonal item); omit to let the seller find a cheaper equivalent of a usual item",
    )
    qty_sales_units: Optional[float] = Field(None, gt=0)
    replaces_product_id: Optional[str] = None
    delivery_date: Optional[date] = None
    note: str = ""


class ProposalDecision(BaseModel):
    reason: str = ""


def _ident(x_agent_id: Optional[str], x_customer_id: Optional[str]) -> service.Identity:
    return service.Identity(
        agent_id=x_agent_id or service.DEFAULT_AGENT,
        customer_id=x_customer_id or service.DEFAULT_CUSTOMER,
    )


def _err(e: Exception) -> JSONResponse:
    st = getattr(e, "status", 400)
    d = {"error": getattr(e, "code", "error"), "message": getattr(e, "message", str(e))}
    if getattr(e, "detail", None) is not None:
        d["detail"] = getattr(e, "detail")
    return JSONResponse(d, status_code=st)


def _upstream(e: Exception) -> service.ServiceError:
    return service._upstream(e)


def _require_catalog() -> None:
    try:
        _cat = service._catalog()
    except Exception:
        return
    if _cat is None or not hasattr(_cat, "catalog_available"):
        return
    if not _cat.catalog_available():
        raise service.ServiceError(
            503,
            "catalog_missing",
            _cat.CATALOG_MISSING_MESSAGE,
            {"fix": "uv run aimo fetch-data", "needs": "AIMO_API_KEY"},
        )


def _screen_summary(text: str) -> tuple[dict[str, Any], list[str]]:
    from aimo.trust.screening import screen_text

    sc = screen_text(text or "", use_gemini=False)
    suspicious = bool(sc.get("suspicious"))
    reasons = list(sc.get("reasons") or [])
    layers = list(sc.get("layers") or [])
    hits = list(dict.fromkeys(sc.get("strong_reasons") or reasons))[:3]
    return {"suspicious": suspicious, "reasons": reasons, "layers": layers}, hits


def _with_injection_flags(need_dict: dict[str, Any], flags: list[str]) -> dict[str, Any]:
    need_copy = dict(need_dict)
    prefs = dict(need_copy.get("preferences") or {})
    existing = list(prefs.get("injection_flags") or [])
    prefs["injection_flags"] = list(dict.fromkeys([*existing, *flags]))
    need_copy["preferences"] = prefs
    return need_copy


def _enrich_proposal_screen(prop: dict[str, Any], raw_note: Optional[str] = None) -> tuple[dict[str, Any], bool]:
    if not isinstance(prop, dict):
        return prop, False
    existing_note_screen = prop.get("note_injection_screen")
    if raw_note is None and isinstance(existing_note_screen, dict):
        if isinstance(prop.get("injection_screen"), dict):
            return prop, False
        out = dict(prop)
        out["injection_screen"] = dict(existing_note_screen)
        return out, False

    note_text = raw_note if raw_note is not None else str(prop.get("note") or "")
    screen_info, hits = _screen_summary(note_text)
    out = dict(prop)
    out["note_injection_screen"] = screen_info
    out["injection_screen"] = dict(screen_info)
    if screen_info["suspicious"]:
        detail = f" ({', '.join(hits)})" if hits else ""
        warning = (
            f"Warning: the seller note contains instructions aimed at AI agents{detail}. "
            "They were ignored, and the customer's policy will ask a human to confirm."
        )
        out["note_warning"] = warning
        flag_vals = list(screen_info["reasons"]) or ["seller_note_injection_suspected"]
        qr = out.get("quote_request")
        if isinstance(qr, dict):
            qr_copy = dict(qr)
            if isinstance(qr_copy.get("need"), dict):
                qr_copy["need"] = _with_injection_flags(qr_copy["need"], flag_vals)
            else:
                p_info = out.get("product") if isinstance(out.get("product"), dict) else {}
                c_unit = p_info.get("content_unit") if p_info.get("content_unit") in ("L", "KG", "PCS") else "PCS"
                c_amt = float(p_info.get("content_amount") or 1.0)
                q_su = float(out.get("qty_sales_units") or 1.0)
                qr_copy["need"] = {
                    "customer_id": str(out.get("customer_id") or service.DEFAULT_CUSTOMER),
                    "product_query": str(p_info.get("name") or p_info.get("product_id") or "proposed item")[:500],
                    "quantity": max(0.01, round(q_su * (c_amt if c_amt > 0 else 1.0), 4)),
                    "unit": c_unit,
                    "delivery_by": str(qr_copy.get("delivery_date") or out.get("delivery_date") or date.today().isoformat()),
                    "raw_text": note_text[:4000],
                    "preferences": {"injection_flags": flag_vals},
                }
            out["quote_request"] = qr_copy
        pv = out.get("policy_preview")
        if isinstance(pv, dict) and pv.get("outcome") not in ("reject", "needs_approval"):
            pv_copy = dict(pv)
            pv_copy["outcome"] = "needs_approval"
            pv_hits = list(pv_copy.get("hits") or [])
            pv_hits.append({"rule_id": "injection", "outcome": "needs_approval", "message": warning})
            pv_copy["hits"] = pv_hits
            out["policy_preview"] = pv_copy
    return out, True


def _persist_proposal_if_complete(prop: dict[str, Any]) -> None:
    if not isinstance(prop, dict):
        return
    if not all(prop.get(k) for k in ("proposal_id", "customer_id", "status", "created_at")):
        return
    try:
        from aimo.agents import seller
        from aimo.backend import orders as store

        lock = getattr(store, "_lock", None)
        if lock is not None:
            with lock:
                seller._save(prop)
        else:
            seller._save(prop)
    except Exception:
        return


def do_plan_recipe(body: RecipePlanRequest, ident: service.Identity) -> dict[str, Any]:
    from aimo.backend.client import UpstreamError
    from aimo.recipes import RecipeError, plan_recipe

    aid = auth._clean_agent_id(ident.agent_id)
    if aid and auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    bcid = auth._clean_customer_id(body.customer_id)
    if bcid and auth._is_unsafe_customer_id(bcid):
        auth.require_known_customer(bcid)
    if getattr(ident, "authenticated", {}).get("customer") and bcid:
        who = service.claim_identity(ident, bcid, None)
    elif bcid:
        who = service.Identity(agent_id=aid or service.DEFAULT_AGENT, customer_id=bcid)
    else:
        who = ident
    cid = auth.require_known_customer(who.customer_id)
    if getattr(plan_recipe, "__module__", "") == "aimo.recipes.planner":
        try:
            _require_catalog()
        except service.ServiceError:
            from aimo.recipes.planner import recipe_needs

            try:
                recipe_needs(
                    body.recipe_id,
                    body.portions,
                    body.delivery_by,
                    cid,
                    text=body.text,
                    excluded_allergens=body.excluded_allergens,
                    lactose_free=body.lactose_free,
                    gluten_free=body.gluten_free,
                )
            except RecipeError as e:
                raise service.ServiceError(e.status, e.code, e.message) from e
            raise
    try:
        res = plan_recipe(
            body.recipe_id,
            body.portions,
            body.delivery_by,
            cid,
            text=body.text,
            excluded_allergens=body.excluded_allergens,
            lactose_free=body.lactose_free,
            gluten_free=body.gluten_free,
            include_candidates=body.include_candidates,
        )
    except RecipeError as e:
        raise service.ServiceError(e.status, e.code, e.message) from e
    except UpstreamError as e:
        raise _upstream(e) from e

    screen_info, hits = _screen_summary(body.text or "")
    suspicious = screen_info["suspicious"]
    reasons = screen_info["reasons"]
    out = dict(res)
    out["injection_screen"] = screen_info
    if suspicious:
        detail = f" ({', '.join(hits)})" if hits else ""
        warning = (
            f"Warning: the request contains instructions aimed at AI agents{detail}. "
            "They were ignored, and the customer's policy will ask a human to confirm."
        )
        out["request_warning"] = warning
        if isinstance(out.get("explanation"), str):
            existing = out["explanation"]
            out["explanation"] = f"{warning} {existing}".strip() if existing else warning
        flag_vals = list(reasons) or ["request_text_injection_suspected"]
        qr = out.get("quote_request")
        if isinstance(qr, dict) and isinstance(qr.get("need"), dict):
            out["quote_request"] = {**qr, "need": _with_injection_flags(qr["need"], flag_vals)}
        for ing in out.get("ingredients") or []:
            if isinstance(ing, dict) and isinstance(ing.get("need"), dict):
                ing["need"] = _with_injection_flags(ing["need"], flag_vals)
    for ing in out.get("ingredients") or []:
        plan_obj = ing.get("plan") if isinstance(ing, dict) else None
        if isinstance(plan_obj, dict):
            for ln in plan_obj.get("lines") or []:
                if isinstance(ln, dict) and not isinstance(ln.get("injection_screen"), dict):
                    flagged = "supplier_text_injection_suspected" in (ln.get("flags") or [])
                    ln["injection_screen"] = {
                        "suspicious": flagged,
                        "reasons": ["supplier_text_injection_suspected"] if flagged else [],
                        "layers": ["regex"],
                    }
    return out


def do_drift(customer_id: str, rebaseline: bool = False) -> dict[str, Any]:
    from aimo.agents.drift import SEEDS, check_drift
    from aimo.backend.client import UpstreamError

    cid = auth._clean_customer_id(customer_id)
    if cid not in SEEDS:
        preview = auth._safe_customer_preview(cid)
        raise service.ServiceError(
            404,
            "no_standing_order",
            f"no standing order on file for {preview!r}",
            {"customers": list(SEEDS)},
        )
    if getattr(check_drift, "__module__", "") == "aimo.agents.drift":
        _require_catalog()
    try:
        return check_drift(cid, rebaseline=rebaseline)
    except UpstreamError as e:
        raise _upstream(e) from e


def do_propose(body: ProposalRequest, agent_id: Optional[str]) -> dict[str, Any]:
    from aimo.agents.seller import ProposalError, propose_offer
    from aimo.backend.client import UpstreamError

    aid = auth._clean_agent_id(agent_id)
    if aid and auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    cid = auth.require_known_customer(body.customer_id)
    if body.qty_sales_units is not None and (
        not math.isfinite(body.qty_sales_units) or body.qty_sales_units <= 0 or body.qty_sales_units > 1_000_000
    ):
        raise service.ServiceError(422, "bad_quantity", "qty_sales_units must be finite, > 0, and <= 1,000,000")
    if body.product_id is not None:
        pid_clean = body.product_id.strip()
        if not pid_clean or len(pid_clean) > 64 or "\x00" in pid_clean:
            raise service.ServiceError(
                404,
                "product_not_found",
                f"product {auth._safe_customer_preview(pid_clean)!r} not in the catalog",
            )
    if getattr(propose_offer, "__module__", "") == "aimo.agents.seller":
        _require_catalog()
    try:
        prop = propose_offer(
            cid,
            product_id=body.product_id,
            qty_sales_units=body.qty_sales_units,
            replaces_product_id=body.replaces_product_id,
            delivery_date=body.delivery_date,
            note=body.note,
            agent_id=aid or None,
        )
    except ProposalError as e:
        raise service.ServiceError(e.status, e.code, e.message, e.detail) from e
    except UpstreamError as e:
        raise _upstream(e) from e

    enriched, changed = _enrich_proposal_screen(prop, body.note)
    if changed:
        _persist_proposal_if_complete(enriched)
    return enriched


def _svc(fn, *a, **kw):
    from aimo.agents.seller import ProposalError

    try:
        return fn(*a, **kw)
    except ProposalError as e:
        raise service.ServiceError(e.status, e.code, e.message, e.detail) from e


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
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Checks the customer's standing order (seeded 8-week history) against live offers for the next delivery:
    `flags[] {product_id, kind: price|volume|availability|allergen, change_pct, message, route}` in plain
    language. Price baseline = first live price this gateway observed. Honours `X-Aimo-Faults`."""
    aid = auth._clean_agent_id(x_agent_id)
    if aid and auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    raw_xcid = auth._clean_customer_id(x_customer_id)
    if rebaseline:
        if not raw_xcid:
            raise service.ServiceError(401, "identity_required", "Rebaselining standing-order drift requires X-Customer-Id.")
        xcid = auth.require_known_customer(raw_xcid)
        cid = auth.require_known_customer(customer_id)
        if xcid != cid:
            raise service.ServiceError(
                403,
                "customer_mismatch",
                f"request names customer {auth._safe_customer_preview(cid)!r} but the caller is authenticated as {xcid!r}",
            )
        auth.check_agent_key(cid, x_agent_key)
        return do_drift(cid, rebaseline=True)
    if raw_xcid:
        xcid = auth.require_known_customer(raw_xcid)
        auth.check_agent_key(xcid, x_agent_key)
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

    aid = auth._clean_agent_id(x_agent_id)
    if not aid:
        raise service.ServiceError(401, "identity_required", "POST /v1/proposals requires X-Agent-Id (valio-aimo/<agent>).")
    if auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    if not is_seller(aid) or not aid.split("/", 1)[1].strip():
        raise service.ServiceError(403, "seller_required", "Only a seller agent (valio-aimo/*) may create proposals.")
    auth.require_known_customer(body.customer_id)
    return do_propose(body, aid)


@router.get("/v1/proposals", tags=[TAG_PROPOSALS], summary="List proposals")
def proposals(
    customer_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="proposed | accepted | declined | all"),
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> list[dict[str, Any]]:
    from aimo.agents.seller import list_proposals

    aid = auth._clean_agent_id(x_agent_id)
    if aid and auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    raw_xcid = auth._clean_customer_id(x_customer_id)
    raw_qcid = auth._clean_customer_id(customer_id)
    xcid = auth.require_known_customer(raw_xcid) if raw_xcid else None
    if xcid:
        auth.check_agent_key(xcid, x_agent_key)
    qcid = auth.require_known_customer(raw_qcid) if raw_qcid else None
    if xcid and qcid and xcid != qcid:
        raise service.ServiceError(
            403,
            "customer_mismatch",
            f"request names customer {qcid!r} but the caller is authenticated as {xcid!r}",
        )
    items = list_proposals(qcid or xcid, status)
    return [_enrich_proposal_screen(p)[0] for p in items]


@router.get("/v1/proposals/{proposal_id}", tags=[TAG_PROPOSALS], summary="Get a proposal")
def proposal(
    proposal_id: str,
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    from aimo.agents.seller import get_proposal

    aid = auth._clean_agent_id(x_agent_id)
    if aid and auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    raw_xcid = auth._clean_customer_id(x_customer_id)
    xcid = auth.require_known_customer(raw_xcid) if raw_xcid else None
    if xcid:
        auth.check_agent_key(xcid, x_agent_key)
    prop = _svc(get_proposal, proposal_id)
    if xcid and prop.get("customer_id") != xcid:
        raise service.ServiceError(
            403,
            "customer_mismatch",
            f"this proposal was made to another customer (authenticated as {xcid!r})",
        )
    return _enrich_proposal_screen(prop)[0]


def _accept_suspicious_proposal(proposal_id: str, prop: dict[str, Any], identity: service.Identity) -> dict[str, Any]:
    from aimo.models import Need

    if identity.customer_id != prop.get("customer_id"):
        raise service.ServiceError(403, "customer_mismatch", "this proposal was made to another customer")
    if prop.get("status") != "proposed":
        raise service.ServiceError(409, "not_open", f"proposal is {prop.get('status')}")
    qr = prop["quote_request"]
    need_obj = Need.model_validate(qr["need"]) if isinstance(qr.get("need"), dict) else None
    quote = service.make_quote(identity, date.fromisoformat(qr["delivery_date"]), qr["lines"], need_obj)
    order = service.submit_order(identity, quote.quote_id, quote.quote_hash or "")
    updated = dict(prop)
    updated.update(
        status="accepted",
        accepted_by=f"agent:{identity.full_agent_id}",
        quote_id=quote.quote_id,
        order_id=order.order_id,
        order_status=order.status,
        decision=order.decision.model_dump(mode="json"),
        accepted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    _persist_proposal_if_complete(updated)
    try:
        service.audit(
            identity.actor,
            "proposal.accepted",
            proposal_id,
            f"{identity.full_agent_id} accepted proposal {proposal_id} from {updated.get('seller_id')}: "
            f"quote {quote.quote_id}, order {order.order_id} is {order.status} under {updated.get('customer_id')}'s policy.",
            {"quote_id": quote.quote_id, "order_id": order.order_id, "status": order.status},
            related_ids=[quote.quote_id, order.order_id],
        )
    except Exception:
        pass
    return {"proposal": updated, "quote": quote.model_dump(mode="json"), "order": order.model_dump(mode="json")}


@router.post("/v1/proposals/{proposal_id}/accept", tags=[TAG_PROPOSALS], summary="Customer's agent accepts")
def accept(
    proposal_id: str,
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Customer side only (403 `seller_cannot_order` for valio-aimo/* agents). Creates a signed quote under the
    buyer's identity and places the order: the customer's policy decides (placed / pending_approval / rejected)."""
    from aimo.agents.seller import ProposalError, accept_proposal, get_proposal, is_seller

    aid = auth._clean_agent_id(x_agent_id)
    raw_cid = auth._clean_customer_id(x_customer_id)
    if not aid or not raw_cid:
        raise service.ServiceError(401, "identity_required", "Accepting a proposal requires X-Customer-Id and X-Agent-Id.")
    if auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    if is_seller(aid):
        try:
            return _svc(accept_proposal, proposal_id, _ident(aid, raw_cid))
        except service.ServiceError as e:
            if e.code == "proposal_not_found":
                try:
                    service.audit(
                        f"agent:{aid}",
                        "proposal.accept_refused",
                        proposal_id,
                        f"Seller agent {aid} tried to accept proposal {proposal_id}; refused: a seller can never order on a customer's behalf.",
                        {"customer_id": auth._safe_customer_preview(raw_cid)},
                    )
                except Exception:
                    pass
                raise service.ServiceError(
                    403,
                    "seller_cannot_order",
                    "a seller agent can only propose; the customer's own agent must accept and the customer's policy decides",
                ) from e
            raise
    cid = auth.require_known_customer(raw_cid)
    auth.check_agent_key(cid, x_agent_key)
    ident_obj = _ident(aid, cid)
    try:
        raw_prop = get_proposal(proposal_id)
    except ProposalError as e:
        raise service.ServiceError(e.status, e.code, e.message, e.detail) from e
    enriched, _ = _enrich_proposal_screen(raw_prop)
    if (enriched.get("note_injection_screen") or {}).get("suspicious"):
        return _accept_suspicious_proposal(proposal_id, enriched, ident_obj)
    out = _svc(accept_proposal, proposal_id, ident_obj)
    if isinstance(out, dict) and isinstance(out.get("proposal"), dict):
        out["proposal"] = _enrich_proposal_screen(out["proposal"])[0]
    return out


@router.post("/v1/proposals/{proposal_id}/decline", tags=[TAG_PROPOSALS], summary="Customer's agent declines")
def decline(
    proposal_id: str,
    body: ProposalDecision = Body(default_factory=ProposalDecision),
    x_agent_id: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
) -> dict[str, Any]:
    from aimo.agents.seller import decline_proposal, is_seller

    aid = auth._clean_agent_id(x_agent_id)
    raw_cid = auth._clean_customer_id(x_customer_id)
    if not aid or not raw_cid:
        raise service.ServiceError(401, "identity_required", "Declining a proposal requires X-Customer-Id and X-Agent-Id.")
    if auth._is_unsafe_agent_id(aid):
        raise service.ServiceError(403, "invalid_agent_id", f"invalid agent id {auth._safe_customer_preview(aid)!r}")
    if is_seller(aid):
        raise service.ServiceError(
            403,
            "seller_cannot_order",
            "a seller agent can only propose; the customer's own agent must decide",
        )
    cid = auth.require_known_customer(raw_cid)
    auth.check_agent_key(cid, x_agent_key)
    out = _svc(decline_proposal, proposal_id, _ident(aid, cid), body.reason)
    return _enrich_proposal_screen(out)[0]


EXTRA_TOOLS = ("list_recipes", "plan_recipe", "check_standing_order_drift", "propose_offer")


def register_mcp_tools(mcp: Any) -> Any:
    """Add the extra tools to an MCPServer (skips tools that are already registered)."""
    from aimo.gateway.mcp_server import _call, _header_or_env, _headers, _identity

    try:
        have = {t.name for t in mcp._tool_manager.list_tools()}
    except Exception:
        have = set()

    def add(fn):
        if fn.__name__ not in have:
            mcp.tool()(fn)

    def list_recipes(ctx: Context = None) -> dict[str, Any]:
        """Dishes the recipe planner knows (lasagne, salmon soup, pancakes, ...) with per-portion ingredient
        quantities (illustrative, not Valio figures). Use plan_recipe to turn one into quote lines."""
        from aimo.recipes import list_recipes as lr

        return {"recipes": lr()}

    def plan_recipe(
        recipe_id: Optional[str] = None,
        portions: Optional[int] = None,
        delivery_by: Optional[str] = None,
        text: str = "",
        excluded_allergens: Optional[list[str]] = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Menu -> recipe -> order. Scale a dish to `portions` (or parse free text like "Lasagne for 120 on
        Friday"), apply the customer's allergen swaps, plan every ingredient against the catalog with live
        offers, and return per-ingredient plans, what is not covered, and `quote_request` = {delivery_date,
        lines} to pass to create_quote. Then place_order; the customer's policy decides."""
        ident = _identity(ctx)
        raw_aid = auth._clean_agent_id(_header_or_env(_headers(ctx), "x-agent-id", "AIMO_AGENT_ID"))

        def _run() -> dict[str, Any]:
            if raw_aid and auth._is_unsafe_agent_id(raw_aid):
                raise service.ServiceError(
                    403,
                    "invalid_agent_id",
                    f"invalid agent id {auth._safe_customer_preview(raw_aid)!r}",
                )
            body = RecipePlanRequest(
                recipe_id=recipe_id,
                portions=portions,
                delivery_by=delivery_by,
                text=text,
                excluded_allergens=excluded_allergens or [],
            )
            return do_plan_recipe(body, ident)

        return _call(ctx, _run)

    def check_standing_order_drift(customer_id: Optional[str] = None, ctx: Context = None) -> dict[str, Any]:
        """Check the customer's standing order for drift before the next delivery: price creep (vs the first
        price we observed), volume creep over the 8-week history, items that cannot be delivered, allergen
        conflicts. Returns flags with plain-language messages and route auto|approval."""
        ident = _identity(ctx)
        raw_aid = auth._clean_agent_id(_header_or_env(_headers(ctx), "x-agent-id", "AIMO_AGENT_ID"))

        def _run() -> dict[str, Any]:
            if raw_aid and auth._is_unsafe_agent_id(raw_aid):
                raise service.ServiceError(
                    403,
                    "invalid_agent_id",
                    f"invalid agent id {auth._safe_customer_preview(raw_aid)!r}",
                )
            qcid = auth._clean_customer_id(customer_id)
            if qcid:
                target = auth.require_known_customer(qcid)
                if getattr(ident, "authenticated", {}).get("customer") and target != ident.customer_id:
                    service.claim_identity(ident, target, None)
                return do_drift(target)
            return do_drift(ident.customer_id)

        return _call(ctx, _run)

    def propose_offer(
        customer_id: str,
        product_id: Optional[str] = None,
        qty_sales_units: Optional[float] = None,
        replaces_product_id: Optional[str] = None,
        delivery_date: Optional[str] = None,
        note: str = "",
        ctx: Context = None,
    ) -> dict[str, Any]:
        """SELLER tool (Valio Aimo side): propose an offer to a customer, e.g. a cheaper equivalent of a usual
        item (omit product_id) or a seasonal item (product_id). Returns a proposal with a non-binding preview
        of the customer's policy verdict. A seller can NEVER place an order: the customer's own agent accepts
        (create_quote with proposal.quote_request, then place_order) and the customer's policy decides."""
        raw_aid = _header_or_env(_headers(ctx), "x-agent-id", "AIMO_AGENT_ID")

        def _run() -> dict[str, Any]:
            parsed_day: Optional[date] = None
            if delivery_date:
                try:
                    parsed_day = date.fromisoformat(str(delivery_date).strip()[:10])
                except ValueError as e:
                    raise service.ServiceError(
                        422,
                        "invalid_delivery_date",
                        f"invalid delivery_date {auth._safe_customer_preview(str(delivery_date))!r}; expected YYYY-MM-DD",
                    ) from e
            body = ProposalRequest(
                customer_id=customer_id,
                product_id=product_id,
                qty_sales_units=qty_sales_units,
                replaces_product_id=replaces_product_id,
                delivery_date=parsed_day,
                note=note,
            )
            return do_propose(body, raw_aid)

        return _call(ctx, _run)

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
