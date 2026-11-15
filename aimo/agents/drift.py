"""Standing-order drift detector. Owner: port.

Ported from Momin's junction26-valio `agents/drift.py` (thresholds PRICE_PCT / AUTO_PRICE_PCT / CREEP_PCT,
the seeded 8-week standing-order histories, the "creep without a decision" rule and the auto/approval
routing), with credit. What changed for our contract:
  - customers are ours (hotel-aurora, school-tampere, hospital-meilahti). His seed product ids are reused where
    they exist in our catalog and fit the customer's allergen policy; his school milk line moved to the hotel
    (our school is milk-free) and his hospital oat flakes (contain gluten) became gluten-free oat muesli;
  - offers come in-process from our backend (`get_offers_with_meta`, live mock API, `X-Aimo-Faults` honoured);
  - the price baseline is the first live price we observed, stored in our state DB (table `port_drift_baseline`),
    instead of his var/drift.json;
  - output is `[{product_id, kind: price|volume|availability|allergen, change_pct, message, ...}]` in plain
    language for a procurement manager, and each check is audit-logged.

The weekly quantities are SEEDED DEMO HISTORY (as in Momin's code), not real customer orders.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("aimo.agents.drift")

ACTOR = "agent:valio-aimo/drift-watch-1"
PRICE_PCT = 5.0        # Momin: flag price moves of 5 % or more
AUTO_PRICE_PCT = 10.0  # Momin: rises up to 10 % may be re-quoted automatically (within the policy)
CREEP_PCT = 20.0       # Momin: a steady rise of 20 % or more over the history is "creep"

# Momin's SEEDS, mapped onto our customers (weekday: 0 = Monday). Ids verified in data/catalog.sqlite.
SEEDS: dict[str, dict[str, Any]] = {
    "hotel-aurora": {
        "weekday": 3,
        "lines": [
            {"product_id": "460396", "label": "Milk", "name": "Valio skimmed milk 10 l novobox", "unit": "CS",
             "weeks": [8, 8, 8, 9, 9, 10, 11, 12]},
            {"product_id": "463982", "label": "Eggs", "name": "Kieku Barn Eggs L180 12300g", "unit": "KI",
             "weeks": [4, 4, 4, 4, 4, 4, 4, 4]},
            {"product_id": "460468", "label": "Coffee", "name": "Paulig Presidentti Tumma Paahto 18x300g", "unit": "KI",
             "weeks": [3, 3, 3, 3, 3, 3, 3, 3]},
            {"product_id": "460884", "label": "Croissants", "name": "Reuter & Stolt Croissant butter 18 % 80 x 90g",
             "unit": "KI", "weeks": [5, 5, 5, 5, 6, 6, 7, 8]},
        ],
    },
    "school-tampere": {
        "weekday": 1,
        "lines": [
            {"product_id": "470863", "label": "Oat drink", "name": "Oatly Oat Drink Semi 1L", "unit": "ST",
             "weeks": [320, 320, 320, 330, 340, 360, 380, 400]},
            {"product_id": "461578", "label": "Rye bread", "name": "Vaasan Rye bread 45x85g", "unit": "CS",
             "weeks": [6, 6, 6, 6, 6, 6, 6, 6]},
            {"product_id": "460573", "label": "Potatoes", "name": "PP POTATO PRE-WASHED BELLAROSA", "unit": "KG",
             "weeks": [120, 120, 120, 120, 120, 120, 120, 120]},
        ],
    },
    "hospital-meilahti": {
        "weekday": 0,
        "lines": [
            {"product_id": "462775", "label": "Lactose-free milk", "name": "Arla Milkdrink 1L lactose free fat free UHT",
             "unit": "ST", "weeks": [60, 60, 60, 62, 64, 66, 70, 74]},
            {"product_id": "461765", "label": "Gluten-free bread", "name": "Moilas Gluten-free Dark Bread 600g sliced frozen",
             "unit": "KI", "weeks": [10, 10, 10, 10, 10, 10, 10, 10]},
            {"product_id": "469269", "label": "Gluten-free oat muesli", "name": "Urtekram Organic Gluten-free Oat Muesli 550g",
             "unit": "ST", "weeks": [20, 20, 20, 20, 20, 20, 20, 20]},
        ],
    },
}

# Plain-language unit words (sales unit codes as used in the product data).
UNIT_WORDS = {"CS": "cases", "KI": "boxes", "ST": "units", "KG": "kg", "L": "litres", "BAG": "bags", "PAK": "packs"}
UNIT_WORD = {"CS": "case", "KI": "box", "ST": "unit", "KG": "kg", "L": "litre", "BAG": "bag", "PAK": "pack"}

_SCHEMA = """create table if not exists port_drift_baseline (
  customer_id text not null, product_id text not null, unit_price real not null, observed_at text not null,
  primary key (customer_id, product_id))"""


def _finite_float(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        f = float(val)
    except (ValueError, TypeError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _finite_pos(val: Any) -> Optional[float]:
    f = _finite_float(val)
    return f if f is not None and f > 0 else None


def _conn():
    from aimo.backend import orders as store
    with store._lock:
        c = store.conn()
        c.execute(_SCHEMA)
        return c


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_delivery(weekday: int, today: Optional[date] = None) -> date:
    """Momin's next_delivery: the next such weekday strictly after today."""
    if isinstance(weekday, bool) or not isinstance(weekday, int) or weekday < 0 or weekday > 6:
        raise ValueError(f"weekday must be an integer 0..6 (got {weekday!r})")
    if today is None:
        from aimo.backend import clock
        today = clock.today_local()
    elif isinstance(today, bool):
        raise ValueError(f"today must be a date (got {today!r})")
    elif isinstance(today, datetime):
        today = today.date()
    elif not isinstance(today, date):
        raise ValueError(f"today must be a date (got {today!r})")
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


def standing_order(customer_id: str, today: Optional[date] = None) -> dict[str, Any]:
    if isinstance(customer_id, bool) or not isinstance(customer_id, str):
        raise KeyError(customer_id)
    seed = SEEDS.get(customer_id)
    if seed is None:
        raise KeyError(customer_id)
    delivery = next_delivery(seed["weekday"], today)
    return {"customer_id": customer_id, "delivery_date": delivery.isoformat(),
            "history": "seeded demo history (8 weeks), not real customer orders",
            "lines": [{"product_id": l["product_id"], "label": l["label"], "name": l["name"], "unit": l["unit"],
                       "quantity": l["weeks"][-1], "weeks": list(l["weeks"])} for l in seed["lines"]]}


def _baseline(customer_id: str) -> dict[str, tuple[float, str]]:
    if isinstance(customer_id, bool) or not isinstance(customer_id, str) or not customer_id:
        return {}
    from aimo.backend import orders as store
    with store._lock:
        rows = _conn().execute("select product_id, unit_price, observed_at from port_drift_baseline where customer_id=?",
                               (customer_id,)).fetchall()
    out: dict[str, tuple[float, str]] = {}
    for r in rows:
        fp = _finite_pos(r[1])
        if fp is not None and isinstance(r[0], str):
            out[r[0]] = (fp, str(r[2] or ""))
    return out


def _save_baseline(customer_id: str, prices: dict[str, float], replace: bool) -> None:
    if isinstance(customer_id, bool) or not isinstance(customer_id, str):
        return
    cid = customer_id.strip()
    if not cid or "\x00" in cid or len(cid) > 128 or not isinstance(prices, dict):
        return
    valid: list[tuple[str, float]] = []
    for pid, p in prices.items():
        if isinstance(pid, bool) or not isinstance(pid, str):
            continue
        clean_pid = pid.strip()
        if not clean_pid or "\x00" in clean_pid or len(clean_pid) > 64:
            continue
        fp = _finite_pos(p)
        if fp is None:
            continue
        valid.append((clean_pid, fp))
    if not valid:
        return
    from aimo.backend import orders as store
    ts = _now().isoformat(timespec="seconds")
    verb = "insert or replace" if replace else "insert or ignore"
    with store._lock:
        c = _conn()
        for clean_pid, fp in valid:
            c.execute(f"{verb} into port_drift_baseline values (?,?,?,?)", (cid, clean_pid, fp, ts))


def _excluded(customer_id: str) -> list[str]:
    try:
        from aimo.gateway.verify import policy_excluded_allergens
        return list(policy_excluded_allergens(customer_id))
    except Exception:
        return []


def _allergen_conflicts(pid: str, excluded: list[str]) -> list[str]:
    if not excluded:
        return []
    try:
        from aimo.catalog import get_product
        p = get_product(pid)
    except Exception:
        return []
    if p is None:
        return []
    out = []
    for g in excluded:
        st = p.allergens.get(g)
        v = getattr(st, "value", st)
        if v in ("contains", "may_contain"):
            out.append(f"{g} ({v.replace('_', ' ')})")
    return out


def volume_flag(line: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Momin's qty_creep: a non-decreasing history that grew by CREEP_PCT or more, with no decision on record."""
    if not isinstance(line, dict):
        return None
    w = line.get("weeks")
    if not isinstance(w, (list, tuple)) or len(w) < 2:
        return None
    nums: list[float] = []
    for x in w:
        fx = _finite_float(x)
        if fx is None:
            return None
        nums.append(fx)
    if nums[0] <= 0 or nums != sorted(nums):
        return None
    raw_pct = (nums[-1] - nums[0]) / nums[0] * 100.0
    if not math.isfinite(raw_pct):
        return None
    pct = round(raw_pct, 1)
    if pct < CREEP_PCT:
        return None
    unit_code = str(line.get("unit") or "")
    unit = UNIT_WORDS.get(unit_code, unit_code)
    label = str(line.get("label") or line.get("product_id") or "")
    return {"product_id": line.get("product_id"), "kind": "volume", "change_pct": pct,
            "before": w[0], "after": w[-1], "route": "approval",
            "message": f"{label} volume crept up {pct:.0f}% over {len(w)} weeks ({nums[0]:g} → {nums[-1]:g} {unit}). "
                       f"Nobody decided this; confirm {nums[-1]:g} {unit} a week or go back to {nums[0]:g}.",
            "fix": {"action": "confirm_qty", "quantity": w[0]}}


def price_flag(line: dict[str, Any], old: float, since: str, new: float) -> Optional[dict[str, Any]]:
    if not isinstance(line, dict):
        return None
    old_f = _finite_pos(old)
    new_f = _finite_pos(new)
    if old_f is None or new_f is None:
        return None
    raw_pct = (new_f - old_f) / old_f * 100.0
    if not math.isfinite(raw_pct):
        return None
    pct = round(raw_pct, 1)
    if abs(pct) < PRICE_PCT:
        return None
    unit_code = str(line.get("unit") or "")
    unit = UNIT_WORD.get(unit_code, unit_code)
    label = str(line.get("label") or line.get("product_id") or "")
    route = "auto" if pct < 0 or pct <= AUTO_PRICE_PCT else "approval"
    verb = "rose" if pct > 0 else "fell"
    day = str(since)[:10] if isinstance(since, str) and since else "baseline"
    tail = (" Within the 10% re-quote band: the agent may re-quote at the new price (policy still applies)."
            if route == "auto" and pct > 0 else
            " Cheaper now: no action needed." if pct < 0 else
            " Above the 10% band: a person should approve the new price or ask for an alternative.")
    return {"product_id": line.get("product_id"), "kind": "price", "change_pct": pct, "before": old_f, "after": new_f,
            "route": route,
            "message": f"{label} price {verb} {abs(pct):.0f}% since {day} (€{old_f:.2f} → €{new_f:.2f} per {unit})." + tail,
            "fix": {"action": "requote", "price": new_f}}


def availability_flag(line: dict[str, Any], offer: Any, delivery: date) -> Optional[dict[str, Any]]:
    if not isinstance(line, dict):
        return None
    if isinstance(delivery, datetime):
        delivery = delivery.date()
    if isinstance(delivery, bool) or not isinstance(delivery, date):
        return None
    weeks = line.get("weeks")
    if not isinstance(weeks, (list, tuple)) or not weeks:
        return None
    qty_f = _finite_pos(weeks[-1])
    why = None
    if offer is None:
        why = "no live offer"
    elif getattr(offer, "sales_block", False) is True:
        why = "sales block"
    elif getattr(offer, "cutoff_passed", False) is True:
        why = "order cut-off has passed"
    elif getattr(offer, "available", False) is not True:
        why = "out of stock" if "out_of_stock" in (getattr(offer, "issues", None) or []) else "not available"
    else:
        if qty_f is None:
            return None
        avail = getattr(offer, "available_amount", None)
        if avail is not None:
            if isinstance(avail, bool) or not isinstance(avail, (int, float)):
                why = "availability error"
            else:
                av = float(avail)
                if math.isnan(av) or av < 0:
                    why = "availability error"
                elif math.isfinite(av) and av < qty_f:
                    unit_code = str(line.get("unit") or "")
                    unit = UNIT_WORDS.get(unit_code, unit_code)
                    why = f"only {av:g} of {qty_f:g} {unit} in stock"
    if why is None:
        return None
    label = str(line.get("label") or line.get("product_id") or "")
    return {"product_id": line.get("product_id"), "kind": "availability", "change_pct": None, "route": "approval",
            "message": f"{label} cannot be delivered as usual on {delivery:%a %d %b} ({why}). "
                       f"The standing order needs a substitute, a smaller quantity or a later date.",
            "fix": {"action": "replan"}}


def check_drift(customer_id: str, *, today: Optional[date] = None, rebaseline: bool = False,
                offers_fn: Optional[Callable] = None, audit: bool = True) -> dict[str, Any]:
    """Full result: {customer_id, delivery_date, standing_order, flags, provenance}."""
    so = standing_order(customer_id, today)
    delivery = date.fromisoformat(so["delivery_date"])
    lines = SEEDS[customer_id]["lines"]
    ids = [l["product_id"] for l in lines]
    qty: dict[str, float] = {}
    for l in lines:
        w = l.get("weeks") or []
        qf = _finite_pos(w[-1]) if w else None
        if qf is not None:
            qty[l["product_id"]] = qf
    meta: dict[str, Any] = {}
    if offers_fn is not None:
        offers = offers_fn(ids, delivery, customer_id=customer_id)
    else:
        from aimo.backend.client import get_offers_with_meta
        offers, meta = get_offers_with_meta(ids, delivery, customer_id, qty)
    if not isinstance(offers, dict):
        offers = {}
    if not isinstance(meta, dict):
        meta = {}
    base = _baseline(customer_id)
    excluded = _excluded(customer_id)
    flags: list[dict[str, Any]] = []
    observed: dict[str, float] = {}
    for l in lines:
        pid = l["product_id"]
        o = offers.get(pid)
        f = availability_flag(l, o, delivery)
        if f:
            flags.append(f)
        up = _finite_pos(getattr(o, "unit_price", None)) if o is not None else None
        if up is not None:
            observed[pid] = up
            if pid in base and not rebaseline:
                pf = price_flag(l, base[pid][0], base[pid][1], up)
                if pf:
                    flags.append(pf)
        vf = volume_flag(l)
        if vf:
            flags.append(vf)
        bad = _allergen_conflicts(pid, excluded)
        if bad:
            flags.append({"product_id": pid, "kind": "allergen", "change_pct": None, "route": "approval",
                          "message": f"{l['label']} now lists {', '.join(bad)}, which {customer_id}'s policy excludes. "
                                     f"Remove it from the standing order or pick a safe alternative.",
                          "fix": {"action": "remove_line"}})
    clean = {p: v for p, v in observed.items()
             if not any(str(i).startswith("injected:") for i in (getattr(offers.get(p), "issues", None) or ()))}
    _save_baseline(customer_id, clean, replace=bool(rebaseline))
    for f in flags:
        f.setdefault("product_name", next((l["name"] for l in lines if l["product_id"] == f["product_id"]), None))
    result = {"customer_id": customer_id, "delivery_date": delivery.isoformat(), "standing_order": so, "flags": flags,
              "summary": (f"{len(flags)} drift flag(s) on {customer_id}'s standing order for {delivery:%a %d %b}: "
                          + " ".join(f["message"] for f in flags)) if flags else
                         f"No drift on {customer_id}'s standing order for {delivery:%a %d %b}.",
              "provenance": {"source": "aimo.agents.drift (ported from Momin's junction26-valio agents/drift.py)",
                             "agent": ACTOR, "checked_at": _now().isoformat(timespec="seconds"),
                             "history": "seeded demo history",
                             "price_baseline": "first live price observed by this gateway (state DB)",
                             "offers": {k: meta.get(k) for k in ("source", "served_from", "fetched_at", "valid_for_s",
                                                                 "faults") if k in meta}}}
    if audit:
        try:
            from aimo.gateway import service
            service.audit(ACTOR, "drift.checked", f"drift-{customer_id}", result["summary"][:900],
                          {"flags": [{k: f.get(k) for k in ("product_id", "kind", "change_pct", "route")} for f in flags],
                           "delivery_date": delivery.isoformat()})
        except Exception:
            log.exception("drift audit failed")
    return result


def detect_drift(customer_id: str, **kw: Any) -> list[dict[str, Any]]:
    """[{product_id, kind: price|volume|availability|allergen, change_pct, message, route, fix, ...}]"""
    return check_drift(customer_id, **kw)["flags"]
