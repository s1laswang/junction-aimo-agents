import importlib
from zoneinfo import ZoneInfo
from datetime import date, datetime, time, timedelta, timezone

from . import valio
from .models import (
    Mandate, Need, NeedItem, Offer, OfferGroup, OffersResponse, Product,
    QuoteLine, QuoteRequestLine, SimulateStock,
)
from .quotes import QuoteError

HEL = ZoneInfo("Europe/Helsinki")
_overrides: dict[str, SimulateStock] = {}
MIN_MATCH = 0.5


def _mod(name: str):
    try:
        return importlib.import_module(f"aimo.{name}")
    except ImportError:
        return None


def _catalog_get(catalog, product_id: str) -> Product | None:
    if not catalog:
        return None
    try:
        return catalog.get(product_id)
    except FileNotFoundError:
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
        p = _catalog_get(catalog, pid)
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


def _priced(customer: str, day: date, item: NeedItem, products: list[Product], scores: dict[str, float], now: datetime | None = None) -> list[Offer]:
    now = now or datetime.now(HEL)
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
            adate = line.get("availableDate")
            if ct:
                co = cutoff(ct, adate, day)
                notes.append(f"cutoff computed from Valio closing time {co:%H:%M}, day before delivery")
                if co <= now:
                    new = date.fromisoformat(adate) if adate else day
                    for _ in range(14):
                        new += timedelta(days=1)
                        co = cutoff(ct, new, day)
                        if co > now:
                            break
                    notes.insert(0, f"cutoff passed for {adate or day}, earliest delivery {new}")
                    adate = new.isoformat()
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
                available_date=adate,
                closing_time=ct,
                notes=notes,
                sales_block="blocked" if blocked else None,
                reliability=catalog.reliability(p.product_id) if catalog else None,
                co2_per_kg=catalog.co2_per_kg(p.product_id) if catalog else None,
                match_score=scores.get(p.product_id, 1.0),
            ))
    allocate = _mod("allocate")

    def per_unit(o):
        r = allocate._ratio(o.product, item.unit) if allocate else None
        return o.unit_price_net / r if r else float("inf")

    offers.sort(key=lambda o: (
        bool(o.available_date and o.available_date > day),
        0.0 if o.match_score >= 0.9 else 1.0 - o.match_score,
        per_unit(o),
        -(o.reliability or 0),
        o.product_id,
    ))
    return offers


def _item(need: Need, item: NeedItem, mandate: Mandate | None, now: datetime | None = None) -> OfferGroup:
    catalog, subst, allocate = _mod("catalog"), _mod("substitute"), _mod("allocate")
    excl = list(dict.fromkeys([*item.constraints.exclude_allergens, *(mandate.exclude_allergens if mandate else [])]))
    c = item.constraints.model_copy(update={"exclude_allergens": excl})
    item = item.model_copy(update={"constraints": c})
    cands = catalog.search(item.query, c, limit=50) if catalog else []
    dropped = 0
    if subst:
        before = len(cands)
        cands = [p for p in cands if subst.allergen_safe(p, c, excl)]
        dropped = before - len(cands)
    scores = {p.product_id: subst.match_score(item, p) if subst else 1.0 for p in cands}
    cands = [p for p in cands if scores[p.product_id] >= MIN_MATCH]
    offers = _priced(need.customer, need.deliver_by, item, cands, scores, now)
    if allocate:
        chosen, shortfall = allocate.allocate(item, offers, need.deliver_by, now)
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
    remaining = shortfall
    if shortfall > 1e-9 and subst and offers:
        used = {o.product_id for o in chosen}
        base = chosen[0] if chosen else offers[0]
        original = base.product
        pool = [o for o in offers if o.available_date and o.available_date <= need.deliver_by and (o.product_id not in used or o.product_id == original.product_id)]
        subs = subst.rank_substitutes(item, pool, excl, original=original, limit=3)
        subs = [x for x in subs if x.product_id not in used]
        filled, remaining = allocate.allocate(item.model_copy(update={"quantity": shortfall}), subs, need.deliver_by, now) if allocate and subs else ([], shortfall)
        for x in filled:
            x.substitute_for = original.product_id
            x.notes.insert(0, subst.tradeoff(x, base))
            chosen.append(x)
    if shortfall > 1e-9 and subst:
        for alt, why in subst.safe_alternatives(item, excl):
            g = _item(need, alt.model_copy(update={"quantity": alt.quantity * remaining / item.quantity}), mandate, now)
            if g.offers:
                for x in g.offers:
                    x.tier = "good"
                    x.substitute_for = None
                    x.notes.insert(0, f"Swap for {item.query}: {why}")
                chosen.extend(g.offers)
                break
    if dropped and chosen:
        chosen[0].notes.insert(0, f"Excluded {dropped} products containing {', '.join(excl)} (mandate)")
    alts = _alternatives(item, [o for o in offers if o.available_date and o.available_date <= need.deliver_by], chosen, excl, subst)
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
    return subst.rank_substitutes(item, pool, excl, original=base.product, limit=3)


def get_offers(need: Need, mandate: Mandate | None = None, now: datetime | None = None) -> OffersResponse:
    return OffersResponse(items=[_item(need, it, mandate, now) for it in need.items])


def _local_noon(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=datetime.now().astimezone().tzinfo)


def price_lines(customer: str, delivery_date: date, lines: list[QuoteRequestLine]):
    if delivery_date < datetime.now(HEL).date():
        raise QuoteError("INVALID_QUOTE", "Delivery date is in the past.")
    catalog = _mod("catalog")
    ids = [l.product_id for l in lines]
    prices = valio.price(customer, delivery_date, ids)
    avail = _availability(customer, delivery_date, ids)
    out, gross, closes = [], 0.0, []
    for l in lines:
        pr = prices.get(l.product_id)
        if not pr or pr.get("error") or not pr.get("catalogPrice"):
            raise QuoteError("INVALID_QUOTE", f"No price available for product {l.product_id}.")
        net = pr["catalogPrice"]["value"]
        gross += l.quantity * (pr.get("priceWithTax") or {}).get("value", net)
        p = _catalog_get(catalog, l.product_id)
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
