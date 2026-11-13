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
            changes.append(f"{p.product_id}: {p.units_needed:g} -> 0 units, {why}")
            continue
        n = min(p.units_needed, math.floor(f.available_amount))
        if n < p.units_needed:
            changes.append(f"{p.product_id}: {p.units_needed:g} -> {n:g} units, stock dropped")
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
        changes.append(f"+{n.units_needed:g} from {n.product_id} delivered {n.available_date}")
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
