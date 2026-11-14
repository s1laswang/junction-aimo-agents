"""Recipe planner: dish + portions -> one Need per ingredient -> catalog plans -> quote lines.

Credit: dish data, alias matching, cover parsing and swap rules are Momin's (junction26-valio,
`aimo/recipes.py` `menu_to_need`, `recipe_swaps`, `_dishes_in`, `_default_covers`; data
`data_extra/recipes.json`). Adapted here to our `Need` / `Plan` contract and our catalog planner.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from aimo.models import Need, Offer, Plan

log = logging.getLogger("aimo.recipes")

_DATA = json.loads((Path(__file__).resolve().parent / "recipes.json").read_text())
DISHES: list[dict[str, Any]] = _DATA["dishes"]
SWAPS: list[dict[str, Any]] = _DATA["swaps"]
DATA_NOTE = _DATA.get("note", "")
SOURCE = "aimo.recipes (recipe data and swap rules ported from Momin's junction26-valio)"

# Momin's unit table: recipe units -> our Need units (divisor)
_UNITS = {"g": ("KG", 1000.0), "ml": ("L", 1000.0), "pcs": ("PCS", 1.0)}
# Momin's cover words (en + fi)
_COVERS = r"(?:guests?|people|persons?|covers?|portions?|servings?|pax|hengelle|henkilölle|henkilolle|hengen|henkeä|annosta|annos)"


# Category guard per ingredient query (our addition): the planner only uses offers for products whose
# Valio category code starts with one of these prefixes, so "milk" never plans a milk-system cleaner and
# "sugar" never plans sugar peas. Prefixes were read off our catalog's search results for each query
# (data/catalog.sqlite, 14 Nov). Category NAMES are not in the data set; these are codes only.
CATEGORY_HINTS: dict[str, list[str]] = {
    "bacon": ["17202"], "beef cubes": ["1872", "17209"], "butter": ["1050"], "carrot": ["1743"],
    "chicken fillet": ["1875", "17210"], "cooking cream": ["10401"], "crushed tomatoes": ["21590"],
    "cucumber": ["17402"], "dill": ["22630", "17451"], "eggs": ["10590"], "gluten-free flour mix": ["2131", "2132", "2137"],
    "gluten-free oats": ["2137", "2138"], "gluten-free pasta": ["21380"], "grated cheese": ["1060"],
    "ground beef": ["41702"], "iceberg lettuce": ["17403"], "lactose free margarine": ["1051"],
    "lactose-free butter": ["10500"], "lactose-free cheese": ["106"], "lactose-free cream": ["10401"],
    "lactose-free milk": ["10400"], "lasagne": ["21380"], "leek": ["17405", "41731"], "lingonberry jam": ["21450"],
    "macaroni": ["21380"], "meatballs": ["17300", "41705"], "milk": ["10400"], "oat cooking": ["10402", "10406"],
    "oat drink": ["10402"], "oat flakes": ["21370"], "onion": ["17405"], "pasta": ["21380"], "pepper red": ["17406"],
    "pork shoulder": ["18710"], "potato": ["17421"], "rapeseed oil": ["10530"], "rice": ["21340"],
    "salmon fillet": ["1870"], "sugar": ["22100"], "tomatoes": ["17401"], "wheat flour": ["2130", "2131"],
}


# Name words that make a product the wrong thing for the recipe line even inside the right category
# (e.g. ground beef LIVER for a lasagne). Our addition.
NAME_EXCLUDE: dict[str, list[str]] = {
    "ground beef": ["liver", "maksa", "patty", "burger", "meatball", "bolognese", "cooked"],
    "milk": ["cleaner", "jug", "powder"],
    "onion": ["mix"],
}


class RecipeError(ValueError):
    """Bad recipe request (unknown recipe, no portions...). The gateway maps it to HTTP 400/404."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ------------------------------------------------------------------ data access
def list_recipes() -> list[dict[str, Any]]:
    """Every dish with its per-cover ingredients (illustrative quantities, not Valio figures)."""
    return [{"id": d["id"], "name": d["en"], "name_fi": d.get("fi"), "aliases": d["aliases"],
             "ingredients": [{"query": i["query"], "per_portion": i["per_cover"], "unit": i["unit"]}
                             for i in d["ingredients"]],
             "note": DATA_NOTE} for d in DISHES]


def get_recipe(recipe_id: str) -> Optional[dict[str, Any]]:
    rid = (recipe_id or "").strip().lower()
    for d in DISHES:
        if d["id"] == rid or rid in [a.lower() for a in d["aliases"]] or d["en"] == rid:
            return d
    return None


def match_dishes(text: str) -> list[tuple[dict[str, Any], Optional[int]]]:
    """Momin's `_dishes_in`: dishes named in free text, with an optional per-dish count ("40 lasagne")."""
    t = (text or "").lower()
    found = []
    for d in DISHES:
        for a in sorted(d["aliases"], key=len, reverse=True):
            m = re.search(r"(?:(\d+)\s*(?:x\s*|portions? of\s*|annosta\s*)?)?\b" + re.escape(a), t)
            if m:
                found.append((d, int(m.group(1)) if m.group(1) else None))
                break
    return found


def default_portions(text: str) -> Optional[int]:
    """Momin's `_default_covers`: "for 120", "120 guests", "120 annosta"."""
    t = (text or "").lower()
    m = re.search(r"(\d+)\s*" + _COVERS, t) or re.search(r"\bfor\s+(\d+)", t)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ swaps
def _dietary(excluded: list[str], text: str, lactose_free: bool, gluten_free: bool) -> dict[str, bool]:
    t = (text or "").lower()
    return {
        "milk": "milk" in excluded,                       # milk ALLERGY: lactose-free still contains milk
        "lactose": lactose_free or "milk" in excluded or bool(re.search(r"lactose[- ]?free|laktoositon", t)),
        "gluten": gluten_free or "gluten" in excluded or bool(re.search(r"gluten[- ]?free|gluteeniton", t)),
    }


def _swap_rule(ingredient: str) -> Optional[dict[str, Any]]:
    q = ingredient.lower().strip()
    for r in SWAPS:
        if any(q == m or q.endswith(" " + m) for m in r["match"]):
            return r
    return None


def choose_swap(ingredient: str, diet: dict[str, bool]) -> Optional[dict[str, Any]]:
    """Momin's `recipe_swaps` picked options[0]. We keep his order but, for a milk ALLERGY, only accept an
    option his data marks "dairy free" (lactose-free cream/milk/butter still contain milk protein)."""
    r = _swap_rule(ingredient)
    if not r or not diet.get(r["trigger"]):
        return None
    opts = list(r["options"])
    if r["trigger"] == "lactose" and diet.get("milk"):
        opts = [o for o in opts if "dairy free" in o.get("why", "").lower()]
        if not opts:
            return {"rule": r["id"], "query": None, "ratio": 1.0,
                    "why": "no dairy-free swap in the recipe data; planned with milk excluded, a human must confirm"}
    o = opts[0]
    return {"rule": r["id"], "query": o["query"], "ratio": float(o.get("ratio", 1.0)), "why": o.get("why", "")}


# ------------------------------------------------------------------ dates and policy
def _today() -> date:
    try:
        from aimo.buyer.need import now_helsinki
        return now_helsinki().date()
    except Exception:  # noqa: BLE001
        return date.today()


def resolve_delivery(delivery_by: Any, text: str = "") -> date:
    if isinstance(delivery_by, date):
        return delivery_by
    if delivery_by:
        s = str(delivery_by).strip()
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            text = f"{s} {text}"
    try:
        from aimo.buyer.need import resolve_date
        r = resolve_date(text) if text else None
        if r:
            return r[0]
    except Exception:  # noqa: BLE001
        pass
    return _today() + timedelta(days=2)


def policy_exclusions(customer_id: str) -> list[str]:
    try:
        from aimo.gateway.verify import policy_excluded_allergens
        return list(policy_excluded_allergens(customer_id))
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------ needs
@dataclass
class IngredientNeed:
    dish: str
    ingredient: str                 # recipe ingredient as written ("cooking cream")
    query: str                      # what we search for (after a swap)
    quantity: float
    unit: str
    need: Need
    swap: Optional[dict[str, Any]] = None


@dataclass
class RecipeNeeds:
    dishes: list[dict[str, Any]]
    portions: dict[str, int]
    customer_id: str
    delivery_by: date
    excluded_allergens: list[str]
    items: list[IngredientNeed] = field(default_factory=list)


def recipe_needs(recipe: Optional[str] = None, portions: Optional[int] = None, delivery_by: Any = None,
                 customer_id: str = "hotel-aurora", *, text: str = "", excluded_allergens: Optional[list[str]] = None,
                 lactose_free: bool = False, gluten_free: bool = False,
                 on_hand: Optional[dict[str, float]] = None) -> RecipeNeeds:
    """Scale a dish (by id/alias) or every dish named in `text` to Needs, one per ingredient.

    Quantities: per_portion x portions, g -> KG and ml -> L, rounded UP to 0.01; ingredients shared by
    several dishes are summed (Momin's menu_to_need). Allergen exclusions = the customer's policy +
    `excluded_allergens` + allergens named in the text; every Need carries them so the catalog never
    plans an unsafe product (unknown is never safe).
    """
    if recipe:
        d = get_recipe(recipe)
        if d is None:
            found = match_dishes(recipe)
            if not found:
                raise RecipeError("recipe_not_found", f"unknown recipe {recipe!r}; see GET /v1/recipes", 404)
            dishes = found
        else:
            dishes = [(d, None)]
    else:
        dishes = match_dishes(text)
        if not dishes:
            raise RecipeError("recipe_not_found", "no known dish in the text; see GET /v1/recipes", 404)
    default = portions or default_portions(text) or (default_portions(recipe or "") if recipe else None)
    excl = list(excluded_allergens or [])
    try:
        from aimo.buyer.need import detect_allergens
        excl += detect_allergens(text)
    except Exception:  # noqa: BLE001
        pass
    excl = list(dict.fromkeys([*policy_exclusions(customer_id), *excl]))
    diet = _dietary(excl, text, lactose_free, gluten_free)
    when = resolve_delivery(delivery_by, text)

    totals: dict[tuple[str, str], dict[str, Any]] = {}
    used: dict[str, int] = {}
    for d, n in dishes:
        covers = n or default
        if not covers or covers <= 0:
            raise RecipeError("portions_required", f"how many portions of {d['en']}? pass portions")
        used[d["id"]] = int(covers)
        for ing in d["ingredients"]:
            q, amount, unit = ing["query"], float(ing["per_cover"]) * covers, ing["unit"]
            sw = choose_swap(q, diet)
            if sw and sw.get("query"):
                amount *= sw["ratio"]
            query = sw["query"] if sw and sw.get("query") else q
            key = (query, unit)
            t = totals.setdefault(key, {"amount": 0.0, "ingredient": q, "dishes": [], "swap": sw})
            t["amount"] += amount
            t["dishes"].append(d["id"])
    items: list[IngredientNeed] = []
    for (query, unit), t in totals.items():
        out_unit, div = _UNITS[unit]
        qty = t["amount"] / div - float((on_hand or {}).get(query, 0) or 0)
        if qty <= 0:
            continue
        qty = math.ceil(qty * 100 - 1e-9) / 100
        claims = ["FREE_FROM_LACTOSE"] if (t["swap"] and "lactose-free" in (t["swap"].get("query") or "")) else []
        need = Need(raw_text=text or f"{t['ingredient']} for {', '.join(t['dishes'])}", customer_id=customer_id,
                    product_query=query, required_claims=claims, excluded_allergens=excl, quantity=qty,
                    unit=out_unit, delivery_by=when)
        # NB: need.preferences stays empty: a non-empty dict switches off the catalog's top-hit reference spec
        items.append(IngredientNeed(dish=",".join(dict.fromkeys(t["dishes"])), ingredient=t["ingredient"],
                                    query=query, quantity=qty, unit=out_unit, need=need, swap=t["swap"]))
    return RecipeNeeds(dishes=[d for d, _ in dishes], portions=used, customer_id=customer_id, delivery_by=when,
                       excluded_allergens=excl, items=items)


# ------------------------------------------------------------------ planning
def _batched_offers(needs: list[Need], plan_fn: Callable, offers_fn: Optional[Callable]) -> tuple[dict[str, Offer], dict]:
    """Pass 1 runs the planner with a recording offers_fn (the candidate shortlist does not depend on
    offers), then ONE price + availability call for the union of all shortlists."""
    wanted: list[str] = []

    def record(ids, requested_date, customer_id=None):  # noqa: ARG001
        wanted.extend(str(i) for i in ids)
        return {}

    for n in needs:
        try:
            plan_fn(n, offers_fn=record)
        except Exception as e:  # noqa: BLE001
            log.warning("recipe pass-1 plan failed for %r: %s", n.product_query, e)
    ids = list(dict.fromkeys(wanted))
    if not ids:
        return {}, {}
    if offers_fn is not None:
        return dict(offers_fn(ids, needs[0].delivery_by, customer_id=needs[0].customer_id)), {"source": "offers_fn"}
    from aimo.backend.client import get_offers_with_meta  # raises UpstreamError -> gateway 503
    offers, meta = get_offers_with_meta(ids, needs[0].delivery_by, needs[0].customer_id)
    return offers, meta


def _plan_summary(plan: Plan, include_candidates: bool) -> dict[str, Any]:
    d = plan.model_dump(mode="json")
    if not include_candidates:
        ok = [c for c in d["candidates"] if c["tier"] != "rejected"][:3]
        d["candidates"] = [{"product_id": c["product"]["id"], "name": c["product"]["name"], "tier": c["tier"],
                            "unit_price": (c.get("offer") or {}).get("unit_price"), "reasons": c["reasons"][:2]}
                           for c in ok]
        d["candidates_rejected"] = sum(1 for c in plan.candidates if c.tier == "rejected")
    return d


def plan_recipe(recipe: Optional[str] = None, portions: Optional[int] = None, delivery_by: Any = None,
                customer_id: str = "hotel-aurora", *, text: str = "", excluded_allergens: Optional[list[str]] = None,
                lactose_free: bool = False, gluten_free: bool = False, on_hand: Optional[dict[str, float]] = None,
                include_candidates: bool = False, offers_fn: Optional[Callable] = None,
                plan_fn: Optional[Callable] = None) -> dict[str, Any]:
    """recipe (id/alias) or free text ("Lasagne for 120 on Friday") -> per-ingredient Needs and Plans,
    plus `quote_request` ready for POST /v1/quotes (or the MCP tool create_quote)."""
    rn = recipe_needs(recipe, portions, delivery_by, customer_id, text=text, excluded_allergens=excluded_allergens,
                      lactose_free=lactose_free, gluten_free=gluten_free, on_hand=on_hand)
    if plan_fn is None:
        from aimo.catalog import plan_need as plan_fn  # type: ignore[no-redef]
    needs = [it.need for it in rn.items]
    offers, meta = _batched_offers(needs, plan_fn, offers_fn)

    cats: dict[str, str] = {}
    names: dict[str, str] = {}
    try:
        from aimo.catalog.textsearch import get_rows
        for pid, r in get_rows(list(offers)).items():
            cats[pid] = str(r.get("category") or "")
            names[pid] = " ".join(str(r.get(k) or "") for k in ("name", "name_en", "name_fi")).lower()
    except Exception:  # noqa: BLE001  catalog rows unavailable: no category guard
        pass

    def from_batch_for(query: str):
        hints = CATEGORY_HINTS.get(query.lower())
        bad = NAME_EXCLUDE.get(query.lower(), [])

        def ok(i: str) -> bool:
            if hints and cats and not any(cats.get(i, "").startswith(h) for h in hints):
                return False
            return not any(w in names.get(i, "") for w in bad)

        def fn(ids, requested_date, customer_id=None):  # noqa: ARG001
            return {i: offers[i] for i in ids if i in offers and ok(i)}
        return fn

    ingredients, uncovered, swaps = [], [], []
    qty_by: dict[str, float] = {}
    total = 0.0
    for it in rn.items:
        plan = plan_fn(it.need, offers_fn=from_batch_for(it.query))
        for ln in plan.lines:
            qty_by[ln.product_id] = qty_by.get(ln.product_id, 0.0) + ln.qty_sales_units
        total += plan.total_net
        if it.swap:
            swaps.append({"ingredient": it.ingredient, "to": it.swap.get("query"), "ratio": it.swap.get("ratio"),
                          "why": it.swap.get("why"), "rule": it.swap.get("rule")})
        if plan.shortfall > 1e-9 or not plan.lines:
            uncovered.append({"ingredient": it.ingredient, "query": it.query, "shortfall": plan.shortfall,
                              "unit": it.unit, "why": plan.explanation})
        ingredients.append({"dish": it.dish, "ingredient": it.ingredient, "query": it.query, "quantity": it.quantity,
                            "unit": it.unit, "swap": it.swap, "need": it.need.model_dump(mode="json"),
                            "plan": _plan_summary(plan, include_candidates)})
    lines = [{"product_id": pid, "qty_sales_units": q} for pid, q in qty_by.items()]
    names = ", ".join(f"{d['en']} x{rn.portions[d['id']]}" for d in rn.dishes)
    covered = len(rn.items) - len(uncovered)
    summary = (f"{names} for {customer_id}, delivery by {rn.delivery_by:%a %d %b}: {len(rn.items)} ingredient(s), "
               f"{covered} fully covered, {len(lines)} product line(s), est. €{total:,.2f} net"
               + (f"; excluded allergens {', '.join(rn.excluded_allergens)}" if rn.excluded_allergens else "")
               + (f"; swaps: " + "; ".join(f"{s['ingredient']} -> {s['to'] or 'none'}" for s in swaps) if swaps else "")
               + (f"; NOT covered: " + ", ".join(u["ingredient"] for u in uncovered) if uncovered else "") + ".")
    return {
        "recipe": {"ids": [d["id"] for d in rn.dishes], "names": [d["en"] for d in rn.dishes], "portions": rn.portions},
        "customer_id": customer_id,
        "delivery_by": rn.delivery_by.isoformat(),
        "excluded_allergens": rn.excluded_allergens,
        "swaps": swaps,
        "ingredients": ingredients,
        "uncovered": uncovered,
        "total_net_estimate": round(total, 2),
        "summary": summary,
        "quote_request": {"customer_id": customer_id, "delivery_date": rn.delivery_by.isoformat(), "lines": lines},
        "provenance": {"source": SOURCE, "recipe_data_note": DATA_NOTE,
                       "credit": "Recipe data and swap logic by Momin (junction26-valio)",
                       "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "offers": {k: meta.get(k) for k in ("source", "served_from", "fetched_at", "valid_for_s", "faults")
                                  if k in meta},
                       "offer_ids_fetched": len(offers)},
    }
