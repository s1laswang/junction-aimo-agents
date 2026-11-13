import json
import math
import re
from datetime import date
from pathlib import Path

from aimo.models import Constraint, Need, NeedItem
from aimo.parse import deliver_date, text_constraints

_DATA = json.loads((Path(__file__).resolve().parent.parent / "data_extra" / "recipes.json").read_text())
DISHES = _DATA["dishes"]
SWAPS = _DATA["swaps"]

_UNITS = {"g": ("kg", 1000), "ml": ("L", 1000), "pcs": ("pcs", 1)}
_COVERS = r"(?:guests?|people|persons?|covers?|portions?|pax|hengelle|henkilölle|henkilolle|hengen|henkeä|annosta|annos)"


def _triggered(trigger, c):
    if trigger == "lactose":
        return bool(c.lactose_free) or any(a in ("milk", "lactose") for a in c.exclude_allergens)
    return bool(c.gluten_free) or "gluten" in c.exclude_allergens


def _rule(ingredient):
    q = ingredient.lower().strip()
    for r in SWAPS:
        if any(q == m or q.endswith(" " + m) for m in r["match"]):
            return r


def recipe_swaps(ingredient, constraints):
    r = _rule(ingredient)
    if not r or not _triggered(r["trigger"], constraints):
        return []
    return [dict(o) for o in r["options"]]


def _dishes_in(text):
    t = text.lower()
    found = []
    for d in DISHES:
        for a in sorted(d["aliases"], key=len, reverse=True):
            m = re.search(r"(?:(\d+)\s*(?:x\s*|portions? of\s*|annosta\s*)?)?\b" + re.escape(a), t)
            if m:
                found.append((d, int(m.group(1)) if m.group(1) else None))
                break
    return found


def _default_covers(text):
    m = re.search(r"(\d+)\s*" + _COVERS, text.lower()) or re.search(r"\bfor\s+(\d+)", text.lower())
    return int(m.group(1)) if m else None


def menu_to_need(text, customer, deliver_by=None, today=None, stock=None, constraints=None):
    today = today or date.today()
    default = _default_covers(text)
    c = constraints or text_constraints(text)
    totals = {}
    for d, n in _dishes_in(text):
        covers = n or default
        if not covers:
            continue
        for ing in d["ingredients"]:
            q, amount, unit = ing["query"], ing["per_cover"] * covers, ing["unit"]
            swapped = False
            opts = recipe_swaps(q, c)
            if opts:
                q, amount, swapped = opts[0]["query"], amount * opts[0]["ratio"], True
            key = (q, unit, swapped)
            totals[key] = totals.get(key, 0) + amount
    items = []
    for (q, unit, swapped), amount in totals.items():
        out_unit, div = _UNITS[unit]
        qty = amount / div - (stock or {}).get(q, 0)
        if qty <= 0:
            continue
        items.append(NeedItem(
            query=q,
            quantity=math.ceil(qty * 100) / 100,
            unit=out_unit,
            constraints=c.model_copy() if swapped else Constraint(exclude_allergens=list(c.exclude_allergens)),
        ))
    return Need(customer=customer, deliver_by=deliver_by or deliver_date(text, today), items=items, raw_text=text)
