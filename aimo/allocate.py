import json
import math
import os
from datetime import date, datetime, timezone
from functools import lru_cache

from .models import NeedItem, Offer, Product

_FACTORS = {
    "l": ("vol", 1.0), "ltr": ("vol", 1.0), "dl": ("vol", 0.1), "cl": ("vol", 0.01), "ml": ("vol", 0.001),
    "kg": ("mass", 1.0), "kgm": ("mass", 1.0), "g": ("mass", 0.001), "grm": ("mass", 0.001),
}


def _base(value: float, unit: str):
    f = _FACTORS.get(unit.strip().lower())
    return (f[0], value * f[1]) if f else None


@lru_cache(maxsize=1)
def _data() -> dict:
    path = os.path.join(os.environ.get("AIMO_DATA_DIR", "data"), "valio_aimo_junction26_product_data.json")
    try:
        with open(path) as fh:
            return {p["id"]: p for p in json.load(fh)}
    except OSError:
        return {}


def _data_size(product_id: str):
    p = _data().get(product_id)
    if not p:
        return None
    conv = p.get("synkkaData", {}).get("unitConversions") or []
    base = next((c for c in conv if c.get("volume") or c.get("netWeight")), None)
    per_base = next((u["sizeInBaseUnits"] for u in p.get("units", []) if u["unitId"] == p.get("salesUnit")), None)
    if not base or per_base is None:
        return None
    if base.get("volume"):
        v = _base(base["volume"]["value"], base["volume"]["unit"])
    else:
        v = _base(base["netWeight"]["value"], base["netWeight"]["unit"])
    return (v[0], v[1] * per_base) if v else None


def _ratio(product: Product, unit: str):
    u = unit.strip().lower()
    if product.sales_unit and u == product.sales_unit.strip().lower():
        return 1.0
    need = _base(1.0, unit)
    if not need:
        return None
    size = _base(product.unit_size.value, product.unit_size.unit) if product.unit_size else None
    size = size or _data_size(product.product_id)
    if not size or size[0] != need[0] or size[1] <= 0:
        return None
    return size[1] / need[1]


def units_for(product: Product, quantity: float, unit: str) -> float | None:
    r = _ratio(product, unit)
    if r is None:
        return None
    return float(math.ceil(round(quantity / r, 9)))


def _closed(closing: str, now: datetime):
    try:
        t = datetime.fromisoformat(closing)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t <= now


def _why(o: Offer, item: NeedItem, deliver_by: date, now: datetime):
    if o.sales_block:
        return f"sales blocked: {o.sales_block}", None
    if o.available_date is None:
        return "availability date unknown", None
    if o.available_date > deliver_by:
        return f"available {o.available_date}, after deliver_by {deliver_by}", None
    if o.closing_time and _closed(o.closing_time, now):
        return f"order cutoff {o.closing_time} already passed", None
    if o.available_amount <= 0:
        return "no stock", None
    r = _ratio(o.product, item.unit)
    if r is None:
        return f"cannot convert {item.unit} to {o.product.sales_unit}", None
    return None, r


def _now(now):
    now = now or datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def fill(item: NeedItem, offers: list[Offer], deliver_by: date, now: datetime | None = None):
    now = _now(now)
    remaining = item.quantity
    out: list[Offer] = []
    skipped: list[str] = []
    for o in offers:
        tag = f"{o.product.name} ({o.product_id})"
        if remaining <= 1e-9:
            break
        why, r = _why(o, item, deliver_by, now)
        if why:
            skipped.append(f"skipped {tag}: {why}")
            continue
        take = min(float(math.ceil(round(remaining / r, 9))), math.floor(o.available_amount))
        if take <= 0:
            skipped.append(f"skipped {tag}: stock below one sales unit")
            continue
        covered = take * r
        remaining -= covered
        a = o.model_copy(deep=True)
        a.units_needed = take
        a.total_net = round(take * a.unit_price_net, 2)
        k = o.total_gross / o.total_net if o.total_net else 1.0
        a.total_gross = round(a.total_net * k, 2)
        out.append(a)
    return out, max(0.0, round(remaining, 6)), skipped


def allocate(item: NeedItem, offers: list[Offer], deliver_by: date, now: datetime | None = None) -> tuple[list[Offer], float]:
    out, shortfall, skipped = fill(item, offers, deliver_by, now)
    if out:
        out[0].notes.extend(skipped)
    return out, shortfall


def _label(p: Product) -> str:
    return f"{p.product_id} {p.name[:40].strip()}".strip()


def _key(o: Offer):
    return o.product_id, o.available_date


def reallocate(item: NeedItem, allocated: list[Offer], fresh_offers: list[Offer], deliver_by: date, now: datetime | None = None) -> tuple[list[Offer], float, list[str]]:
    now = _now(now)
    fresh = {_key(o): o for o in fresh_offers}
    changes: list[str] = []
    kept: dict = {}
    used: dict = {}
    covered = 0.0
    for p in allocated:
        f = fresh.get(_key(p))
        why, r = (None, None)
        if f is None:
            why = "no longer offered"
        else:
            why, r = _why(f, item, deliver_by, now)
        if why:
            changes.append(f"{_label(p.product)}: {p.units_needed:g} -> 0 units, {why}")
            continue
        n = min(p.units_needed, math.floor(f.available_amount))
        if n < p.units_needed:
            changes.append(f"{_label(p.product)}: {p.units_needed:g} -> {n:g} units, stock dropped")
        if n <= 0:
            continue
        a = f.model_copy(deep=True)
        a.units_needed = float(n)
        a.total_net = round(n * a.unit_price_net, 2)
        a.total_gross = round(a.total_net * (f.total_gross / f.total_net if f.total_net else 1.0), 2)
        kept[_key(p)] = a
        used[_key(p)] = n
        covered += n * r
    pool = []
    for o in fresh_offers:
        left = o.available_amount - used.get(_key(o), 0)
        pool.append(o.model_copy(update={"available_amount": left}))
    rest = item.model_copy(update={"quantity": max(0.0, round(item.quantity - covered, 6))})
    new, shortfall, skipped = fill(rest, pool, deliver_by, now) if rest.quantity > 1e-9 else ([], 0.0, [])
    for n in new:
        changes.append(f"+{n.units_needed:g} from {_label(n.product)} delivered {n.available_date}")
        if _key(n) in kept:
            k = kept[_key(n)]
            k.units_needed += n.units_needed
            k.total_net = round(k.units_needed * k.unit_price_net, 2)
            k.total_gross = round(k.total_net * (n.total_gross / n.total_net if n.total_net else 1.0), 2)
        else:
            kept[_key(n)] = n
    out = list(kept.values())
    if shortfall > 0:
        changes.append(f"shortfall {shortfall:g} {item.unit}")
    return out, shortfall, changes


def options(item: NeedItem, offers: list[Offer], deliver_by: date, now: datetime | None = None) -> list[dict]:
    now = _now(now)
    alloc, short = allocate(item, offers, deliver_by, now)
    if short <= 1e-9:
        return []
    u = item.unit
    covered = round(item.quantity - short, 6)
    out = []
    if covered > 0:
        out.append({"kind": "partial", "date": deliver_by, "covered": covered, "shortfall": short,
                    "why": f"{covered:g} {u} of {item.quantity:g} {u} by {deliver_by}, {short:g} {u} short"})
    later = sorted({o.available_date for o in offers if o.available_date and o.available_date > deliver_by})
    late = next((d for d in later if allocate(item, offers, d, now)[1] <= 1e-9), None)
    if late:
        n = (late - deliver_by).days
        out.append({"kind": "late", "date": late, "days_late": n,
                    "why": f"all {item.quantity:g} {u} available by {late}, {n} days after {deliver_by}"})
    if covered > 0:
        used: dict = {}
        for a in alloc:
            used[_key(a)] = used.get(_key(a), 0) + a.units_needed
        pool = [o.model_copy(update={"available_amount": o.available_amount - used.get(_key(o), 0)})
                for o in offers if o.available_date and o.available_date > deliver_by]
        rest = item.model_copy(update={"quantity": short})
        d = next((d for d in later if fill(rest, pool, d, now)[1] <= 1e-9), None)
        if d:
            n = (d - deliver_by).days
            out.append({"kind": "split", "date": d, "on_time": covered, "later": short, "days_late": n,
                        "why": f"{covered:g} {u} by {deliver_by}, remaining {short:g} {u} on {d}, {n} days late"})
    return out


def stockout_plan(lines, fresh_offers: list[Offer], deliver_by: date, now: datetime | None = None) -> dict:
    from .models import QuoteLine
    now = _now(now)
    new_lines: list = []
    changes: list[str] = []
    opts: list[dict] = []
    shortfall = 0.0
    for l in lines:
        mine = [o for o in fresh_offers if o.product_id == l.product_id]
        unit = l.sales_unit or (mine[0].product.sales_unit if mine else None) or "ST"
        item = NeedItem(query=l.name or l.product_id, quantity=l.quantity, unit=unit)
        prev = [o.model_copy(update={"units_needed": l.quantity}) for o in mine[:1]]
        if not prev:
            changes.append(f"{l.product_id} {(l.name or '')[:40].strip()}: {l.quantity:g} -> 0 units, no longer offered")
        out, short, ch = reallocate(item, prev, fresh_offers, deliver_by, now)
        changes += ch
        if short > 1e-9:
            shortfall += short
            opts += [{**x, "product_id": l.product_id} for x in options(item, fresh_offers, deliver_by, now)]
        for o in out:
            new_lines.append(QuoteLine(product_id=o.product_id, name=o.product.name, quantity=o.units_needed,
                                       sales_unit=o.product.sales_unit, unit_price_net=o.unit_price_net,
                                       line_total_net=round(o.units_needed * o.unit_price_net, 2)))
    return {"changed": bool(changes), "lines": new_lines, "shortfall": shortfall, "changes": changes, "options": opts}
