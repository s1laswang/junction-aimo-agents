import re

from aimo.allocate import _ratio
from aimo.models import Constraint, NeedItem, Offer, Product

CODE_NAMES = {
    "AC": "crustaceans", "AE": "egg", "AF": "fish", "AM": "milk", "AN": "tree nuts", "AP": "peanuts",
    "AS": "sesame", "AU": "sulphites", "AW": "gluten", "AY": "soy", "BC": "celery", "BM": "mustard",
    "NL": "lupin", "UM": "molluscs", "ML": "lactose", "UW": "wheat", "GB": "barley", "GO": "oats", "NR": "rye",
}
ALIASES = {
    "milk": {"milk", "lactose"}, "maito": {"milk", "lactose"}, "dairy": {"milk", "lactose"}, "lactose": {"milk", "lactose"},
    "egg": {"egg"}, "eggs": {"egg"}, "fish": {"fish"}, "crustaceans": {"crustaceans"}, "shellfish": {"crustaceans", "molluscs"},
    "soy": {"soy"}, "soya": {"soy"}, "gluten": {"gluten", "wheat", "barley", "rye", "oats"},
    "wheat": {"wheat", "gluten"}, "barley": {"barley", "gluten"}, "rye": {"rye", "gluten"}, "oat": {"oats"}, "oats": {"oats"},
    "mustard": {"mustard"}, "sesame": {"sesame"}, "celery": {"celery"}, "peanut": {"peanuts"}, "peanuts": {"peanuts"},
    "nuts": {"tree nuts", "peanuts"}, "tree nuts": {"tree nuts"}, "sulphites": {"sulphites"}, "lupin": {"lupin"},
}
GLUTEN_HARD = {"gluten", "wheat", "barley", "rye"}
LACTOSE_NAME = re.compile(r"lactose[- ]free|laktoositon|laktosfri")
GLUTEN_NAME = re.compile(r"gluten[- ]free|gluteeniton|glutenfri")


def _present(product):
    out = set()
    for a in product.allergens:
        a = str(a).strip()
        a = a.split(":", 1)[-1] if a.lower().startswith("may_contain:") else a
        out.add(CODE_NAMES.get(a.upper(), a.lower()))
    return out


def _labels(product):
    return {re.sub(r"[\s-]+", "_", str(x).strip().lower()) for x in product.labels}


def allergen_safe(product: Product, constraints: Constraint, exclude_allergens: list[str]) -> bool:
    names = {str(a).strip().lower() for a in [*constraints.exclude_allergens, *exclude_allergens]}
    if not (names or constraints.lactose_free or constraints.gluten_free):
        return True
    if not product.allergens and not product.labels:
        return False
    present, labels, name = _present(product), _labels(product), product.name.lower()
    lf = "free_from_lactose" in labels or bool(LACTOSE_NAME.search(name))
    for a in names:
        mapped = ALIASES.get(a, {a})
        if a not in ALIASES and a not in CODE_NAMES.values():
            return False
        if mapped & present and not (a in ("lactose",) and lf):
            return False
    if constraints.lactose_free and not lf:
        return False
    if constraints.gluten_free:
        if GLUTEN_HARD & present:
            return False
        if not ("free_from_gluten" in labels or GLUTEN_NAME.search(name)):
            return False
    return True


SYNONYMS = {
    "milk": ["milk", "maito", "mjölk"],
    "cream": ["cream", "kerma", "grädde"],
    "butter": ["butter", "voi", "smör"],
    "yogurt": ["yogurt", "yoghurt", "jogurtti"],
    "cheese": ["cheese", "juusto", "ost"],
    "potato": ["potato", "peruna"],
    "tomato": ["tomato", "tomaatti"],
    "carrot": ["carrot", "porkkana"],
    "apple": ["apple", "omena"],
    "egg": ["egg", "kananmuna", "muna"],
    "flour": ["flour", "jauho"],
    "rice": ["rice", "riisi"],
    "salmon": ["salmon", "lohi"],
    "quark": ["quark", "rahka"],
    "chicken": ["chicken", "kana"],
    "onion": ["onion", "sipuli"],
    "oat": ["oat", "kaura"],
    "bread": ["bread", "leipä"],
    "fish": ["fish", "kala"],
    "beef": ["beef", "naudan"],
    "pork": ["pork", "possu", "porsaan"],
    "sugar": ["sugar", "sokeri"],
    "juice": ["juice", "mehu"],
}
DERIVED = [
    "sauce", "kastike", "salad", "salaatti", "soup", "keitto", "cake", "kakku", "pie", "piirakka", "juice", "mehu",
    "mash", "muusi", "chips", "fries", "ranskal", "pizza", "bar", "patukka", "pudding", "puuro", "jam", "hillo",
    "cookie", "keksi", "noodle", "nuudeli", "mayo", "majoneesi", "dumpling", "bun", "pulla", "croissant", "knife",
    "veitsi", "lasagne", "burger", "sandwich", "decoration", "koriste", "chicken", "kana", "meat", "liha",
    "potato", "peruna", "cider", "siideri", "ketchup", "paste", "seos", "rahka", "quark",
]


def _forms(w):
    w = {"potatoes": "potato", "tomatoes": "tomato"}.get(w, w)
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    forms = SYNONYMS.get(w, [w])
    return [f[:-1] if len(f) > 4 and f[-1] in "aeiouyäö" else f for f in forms]


PENALTY_GROUPS = [
    ("chocolate", ["chocolate", "kaakao", "suklaa", "cocoa", "choklad"], 0.7),
    ("flavour", ["vanilla", "vanilja", "strawberry", "mansikka", "banana", "banaani", "peach", "persikka", "mango", "flavoured", "maustettu"], 0.5),
    ("coffee", ["coffee", "kahvi", "café", "cafe", "latte", "cappuccino", "espresso", "cold brew"], 0.7),
    ("powder", ["powder", "jauhe", "pulver"], 0.6),
    ("fermented", ["fermented", "piimä", "viili", "jogurtti", "yogurt", "yoghurt", "kefir", "filmjölk"], 0.5),
    ("cooking", ["cooking", "ruoka", "cream", "kerma", "mix", "blend", "vegetable fat", "kasvirasva"], 0.35),
    ("bakery", ["cake", "kakku", "bread", "leipä", "flour", "jauho", "base", "pohja"], 0.7),
    ("portion", ["portion", "annos"], 0.4),
]
MILK_CATEGORY = "10400"
PORTION_RE = re.compile(r"\b\d+\s*x\s*\d+\s*(cl|ml|dl)\b")


def _has(text, words):
    return any(w in text for w in words)


def match_score(item: NeedItem, product: Product) -> float:
    q = item.query.lower()
    name = product.name.lower()
    qwords = [w for w in re.findall(r"[\wäöå]+", q) if w not in ("lactose", "free", "laktoositon", "gluten", "and", "of")]
    if not qwords:
        return 0.0
    hit = 0
    for w in qwords:
        if _has(name, _forms(w)):
            hit += 1
    if hit == 0:
        return 0.0
    score = 0.6 + 0.4 * hit / len(qwords)
    for _, words, pen in PENALTY_GROUPS:
        if _has(name, words) and not _has(q, words):
            score *= 1 - pen
    qf = [f for w in qwords for f in _forms(w) + [w]]
    for d in DERIVED:
        hit_d = d in name if len(d) >= 5 else re.search(r"\b" + d, name)
        if hit_d and not any(d in f or f in d for f in qf):
            score *= 0.5
    score -= 0.005 * min(len(re.findall(r"[\wäöå]+", name)), 20)
    if PORTION_RE.search(name) and "portion" not in q:
        score *= 0.4
    if qwords == ["milk"] or qwords == ["maito"]:
        score *= 1.0 if product.category == MILK_CATEGORY else 0.4
    return round(max(0.0, min(1.0, score)), 3)


MIN_MATCH = 0.5


def _unit_price(o, item):
    r = _ratio(o.product, item.unit)
    if r and r > 0:
        return o.unit_price_net / r, f"per {item.unit}"
    return o.unit_price_net, "per sales unit"


def _pack(p, item):
    r = _ratio(p, item.unit)
    return f"{r:.3g} {item.unit} packs" if r and not (p.sales_unit and item.unit.strip().lower() == p.sales_unit.strip().lower()) else None


def _unit_for(base):
    for u in ("l", "kg"):
        if _ratio(base.product, u):
            return u
    return base.product.sales_unit or "pcs"


def tradeoff(o: Offer, base: Offer, unit: str | None = None) -> str:
    item = NeedItem(query="", quantity=1, unit=unit or _unit_for(base))
    return _line(o, base, item)


def _line(o, base, item):
    price, per = _unit_price(o, item)
    parts = []
    if base:
        bp, bper = _unit_price(base, item)
        if bper == per and bp > 0:
            d = (price - bp) / bp * 100
            parts.append(f"{abs(d):.0f}% {'cheaper' if d < 0 else 'pricier'} {per}" if round(abs(d)) else f"same price {per}")
        a, b = _pack(o.product, item), _pack(base.product, item)
        if a and b and a != b:
            parts.append(f"{a} instead of {b}")
        if o.reliability is not None and base.reliability is not None and o.reliability != base.reliability:
            parts.append(f"reliability {o.reliability:g} vs {base.reliability:g}")
        if o.co2_per_kg is not None and base.co2_per_kg is not None and o.co2_per_kg != base.co2_per_kg:
            parts.append(f"CO2 {o.co2_per_kg:g} vs {base.co2_per_kg:g} kg/kg")
        if not parts:
            parts.append(f"same price, pack size, reliability and CO2 as {base.product.name}")
        return ", ".join(parts)
    parts.append(f"{price:.2f} {o.currency} {per}")
    if _pack(o.product, item):
        parts.append(_pack(o.product, item))
    if o.reliability is not None:
        parts.append(f"reliability {o.reliability:g}")
    if o.co2_per_kg is not None:
        parts.append(f"CO2 {o.co2_per_kg:g} kg/kg")
    return ", ".join(parts)


def rank_substitutes(item: NeedItem, offers: list[Offer], exclude_allergens: list[str], original: Product | None = None, limit: int = 3) -> list[Offer]:
    oid = original.product_id if original else None
    base = next((o for o in offers if o.product_id == oid), None)
    cands = []
    for o in offers:
        if o.product_id == oid or o.sales_block or o.available_amount <= 0:
            continue
        if not allergen_safe(o.product, item.constraints, exclude_allergens):
            continue
        m = match_score(item, o.product)
        if m >= MIN_MATCH:
            cands.append(o.model_copy(update={"match_score": m}))
    if not cands:
        return []
    key_best = lambda o: (-o.match_score, -(o.reliability or 0), _unit_price(o, item)[0])
    key_good = lambda o: (_unit_price(o, item)[0], -o.match_score)
    key_better = lambda o: (-(o.match_score * 0.5 + (o.reliability or 0) * 0.3 - _unit_price(o, item)[0] / max(_unit_price(x, item)[0] for x in cands) * 0.2), o.product_id)
    picked = []
    for tier, key in (("good", key_good), ("best", key_best), ("better", key_better)):
        pool = [o for o in cands if o.product_id not in {p.product_id for p, _ in picked}]
        if pool:
            picked.append((min(pool, key=key), tier))
    out = []
    for o, tier in picked:
        out.append(o.model_copy(update={"tier": tier, "notes": [_line(o, base, item), *o.notes]}))
    order = {"good": 0, "better": 1, "best": 2}
    return sorted(out, key=lambda o: order[o.tier])[:limit]


def safe_alternatives(item: NeedItem, exclude_allergens: list[str]) -> list[tuple[NeedItem, str]]:
    from aimo import catalog
    from aimo.recipes import recipe_swaps

    c = item.constraints.model_copy(update={"exclude_allergens": [*item.constraints.exclude_allergens, *exclude_allergens]})
    out = []
    for o in recipe_swaps(item.query, c):
        alt = item.model_copy(update={"query": o["query"], "quantity": item.quantity * o["ratio"], "constraints": c})
        if any(allergen_safe(p, c, []) and match_score(alt, p) >= MIN_MATCH for p in catalog.search(alt.query, c, limit=50)):
            out.append((alt, o["why"]))
    return out
