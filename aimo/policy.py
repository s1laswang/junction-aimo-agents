from datetime import datetime

from aimo.models import Decision, Reason

_CODES = {
    "peanut": "AP", "peanuts": "AP", "nuts": "AN", "nut": "AN", "tree nuts": "AN", "milk": "AM",
    "lactose": "ML", "wheat": "UW", "barley": "GB", "oats": "GO", "rye": "NR", "egg": "AE", "eggs": "AE", "gluten": "AW", "soy": "AY",
    "soya": "AY", "fish": "AF", "crustaceans": "AC", "shellfish": "AC", "sesame": "AS",
    "celery": "BC", "mustard": "BM", "lupin": "NL", "molluscs": "UM", "sulphites": "AU",
}
_LABELS = {"ML": "lactose", "UW": "wheat", "GB": "barley", "GO": "oats", "NR": "rye", "AP": "peanut", "AN": "nuts", "AM": "milk", "AE": "egg", "AW": "gluten", "AY": "soy",
           "AF": "fish", "AC": "crustaceans", "AS": "sesame", "BC": "celery", "BM": "mustard",
           "NL": "lupin", "UM": "molluscs", "AU": "sulphites"}


def _canon(a):
    a = str(a).strip()
    return _CODES.get(a.lower(), a.upper())


_WIDE = {"nuts": {"AN", "AP"}, "nut": {"AN", "AP"}, "milk": {"AM", "ML"}, "dairy": {"AM", "ML"}, "gluten": {"AW", "UW", "GB", "NR"}}


def _expand(a):
    return _WIDE.get(str(a).strip().lower()) or {_canon(a)}


def _eur(x):
    return f"{x:,.2f} EUR"


def decide(quote, mandate, products, spent_this_week=0.0, substitutions=(), now=None):
    blocked, ask, info = [], [], []
    total = quote.total_net
    banned = set().union(*[_expand(a) for a in mandate.exclude_allergens]) if mandate.exclude_allergens else set()

    def add(bucket, code, text):
        bucket.append(Reason(code=code, text=text))

    ids = [l.product_id for l in quote.lines] + [s.substitute_id for s in substitutions]
    for pid in dict.fromkeys(ids):
        p = products.get(pid)
        if p is None:
            if banned:
                add(ask, "UNKNOWN_PRODUCT", f"Could not check allergens for {pid}: product data missing")
            continue
        is_trace = lambda a: str(a).lower().startswith(("may:", "may_contain:"))
        have = {_canon(a) for a in p.allergens if not is_trace(a)}
        trace = {_canon(str(a).split(":", 1)[1]) for a in p.allergens if is_trace(a)}
        hit = sorted(have & banned)
        if hit:
            names = ", ".join(_LABELS.get(h, h) for h in hit)
            add(blocked, "ALLERGEN_CONFLICT", f"{p.name} ({pid}) contains {names}, which {mandate.customer} excludes")
        tr = sorted((trace & banned) - set(hit))
        if tr:
            names = ", ".join(_LABELS.get(h, h) for h in tr)
            add(blocked, "ALLERGEN_CONFLICT", f"{p.name} ({pid}) may contain traces of {names}, which {mandate.customer} excludes")
        if mandate.allowed_categories is not None and p.category not in mandate.allowed_categories:
            add(blocked, "CATEGORY_NOT_ALLOWED", f"{p.name} ({pid}) is in category {p.category}, allowed: {', '.join(mandate.allowed_categories)}")
        if mandate.preferred_brands and p.brand not in mandate.preferred_brands:
            add(info, "NON_PREFERRED_BRAND", f"{p.name} ({pid}) is brand {p.brand}, preferred: {', '.join(mandate.preferred_brands)}")

    def nm(pid):
        p = products.get(pid)
        return f"{p.name} ({pid})" if p else str(pid)

    for s in substitutions:
        sub = products.get(s.substitute_id)
        swap = f"{nm(s.substitute_id)} in place of {nm(s.original_id)}"
        if mandate.substitutions == "never":
            add(blocked, "SUBSTITUTION_NOT_ALLOWED", f"Substitution of {swap} is not allowed by the mandate of {mandate.customer}")
        elif mandate.substitutions == "ask":
            add(ask, "SUBSTITUTION_NEEDS_APPROVAL", f"The order swaps {swap}; {mandate.customer} requires a human to approve every substitution")
        elif mandate.preferred_brands and (sub is None or sub.brand not in mandate.preferred_brands):
            brand = sub.brand if sub else "unknown"
            add(ask, "SUBSTITUTION_NEEDS_APPROVAL", f"The order swaps {swap}; brand {brand} is outside the preferred brands {', '.join(mandate.preferred_brands)}")

    if total > mandate.per_order_eur:
        add(ask, "OVER_ORDER_LIMIT", f"Order total {_eur(total)} is over the {_eur(mandate.per_order_eur)} per-order limit")
    if spent_this_week + total > mandate.per_week_eur:
        add(ask, "OVER_WEEK_LIMIT", f"Order total {_eur(total)} plus {_eur(spent_this_week)} already spent this week is {_eur(spent_this_week + total)}, over the {_eur(mandate.per_week_eur)} weekly budget")

    t = now or datetime.now(quote.valid_until.tzinfo)
    if quote.valid_until < t:
        add(blocked, "PAST_CUTOFF", f"Quote was valid until {quote.valid_until:%Y-%m-%d %H:%M}, the ordering cutoff has passed")

    if blocked:
        return Decision(outcome="blocked", reasons=blocked + ask + info)
    if ask:
        return Decision(outcome="needs_approval", reasons=ask + info)
    ok = [Reason(code="WITHIN_MANDATE", text=f"Order total {_eur(total)} is under the {_eur(mandate.per_order_eur)} per-order limit and {_eur(spent_this_week + total)} of the {_eur(mandate.per_week_eur)} weekly budget")]
    return Decision(outcome="auto", reasons=ok + info)
