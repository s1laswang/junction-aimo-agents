import re

from aimo.models import Constraint, NeedItem, Offer, Product

ALLERGEN_CODES = {
    "milk": {"AM", "ML"},
    "lactose": {"AM", "ML"},
    "dairy": {"AM", "ML"},
    "egg": {"AE"},
    "eggs": {"AE"},
    "fish": {"AF"},
    "crustaceans": {"AC"},
    "shellfish": {"AC"},
    "soy": {"AY"},
    "soya": {"AY"},
    "gluten": {"AW", "UW", "GB", "NR", "NM", "GO", "SP"},
    "wheat": {"AW", "UW"},
    "barley": {"GB"},
    "rye": {"NR"},
    "oat": {"GO"},
    "oats": {"GO"},
    "mustard": {"BM"},
    "sesame": {"AS"},
    "celery": {"BC"},
    "peanut": {"AP"},
    "peanuts": {"AP"},
    "nuts": {"AN", "SA", "SH", "SC", "SW", "SM", "SR", "ST", "SP"},
    "tree nuts": {"AN", "SA", "SH", "SC", "SW", "SM", "SR", "ST"},
    "almond": {"SA"},
    "hazelnut": {"SH"},
    "sulphites": {"AU"},
    "lupin": {"NL"},
}
GLUTEN_HARD = {"AW", "UW", "GB", "NR", "NM"}
LACTOSE_NAME = re.compile(r"lactose[- ]free|laktoositon|laktosfri")
GLUTEN_NAME = re.compile(r"gluten[- ]free|gluteeniton|glutenfri")


def _norm(s):
    return re.sub(r"[\s-]+", "_", str(s).strip().upper())


def _codes(product):
    return {_norm(a).split(":")[0] for a in product.allergens}


def _labels(product):
    return {_norm(x) for x in product.labels}


def allergen_safe(product: Product, constraints: Constraint, exclude_allergens: list[str]) -> bool:
    names = {str(a).strip().lower() for a in [*constraints.exclude_allergens, *exclude_allergens]}
    need_data = bool(names) or constraints.lactose_free or constraints.gluten_free
    if not need_data:
        return True
    if not product.allergens and not product.labels:
        return False
    codes, labels, name = _codes(product), _labels(product), product.name.lower()
    for a in names:
        mapped = ALLERGEN_CODES.get(a)
        if mapped is None:
            if _norm(a) in codes:
                return False
            if a in ("lactose",):
                continue
            return False
        if mapped & codes:
            if a == "lactose" and ("FREE_FROM_LACTOSE" in labels or LACTOSE_NAME.search(name)):
                continue
            return False
    if constraints.lactose_free and not ("FREE_FROM_LACTOSE" in labels or LACTOSE_NAME.search(name)):
        return False
    if constraints.gluten_free:
        if GLUTEN_HARD & codes:
            return False
        if not ("FREE_FROM_GLUTEN" in labels or GLUTEN_NAME.search(name)):
            return False
    return True


SYNONYMS = {
    "milk": ["milk", "maito", "mjölk"],
    "cream": ["cream", "kerma", "grädde"],
    "butter": ["butter", "voi", "smör"],
    "yogurt": ["yogurt", "yoghurt", "jogurtti"],
    "cheese": ["cheese", "juusto", "ost"],
}
PENALTY_GROUPS = [
    ("chocolate", ["chocolate", "kaakao", "suklaa", "cocoa", "choklad"], 0.7),
    ("flavour", ["vanilla", "vanilja", "strawberry", "mansikka", "banana", "banaani", "peach", "persikka", "mango", "flavoured", "maustettu"], 0.5),
    ("coffee", ["coffee", "kahvi", "café", "cafe", "latte", "cappuccino", "espresso", "cold brew"], 0.5),
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
        syn = SYNONYMS.get(w, [w])
        if _has(name, syn):
            hit += 1
    if hit == 0:
        return 0.0
    score = 0.6 + 0.4 * hit / len(qwords)
    for _, words, pen in PENALTY_GROUPS:
        if _has(name, words) and not _has(q, words):
            score *= 1 - pen
    if PORTION_RE.search(name) and "portion" not in q:
        score *= 0.6
    if "milk" in qwords or "maito" in qwords:
        score *= 1.0 if product.category == MILK_CATEGORY else 0.4
    return round(max(0.0, min(1.0, score)), 3)
