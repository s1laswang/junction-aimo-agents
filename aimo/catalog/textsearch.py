"""Text search over the catalog: FTS5 trigram (handles Finnish compounds like
"kevytmaitojuoma"), fi/en/sv synonym expansion, claim/category/unit filters, optional
semantic recall with Gemini embeddings when FTS is weak."""
from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from typing import Any, Optional

from aimo.backend.catalog_store import connect, row_to_dict
from aimo.models import ProductSummary

# concept -> surface forms (en / fi / sv). Phrases first; matching is substring (trigram).
SYNONYMS: dict[str, list[str]] = {
    "lactose free": ["lactose free", "lactose-free", "lactosefree", "lactosfree", "laktoositon", "laktosfri"],
    "gluten free": ["gluten free", "gluten-free", "gluteeniton", "glutenfri"],
    "low lactose": ["low lactose", "vähälaktoosinen", "laktosreducerad"],
    "semi skimmed": ["semi skimmed", "semi-skimmed", "kevyt", "lätt", "low fat"],
    "skimmed": ["skimmed", "fat free", "fatfree", "rasvaton", "fettfri"],
    "whole milk": ["whole milk", "täysmaito", "standardmjölk", "helmjölk"],
    "milk": ["milk", "maito", "mjölk"],
    "cream": ["cream", "kerma", "grädde"],
    "butter": ["butter", "voi", "smör"],
    "cheese": ["cheese", "juusto"],
    "yogurt": ["yogurt", "yoghurt", "jogurtti", "yoghurt"],
    "egg": ["egg", "kananmuna", "ägg"],
    "bread": ["bread", "leipä", "bröd"],
    "oat": ["oat", "kaura", "havre"],
    "coffee": ["coffee", "kahvi", "kaffe"],
    "juice": ["juice", "mehu", "saft"],
    "potato": ["potato", "peruna", "potatis"],
    "chicken": ["chicken", "kana", "broiler", "kyckling"],
    "beef": ["beef", "nauta", "nöt"],
    "pork": ["pork", "sika", "possu", "fläsk"],
    "fish": ["fish", "kala", "fisk"],
    "salmon": ["salmon", "lohi", "lax"],
    "flour": ["flour", "jauho", "mjöl"],
    "sugar": ["sugar", "sokeri", "socker"],
    "organic": ["organic", "luomu", "ekologisk"],
    "vegan": ["vegan", "vegaani"],
    "ice cream": ["ice cream", "jäätelö"],
    "rice": ["rice", "riisi"],
    "pasta": ["pasta"],
    "tomato": ["tomato", "tomaatti", "tomat"],
    "apple": ["apple", "omena", "äpple"],
    "sour milk": ["sour milk", "piimä", "filmjölk"],
    "drink": ["drink", "juoma", "dryck"],
    "milk drink": ["milk drink", "milkdrink", "maitojuoma", "mjölkdryck"],
}
# words that imply a claim filter (used as a soft boost in search; plan_need uses explicit claims)
IMPLIED_CLAIMS = {"lactose free": "FREE_FROM_LACTOSE", "gluten free": "FREE_FROM_GLUTEN", "vegan": "VEGAN",
                  "organic": "ORGANIC"}
# product-type modifiers: if a name carries one the query did not ask for, it is probably a
# different product (milk -> milk coffee drink / milk chocolate / cheese). Soft penalty only.
VARIANT_MARKERS = ["mozzarella", "filata", "coffee", "kahvi", "latte", "mocca", "mocha", "cappuccino", "chocolate", "suklaa", "cocoa",
                   "kaakao", "flavour", "flavor", "maku", "vanilla", "vanilja", "mint", "strawberry", "mansikka",
                   "banana", "cheese", "juusto", "yoghurt", "yogurt", "jogurtti", "curdled", "rahka", "quark", "sour",
                   "piimä", "ice cream", "jäätelö", "pudding", "vanukas", "porridge", "puuro", "soup", "keitto",
                   "sauce", "kastike", "cake", "kakku", "pie", "piirakka", "bread", "leipä", "powder", "jauhe",
                   "dessert", "jälkiruoka", "smoothie", "protein", "proteiini", "cream", "kerma", "butter", "voi "]
STOP = {"of", "the", "a", "an", "and", "for", "with", "ja", "och", "litres", "liters", "litre", "liter", "l", "kg",
        "pcs", "some", "need", "we", "by", "at", "to", "on", "in", "please", "order", "deliver", "delivered",
        "delivery", "our", "us", "is", "are", "have", "has", "several", "many", "kilos", "kilo", "kilograms",
        "units", "pieces", "school", "schools", "pupils", "pupil", "students", "children", "kids", "lunch",
        "dinner", "breakfast", "kitchen", "hotel", "hospital", "patients", "guests", "staff", "event",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today", "tomorrow",
        "next", "week", "this", "morning", "evening", "maanantai", "tiistai", "keskiviikko", "torstai",
        "perjantai", "lauantai", "sunnuntai", "huomenna", "wednesday's", "monday's"}

# Allergen words. Exclusion mentions of these ("milk allergy", "no nuts", "dairy-free") must never
# become search concepts or product-type hints: safety is enforced by Need.excluded_allergens.
# gluten/lactose "-free" are NOT stripped: they are positive claims that appear in product names.
_ALLERGEN_WORDS = (r"(?:cow'?s\s+)?milk|dairy|nuts?|tree\s+nuts?|peanuts?|eggs?|soy|soya|fish|shellfish|sesame|"
                   r"celery|mustard|lupin|crustaceans?|molluscs?|sulphites?|sulfites?|wheat|maito|pähkinä\w*|"
                   r"kananmuna|muna|soija|kala|seesami|selleri|sinappi")
_ALLERGEN_LIST = rf"(?:{_ALLERGEN_WORDS})(?:\s*(?:,|and|or|&|ja|tai)\s*(?:{_ALLERGEN_WORDS}))*"
_EXCLUSION_PATTERNS = [
    # whole clauses that talk about allergies / intolerances
    re.compile(r"\b(?:for|with)?\s*(?:coeliacs?|celiacs?|keliaakik\w*)\s*(?:patients?|pupils?|guests?|diet|ruokavalio\w*)?", re.I),
    re.compile(r"[^,.;:!?()]*\b(?:allerg\w*|intoleran\w*|allergia\w*|anaphyla\w*)\b[^,.;:!?()]*", re.I),
    re.compile(rf"\b(?:no|without|free\s+from|free\s+of|not|zero|ei|ilman)\s+{_ALLERGEN_LIST}\b", re.I),
    re.compile(rf"\b{_ALLERGEN_LIST}\s*-?\s*free\b", re.I),
    re.compile(r"\b(?:maidoton|maidottomat?|pähkinätön|pähkinättömät?|munaton|kananmunaton|soijaton|mjölkfri|nötfri)\b", re.I),
]


def clean_query(q: str) -> str:
    """Remove allergen-exclusion mentions so they never feed search or product-type inference.

    'oat drink, pupils have milk and nut allergies' -> 'oat drink'
    'milk-free oat drink' -> 'oat drink';  'lactose-free milk' -> unchanged
    """
    s = q or ""
    for pat in _EXCLUSION_PATTERNS:
        s = pat.sub(" ", s)
    if not re.search(r"\w", s):   # the whole text was an allergy clause: drop only the allergy words
        s = re.sub(rf"\b(?:{_ALLERGEN_WORDS}|allerg\w*|intoleran\w*|have|has)\b", " ", q or "", flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"\s*[,;]\s*(?=[,;]|$)", "", s)).strip(" ,;.-")

_SURFACE_TO_CONCEPT: dict[str, str] = {}
for _c, _forms in SYNONYMS.items():
    for _f in _forms:
        _SURFACE_TO_CONCEPT.setdefault(_f.lower(), _c)
_SURFACES_BY_LEN = sorted(_SURFACE_TO_CONCEPT, key=len, reverse=True)


def parse_query(q: str) -> list[tuple[str, list[str]]]:
    """'lactose-free milk' -> [('lactose free', [...forms]), ('milk', ['milk','maito','mjölk'])]."""
    s = " " + re.sub(r"[^\w\-%,.']+", " ", clean_query(q).lower()) + " "
    found: list[tuple[int, str, list[str]]] = []   # (position in query, concept, forms)
    for surf in _SURFACES_BY_LEN:
        pat = re.compile(r"(?<![\w])" + re.escape(surf) + r"(?![\w])")
        m = pat.search(s)
        if m:
            c = _SURFACE_TO_CONCEPT[surf]
            if all(c != x[1] for x in found):
                found.append((m.start(), c, SYNONYMS[c]))
            s = pat.sub(lambda mm: " " * len(mm.group(0)), s)   # keep positions stable
    for m in re.finditer(r"\S+", s):
        w = m.group(0).strip("-.,'")
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) < 3 or w in STOP or re.fullmatch(r"[\d.,%]+", w):
            continue
        found.append((m.start(), w, [w]))
    return [(c, forms) for _pos, c, forms in sorted(found, key=lambda x: x[0])]


def _fts_term(t: str) -> Optional[str]:
    t = t.replace('"', " ").strip()
    if len(t) < 3:
        return None
    return f'"{t}"'


def _fts_query(concepts: list[tuple[str, list[str]]], mode: str = "AND") -> Optional[str]:
    groups = []
    for _c, forms in concepts:
        terms = [x for x in (_fts_term(f) for f in forms) if x]
        if terms:
            groups.append("(" + " OR ".join(terms) + ")")
    if not groups:
        return None
    return f" {mode} ".join(groups)


@lru_cache(maxsize=1)
def _con() -> sqlite3.Connection:
    return connect()


# ---------------------------------------------------------------- food vs non-food signal
# Category names are not in the data, so "non-food" is inferred from the data itself: categories where
# < 20 % of products carry a nutrition declaration AND whose code family is equipment / cleaning /
# disposables / paper / office (20xxx, 3xxxx, 6xxxx, 7xxxx, 9xxxx; UNVERIFIED code semantics, checked
# by eye: pans, plates, Tork napkins, cleaners, candles, office paper). Fresh produce (17xxx) and
# coffee/tea (22xxx) also lack nutrition data but stay food. Names add a second signal.
_NONFOOD_NAME = re.compile(r"\b(?:jugs?|kannu\w*|kaadin|pitcher|cleaner\w*|cleaning|pesu\w*|puhdist\w*|detergent|"
                           r"rinse aid|soap|saippua|dispenser|annostelija|napkins?|servet\w*|gloves?|käsine\w*|"
                           r"cloth|liina|foil|folio|film|kalvo|tray|tarjotin|cup|cups|mug|muki|plate|lautanen|"
                           r"bowl|kulho|lid|kansi|frother|vaahdotin|machine|kone|thermometer|brush|harja|"
                           r"container|rasia|bag|bags|pussi)\b", re.I)
_NONFOOD_QUERY = _NONFOOD_NAME


@lru_cache(maxsize=1)
def _nonfood_categories() -> frozenset[str]:
    rows = _con().execute("select category, avg(nutrition != '{}') as r from products group by category").fetchall()
    return frozenset(r["category"] for r in rows
                     if r["category"] and r["r"] < 0.2 and r["category"][0] in "23679" and not r["category"].startswith("22"))


def food_class(d: dict[str, Any]) -> str:
    """'food' | 'nonfood' | 'unknown' for ranking only (never a safety decision)."""
    if d.get("category") in _nonfood_categories():
        return "nonfood"
    if d.get("nutrition"):
        return "food"
    name = " ".join(x or "" for x in (d.get("name_en"), d.get("name_fi")))
    if _NONFOOD_NAME.search(name) and not d.get("ingredients_text"):
        return "nonfood"
    return "food" if d.get("ingredients_text") else "unknown"


def query_wants_nonfood(query: str) -> bool:
    return bool(_NONFOOD_QUERY.search(query or ""))


def summary_from_row(d: dict[str, Any], score: Optional[float] = None) -> ProductSummary:
    cu = d.get("content_unit")
    return ProductSummary(
        id=d["id"], gtin=d.get("gtin"), name=d.get("name") or d["id"], name_fi=d.get("name_fi"),
        brand=d.get("brand"), vendor=d.get("vendor"), category=d.get("category"),
        sales_unit=d.get("sales_unit") or "", content_amount=d.get("content_amount"),
        content_unit=cu if cu in ("L", "KG", "PCS") else None, claims=d.get("claims") or [],
        labels=d.get("labels") or [], delivery_reliability=d.get("delivery_reliability"),
        co2_kg_per_kg=d.get("co2_kg_per_kg"), score=score)


def get_rows(ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    con = _con()
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in con.execute(f"select * from products where id in ({','.join('?' * len(chunk))})", chunk):
            d = row_to_dict(r)
            try:  # allergen contradiction sweep: structured FREE_FROM vs ingredient text -> unknown
                from aimo.catalog.data_quality import apply as _dq
                _dq(d)
            except Exception:  # noqa: BLE001
                pass
            out[r["id"]] = d
    return out


def _filter_sql(claims: list[str], category: Optional[str], content_unit: Optional[str],
                free_of: Optional[list[str]] = None) -> tuple[str, list]:
    where, args = [], []
    for g in free_of or []:   # structured 'free' only (unknown never counts as safe)
        if re.fullmatch(r"[a-z]+", g):
            where.append(f"json_extract(p.allergens, '$.{g}') = 'free'")
    for c in claims or []:
        where.append("p.id in (select id from claims where claim = ?)")
        args.append(c)
    if category:
        where.append("p.category = ?")
        args.append(category)
    if content_unit:
        where.append("p.content_unit = ?")
        args.append(content_unit)
    return (" and " + " and ".join(where)) if where else "", args


def fts_search(query: str, *, claims: list[str] | None = None, category: Optional[str] = None,
               content_unit: Optional[str] = None, limit: int = 20, pool: int = 400,
               free_of: Optional[list[str]] = None) -> list[tuple[str, float]]:
    """[(id, score)] best first.

    FTS5 (trigram, all names + brand + ingredients + claim words) recalls up to `pool` rows; then
    we re-score in Python: concept coverage in the product *name* (any language) dominates,
    implied claims add, unrequested product-type modifiers subtract, bm25 breaks ties.
    """
    concepts = parse_query(query)
    con = _con()
    fsql, fargs = _filter_sql(claims or [], category, content_unit, free_of)
    bm: dict[str, float] = {}
    for mode in ("AND", "OR"):
        q = _fts_query(concepts, mode)
        if not q:
            break
        sql = (f"select f.id as id, bm25(fts, 0, 10.0, 10.0, 6.0, 3.0, 1.0, 4.0) as r from fts f "
               f"join products p on p.id = f.id where fts match ? {fsql} order by r limit ?")
        try:
            rows = con.execute(sql, [q, *fargs, pool]).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            bm.setdefault(r["id"], -float(r["r"]) * (1.0 if mode == "AND" else 0.5))
        if len(bm) >= limit or len(concepts) <= 1:
            break
    if not concepts and (claims or category or content_unit):  # pure filter browse
        rows = con.execute(f"select p.id as id from products p where 1=1 {fsql} limit ?", [*fargs, limit]).fetchall()
        return [(r["id"], 0.0) for r in rows]
    if not bm:
        return []
    top = max(bm.values()) or 1.0
    rows = get_rows(list(bm))
    ql = " ".join(f.lower() for _c, forms in concepts for f in forms)
    wants_nonfood = query_wants_nonfood(query)
    try:
        from aimo.catalog.need_types import types_from_synonyms
        from aimo.catalog.taxonomy import product_type as _ptype
        qtypes = types_from_synonyms(clean_query(query))
    except Exception:  # noqa: BLE001
        qtypes = set()
    results: dict[str, float] = {}
    for pid, d in rows.items():
        names = " ".join(x or "" for x in (d.get("name_en"), d.get("name_fi"), d.get("name_sv"))).lower()
        hit = sum(1 for _c, forms in concepts if any(f.lower() in names for f in forms))
        score = 10.0 * hit / max(len(concepts), 1)
        n_en = (d.get("name_en") or d.get("name_fi") or "").lower()
        extra = sum(1 for m in VARIANT_MARKERS if m in n_en and m.strip() not in ql)
        score -= 3.0 * min(extra, 2)
        for c, _ in concepts:
            cl = IMPLIED_CLAIMS.get(c)
            if cl and cl in (d.get("claims") or []):
                score += 2.0
        score += 2.0 * bm[pid] / top                       # 0..2 tie-breaker
        score -= 0.01 * max(0, len(n_en) - 40)             # long names usually = more specific products
        if not wants_nonfood:                              # food words should find food, not milk jugs
            fc = food_class(d)
            score += 1.0 if fc == "food" else (-8.0 if fc == "nonfood" else 0.0)
        if qtypes:                                         # taxonomy: "bread" should not rank a flour mix first
            pt = _ptype(pid)
            if pt:
                score += 3.0 if pt in qtypes else -6.0
        results[pid] = round(score, 4)
    return sorted(results.items(), key=lambda x: -x[1])[:limit]


def semantic_hits(query: str, *, claims: list[str] | None = None, category: Optional[str] = None,
                  content_unit: Optional[str] = None, k: int = 40) -> list[tuple[str, float]]:
    """Gemini-embedding recall with the same filters as FTS. [] if embeddings are not built/available."""
    try:
        from aimo.catalog.embeddings import available, semantic_search
        from aimo.catalog.netcheck import semantic_enabled
        if not semantic_enabled() or not available() or not (query or "").strip():
            return []
        raw = semantic_search(query, k=k * 3)
    except Exception:  # noqa: BLE001 - embeddings are optional (no key, offline, not built)
        return []
    rows = get_rows([pid for pid, _ in raw])
    out = []
    for pid, sim in raw:
        d = rows.get(pid)
        if not d:
            continue
        if claims and not set(claims) <= set(d.get("claims") or []):
            continue
        if category and d.get("category") != category:
            continue
        if content_unit and d.get("content_unit") != content_unit:
            continue
        out.append((pid, float(sim)))
        if len(out) >= k:
            break
    return out


def search(query: str, *, claims: list[str] | None = None, category: str | None = None,
           content_unit: str | None = None, limit: int = 20, semantic: bool = True) -> list[ProductSummary]:
    """Hybrid search: FTS (exact words, fi/en/sv synonyms) fused with Gemini semantic recall by
    weighted reciprocal rank fusion. FTS that matches every query concept in the name keeps
    priority; semantic recall rescues free-form queries ("dairy-free drink for coffee")."""
    fts = fts_search(query, claims=claims, category=category, content_unit=content_unit, limit=limit * 2)
    sem = semantic_hits(query, claims=claims, category=category, content_unit=content_unit, k=limit * 2) if semantic else []
    if not sem:
        hits = fts[:limit]
    else:
        fused: dict[str, float] = {}
        full = {pid for pid, sc in fts if sc >= 9.0}          # every concept found in the name
        for rank, (pid, _sc) in enumerate(fts):
            fused[pid] = fused.get(pid, 0) + 1.0 / (20 + rank) + (0.05 if pid in full else 0)
        for rank, (pid, _sim) in enumerate(sem):
            fused[pid] = fused.get(pid, 0) + 0.8 / (20 + rank)
        if not query_wants_nonfood(query):
            frows = get_rows(list(fused))
            for pid in fused:
                if pid in frows and food_class(frows[pid]) == "nonfood":
                    fused[pid] *= 0.3
        hits = sorted(((pid, sc * 100) for pid, sc in fused.items()), key=lambda x: -x[1])[:limit]
    rows = get_rows([h[0] for h in hits])
    return [summary_from_row(rows[pid], round(score, 4)) for pid, score in hits if pid in rows]
