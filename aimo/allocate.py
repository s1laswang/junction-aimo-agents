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


def fill(item: NeedItem, offers: list[Offer], deliver_by: date, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    remaining = item.quantity
    out: list[Offer] = []
    skipped: list[str] = []
    for o in offers:
        tag = f"{o.product.name} ({o.product_id})"
        why = None
        if remaining <= 1e-9:
            break
        if o.sales_block:
            why = f"sales blocked: {o.sales_block}"
        elif o.available_date is None:
            why = "availability date unknown"
        elif o.available_date > deliver_by:
            why = f"available {o.available_date}, after deliver_by {deliver_by}"
        elif o.closing_time and _closed(o.closing_time, now):
            why = f"order cutoff {o.closing_time} already passed"
        elif o.available_amount <= 0:
            why = "no stock"
        else:
            r = _ratio(o.product, item.unit)
            if r is None:
                why = f"cannot convert {item.unit} to {o.product.sales_unit}"
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
