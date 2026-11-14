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
_DAIRY_EXCL = {"milk", "dairy", "maito"}
_LACTOSE_EXCL = _DAIRY_EXCL | {"lactose", "laktoosi"}
_GLUTEN_EXCL = {"gluten", "wheat", "barley", "rye", "oats", "gluteeni", "vehnä"}


def _excl(c):
    return {a.strip().lower() for a in c.exclude_allergens} if c else set()


def _triggered(trigger, c):
    excl = _excl(c)
    if trigger == "lactose":
        return bool(c and c.lactose_free) or bool(excl & _LACTOSE_EXCL)
    if trigger == "gluten":
        return bool(c and c.gluten_free) or bool(excl & _GLUTEN_EXCL)
    return False


def _rule(ingredient):
    q = ingredient.lower().strip()
    for r in SWAPS:
        if any(q == m or q.endswith(" " + m) for m in r["match"]):
            return r


def recipe_swaps(ingredient, constraints):
    r = _rule(ingredient)
    if not r or not _triggered(r["trigger"], constraints):
        return []
    opts = [dict(o) for o in r["options"]]
    if r["trigger"] == "lactose" and (_excl(constraints) & _DAIRY_EXCL):
        opts = [o for o in opts if "dairy free" in o["why"]] or opts
    return opts


def _dishes_in(text):
    t = text.lower()
    pairs = sorted(
        ((a, i, d) for i, d in enumerate(DISHES) for a in {*d["aliases"], d["en"], d["fi"]}),
        key=lambda x: (-len(x[0]), x[1]),
    )
    spans, picked = [], {}
    for a, i, d in pairs:
        if i in picked:
            continue
        for m in re.finditer(r"(?:(\d+)\s*(?:x\s*|portions? of\s*|annosta\s*)?)?\b" + re.escape(a), t):
            s, e = m.end() - len(a), m.end()
            if any(s < pe and e > ps for ps, pe in spans):
                continue
            spans.append((m.start(), e))
            picked[i] = (d, int(m.group(1)) if m.group(1) else None)
            break
    return [picked[i] for i in sorted(picked)]


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
            quantity=math.ceil(round(qty * 100, 9)) / 100,
            unit=out_unit,
            constraints=c.model_copy() if swapped else Constraint(exclude_allergens=list(c.exclude_allergens)),
        ))
    return Need(customer=customer, deliver_by=deliver_by or deliver_date(text, today), items=items, raw_text=text)
