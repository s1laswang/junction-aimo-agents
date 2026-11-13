import importlib
from zoneinfo import ZoneInfo
from datetime import date, datetime, time, timedelta, timezone

from . import valio
from .models import (
    Mandate, Need, NeedItem, Offer, OfferGroup, OffersResponse, Product,
    QuoteLine, QuoteRequestLine, SimulateStock,
)

HEL = ZoneInfo("Europe/Helsinki")
_overrides: dict[str, SimulateStock] = {}
MIN_MATCH = 0.5


def _mod(name: str):
    try:
        return importlib.import_module(f"aimo.{name}")
    except ImportError:
        return None


def simulate(s: SimulateStock) -> None:
    _overrides[str(s.product_id)] = s
    from aimo import orders
    react = getattr(orders, "react_stockout", None)
    if react:
        react(str(s.product_id))


def fresh_offers(customer: str, day: date, product_ids) -> list[Offer]:
    catalog = _mod("catalog")
    seen, out = set(), []
    for pid in product_ids:
        p = catalog.get(pid)
        if not p:
            continue
        pool = [p] + [c for c in catalog.search(p.name, limit=10) if c.product_id != pid]
        for o in _priced(customer, day, NeedItem(query=p.name, quantity=1, unit=p.sales_unit or "ST"), pool, {}):
            if (o.product_id, o.available_date) not in seen:
                seen.add((o.product_id, o.available_date))
                out.append(o)
    return out


def cutoff(closing_time: str, available_date, day: date) -> datetime:
    clock = datetime.fromisoformat(closing_time).astimezone(HEL).timetz().replace(tzinfo=None)
    if isinstance(available_date, str):
        available_date = date.fromisoformat(available_date)
    return datetime.combine((available_date or day) - timedelta(days=1), clock, HEL)


def clear_simulation() -> None:
    _overrides.clear()


def _availability(customer: str, day: date, ids) -> dict[str, dict]:
    data = valio.availability(customer, day, ids)
    out = {}
    for pid, a in data.items():
        o = _overrides.get(pid)
        if o:
            lines = a.get("scheduleLines") or [{}]
            a = {
                **a,
                "scheduleLines": [{
                    "salesBlock": False,
                    "availableAmount": o.available_amount,
                    "availableDate": (o.available_date or day).isoformat(),
                    "salesUnit": lines[0].get("salesUnit"),
                }],
            }
        out[pid] = a
    return out


def _units(product: Product, quantity: float, unit: str) -> float | None:
    allocate = _mod("allocate")
    if allocate:
        return allocate.units_for(product, quantity, unit)
    return float(-(-quantity // 1))


def _priced(customer: str, day: date, item: NeedItem, products: list[Product], scores: dict[str, float]) -> list[Offer]:
    catalog = _mod("catalog")
    ids = [p.product_id for p in products]
    prices = valio.price(customer, day, ids)
    avail = _availability(customer, day, ids)
    offers = []
    for p in products:
        pr, av = prices.get(p.product_id), avail.get(p.product_id)
        if not pr or not av or pr.get("error") or not pr.get("catalogPrice"):
            continue
        net = pr["catalogPrice"]["value"]
        gross = (pr.get("priceWithTax") or {}).get("value", net)
        units = _units(p, item.quantity, item.unit)
        if units is None:
            continue
        for line in av.get("scheduleLines") or []:
            blocked = av.get("salesBlock") or line.get("salesBlock")
            ct, notes = av.get("closingTime"), []
            if ct:
                co = cutoff(ct, line.get("availableDate"), day)
                notes.append(f"cutoff computed from Valio closing time {co:%H:%M}, day before delivery")
                ct = co.isoformat()
            offers.append(Offer(
                product_id=p.product_id,
                product=p,
                units_needed=units,
                unit_price_net=net,
                total_net=round(units * net, 2),
                total_gross=round(units * gross, 2),
                currency=pr.get("currency") or "EUR",
                available_amount=line.get("availableAmount") or 0.0,
                available_date=line.get("availableDate"),
                closing_time=ct,
                notes=notes,
                sales_block="blocked" if blocked else None,
                reliability=catalog.reliability(p.product_id) if catalog else None,
                co2_per_kg=catalog.co2_per_kg(p.product_id) if catalog else None,
                match_score=scores.get(p.product_id, 1.0),
            ))
    offers.sort(key=lambda o: (-o.match_score, o.total_net))
    return offers


def _item(need: Need, item: NeedItem, mandate: Mandate | None) -> OfferGroup:
    catalog, subst, allocate = _mod("catalog"), _mod("substitute"), _mod("allocate")
    excl = list(dict.fromkeys([*item.constraints.exclude_allergens, *(mandate.exclude_allergens if mandate else [])]))
    c = item.constraints.model_copy(update={"exclude_allergens": excl})
    item = item.model_copy(update={"constraints": c})
    cands = catalog.search(item.query, c, limit=50) if catalog else []
    if subst:
        cands = [p for p in cands if subst.allergen_safe(p, c, excl)]
    scores = {p.product_id: subst.match_score(item, p) if subst else 1.0 for p in cands}
    cands = [p for p in cands if scores[p.product_id] >= MIN_MATCH]
    offers = _priced(need.customer, need.deliver_by, item, cands, scores)
    if allocate:
        chosen, shortfall = allocate.allocate(item, offers, need.deliver_by)
    else:
        chosen = [o for o in offers if o.available_amount > 0 and not o.sales_block][:1]
        shortfall = 0.0 if chosen else item.quantity
    if subst and len(chosen) > 1:
        brands = {b.lower() for b in c.brands}
        for o in chosen[1:]:
            if o.product_id == chosen[0].product_id:
                continue
            if o.match_score < chosen[0].match_score or (brands and (o.product.brand or "").lower() not in brands):
                o.substitute_for = chosen[0].product_id
                o.tier = o.tier or "good"
            o.notes.insert(0, subst.tradeoff(o, chosen[0]))
    if shortfall > 1e-9 and subst and offers:
        used = {o.product_id for o in chosen}
        original = (chosen[0] if chosen else offers[0]).product
        pool = [o for o in offers if o.product_id not in used or o.product_id == original.product_id]
        for s in subst.rank_substitutes(item, pool, excl, original=original, limit=3):
            if s.product_id in used:
                continue
            units = _units(s.product, shortfall, item.unit)
            if units is None:
                continue
            k = s.total_gross / s.total_net if s.total_net else 1.0
            net = round(units * s.unit_price_net, 2)
            chosen.append(s.model_copy(update={
                "units_needed": units, "total_net": net, "total_gross": round(net * k, 2),
                "substitute_for": original.product_id,
            }))
    alts = _alternatives(item, offers, chosen, excl, subst)
    opts = allocate.options(item, offers, need.deliver_by) if allocate and shortfall > 1e-9 else []
    return OfferGroup(need_item=item, offers=chosen, shortfall=shortfall, alternatives=alts, options=opts)


def _alternatives(item: NeedItem, offers: list[Offer], chosen: list[Offer], excl: list[str], subst) -> list[Offer]:
    if not subst or not offers:
        return []
    used = {o.product_id for o in chosen}
    base = chosen[0] if chosen else offers[0]
    seen, pool = set(), []
    for o in offers:
        if o.product_id not in seen and (o.product_id not in used or o is base or o.product_id == base.product_id):
            seen.add(o.product_id)
            pool.append(o)
    out = []
    for _ in range(2):
        picked = subst.rank_substitutes(item, pool, excl, original=base.product, limit=3)
        if not picked:
            break
        out += picked
        ids = {o.product_id for o in picked}
        pool = [o for o in pool if o.product_id not in ids]
    return out[:5]


def get_offers(need: Need, mandate: Mandate | None = None) -> OffersResponse:
    return OffersResponse(items=[_item(need, it, mandate) for it in need.items])


def _local_noon(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=datetime.now().astimezone().tzinfo)


def price_lines(customer: str, delivery_date: date, lines: list[QuoteRequestLine]):
    catalog = _mod("catalog")
    ids = [l.product_id for l in lines]
    prices = valio.price(customer, delivery_date, ids)
    avail = _availability(customer, delivery_date, ids)
    out, gross, closes = [], 0.0, []
    for l in lines:
        pr = prices.get(l.product_id)
        if not pr or pr.get("error") or not pr.get("catalogPrice"):
            raise ValueError(f"no price for {l.product_id}")
        net = pr["catalogPrice"]["value"]
        gross += l.quantity * (pr.get("priceWithTax") or {}).get("value", net)
        p = catalog.get(l.product_id) if catalog else None
        out.append(QuoteLine(
            product_id=l.product_id,
            name=p.name if p else None,
            quantity=l.quantity,
            sales_unit=p.sales_unit if p else None,
            unit_price_net=net,
            line_total_net=round(l.quantity * net, 2),
            substitute_for=l.substitute_for,
        ))
        a = avail.get(l.product_id) or {}
        if a.get("closingTime"):
            sl = (a.get("scheduleLines") or [{}])[0]
            closes.append(cutoff(a["closingTime"], sl.get("availableDate"), delivery_date))
    valid = min(closes) if closes else _local_noon(delivery_date - timedelta(days=1))
    return out, round(gross, 2), valid
