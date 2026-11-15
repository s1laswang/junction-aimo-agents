"""Text search over the catalog: FTS5 trigram (handles Finnish compounds like
"kevytmaitojuoma"), fi/en/sv synonym expansion, claim/category/unit filters, optional
semantic recall with Gemini embeddings when FTS is weak."""
from __future__ import annotations

from collections import namedtuple
import math
import re
import sqlite3
import threading
from functools import lru_cache
from typing import Any, Optional

from aimo.backend.catalog_store import connect, row_to_dict
from aimo.models import ProductSummary

_db_lock = threading.RLock()
_CacheInfo = namedtuple("CacheInfo", ["hits", "misses", "maxsize", "currsize"])
_tls = threading.local()
_con_guard = threading.Lock()
_con_epoch = 0
_con_pool: list[sqlite3.Connection] = []
_con_hits = 0
_con_misses = 0

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


def _clean_bound(val: Any, max_val: int) -> int:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return 0
    if isinstance(val, float):
        if math.isnan(val):
            return 0
        if math.isinf(val):
            return max_val if val > 0 else 0
    if val <= 0:
        return 0
    return min(int(val), max_val)


def _clean_float(val: Any, *, positive: bool = False, non_negative: bool = False) -> Optional[float]:
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        fv = float(val)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(fv):
        return None
    if positive and fv <= 0.0:
        return None
    if non_negative and fv < 0.0:
        return None
    return fv


def _clean_opt_str(val: Any) -> Optional[str]:
    if not isinstance(val, str):
        return None
    s = val.replace("\x00", "")
    return s if s else None


def _parse_filters(
    claims: Any = None,
    category: Any = None,
    content_unit: Any = None,
    free_of: Any = None,
) -> Optional[tuple[list[str], Optional[str], Optional[str], list[str]]]:
    clean_claims: list[str] = []
    if claims is not None:
        if not isinstance(claims, (list, tuple, set, frozenset)):
            return None
        for c in claims:
            if not isinstance(c, str) or "\x00" in c or not c.strip():
                return None
            clean_claims.append(c)
    clean_cat: Optional[str] = None
    if category is not None and category != "":
        if not isinstance(category, str) or "\x00" in category:
            return None
        clean_cat = category
    clean_unit: Optional[str] = None
    if content_unit is not None and content_unit != "":
        if not isinstance(content_unit, str) or "\x00" in content_unit:
            return None
        clean_unit = content_unit
    clean_free: list[str] = []
    if free_of is not None:
        if not isinstance(free_of, (list, tuple, set, frozenset)):
            return None
        for g in free_of:
            if not isinstance(g, str) or "\x00" in g:
                return None
            clean_free.append(g)
    return clean_claims, clean_cat, clean_unit, clean_free


def clean_query(q: str) -> str:
    """Remove allergen-exclusion mentions so they never feed search or product-type inference.

    'oat drink, pupils have milk and nut allergies' -> 'oat drink'
    'milk-free oat drink' -> 'oat drink';  'lactose-free milk' -> unchanged
    """
    if not isinstance(q, str) or "\x00" in q:
        return ""
    raw = q[:4096]
    s = raw
    for pat in _EXCLUSION_PATTERNS:
        s = pat.sub(" ", s)
    if not re.search(r"\w", s):
        s = re.sub(rf"\b(?:{_ALLERGEN_WORDS}|allerg\w*|intoleran\w*|have|has)\b", " ", raw, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"\s*[,;]\s*(?=[,;]|$)", "", s)).strip(" ,;.-")

_SURFACE_TO_CONCEPT: dict[str, str] = {}
for _c, _forms in SYNONYMS.items():
    for _f in _forms:
        _SURFACE_TO_CONCEPT.setdefault(_f.lower(), _c)
_SURFACES_BY_LEN = sorted(_SURFACE_TO_CONCEPT, key=len, reverse=True)


def parse_query(q: str) -> list[tuple[str, list[str]]]:
    """'lactose-free milk' -> [('lactose free', [...forms]), ('milk', ['milk','maito','mjölk'])]."""
    if not isinstance(q, str) or "\x00" in q:
        return []
    cleaned = clean_query(q)
    if not cleaned:
        return []
    s = " " + re.sub(r"[^\w\-%,.']+", " ", cleaned.lower()) + " "
    found: list[tuple[int, str, list[str]]] = []
    for surf in _SURFACES_BY_LEN:
        pat = re.compile(r"(?<![\w])" + re.escape(surf) + r"(?![\w])")
        m = pat.search(s)
        if m:
            c = _SURFACE_TO_CONCEPT[surf]
            if all(c != x[1] for x in found):
                found.append((m.start(), c, SYNONYMS[c]))
            s = pat.sub(lambda mm: " " * len(mm.group(0)), s)
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


def _con() -> sqlite3.Connection:
    global _con_hits, _con_misses
    cur = getattr(_tls, "entry", None)
    with _con_guard:
        epoch = _con_epoch
    if cur is not None and cur[0] == epoch:
        try:
            cur[1].execute("select 1")
            with _con_guard:
                _con_hits += 1
            return cur[1]
        except sqlite3.ProgrammingError:
            pass
    con = connect()
    with _con_guard:
        if _con_epoch == epoch:
            _con_pool.append(con)
        _con_misses += 1
        cur_epoch = _con_epoch
    _tls.entry = (cur_epoch, con)
    return con


def _con_cache_clear() -> None:
    global _con_epoch, _con_hits, _con_misses
    with _con_guard:
        _con_epoch += 1
        old = list(_con_pool)
        _con_pool.clear()
        _con_hits = 0
        _con_misses = 0
    _tls.entry = None
    for c in old:
        try:
            c.close()
        except Exception:
            pass


def _con_cache_info() -> _CacheInfo:
    with _con_guard:
        return _CacheInfo(_con_hits, _con_misses, 1, len(_con_pool))


_con.cache_clear = _con_cache_clear
_con.cache_info = _con_cache_info


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
    with _db_lock:
        rows = [
            (r["category"], r["r"])
            for r in _con().execute("select category, avg(nutrition != '{}') as r from products group by category").fetchall()
        ]
    return frozenset(
        cat
        for cat, ratio in rows
        if isinstance(cat, str) and cat and ratio is not None and ratio < 0.2 and cat[0] in "23679" and not cat.startswith("22")
    )


def food_class(d: dict[str, Any]) -> str:
    """'food' | 'nonfood' | 'unknown' for ranking only (never a safety decision)."""
    if not isinstance(d, dict):
        return "unknown"
    cat = d.get("category")
    if isinstance(cat, str) and cat in _nonfood_categories():
        return "nonfood"
    if d.get("nutrition"):
        return "food"
    name = " ".join(x for x in (d.get("name_en"), d.get("name_fi")) if isinstance(x, str))
    if _NONFOOD_NAME.search(name) and not d.get("ingredients_text"):
        return "nonfood"
    return "food" if d.get("ingredients_text") else "unknown"


def query_wants_nonfood(query: str) -> bool:
    if not isinstance(query, str) or "\x00" in query:
        return False
    return bool(_NONFOOD_QUERY.search(query[:4096]))


def summary_from_row(d: dict[str, Any], score: Optional[float] = None) -> ProductSummary:
    if not isinstance(d, dict):
        raise ValueError("row must be a dict")
    raw_id = d.get("id")
    if raw_id is None or isinstance(raw_id, (bool, list, dict, set, tuple)):
        raise ValueError("row missing valid id")
    pid = str(raw_id).replace("\x00", "").strip()
    if not pid:
        raise ValueError("row missing valid id")
    cu = d.get("content_unit")
    raw_name = d.get("name")
    name = raw_name.replace("\x00", "").strip() if isinstance(raw_name, str) else ""
    raw_su = d.get("sales_unit")
    sales_unit = raw_su.replace("\x00", "") if isinstance(raw_su, str) else ""
    raw_claims = d.get("claims")
    claims = [c for c in raw_claims if isinstance(c, str) and c and "\x00" not in c] if isinstance(raw_claims, (list, tuple, set, frozenset)) else []
    raw_labels = d.get("labels")
    labels = [lb for lb in raw_labels if isinstance(lb, str) and lb and "\x00" not in lb] if isinstance(raw_labels, (list, tuple, set, frozenset)) else []
    rel = _clean_float(d.get("delivery_reliability"), non_negative=True)
    if rel is not None and rel > 1.0:
        rel = None
    return ProductSummary(
        id=pid,
        gtin=_clean_opt_str(d.get("gtin")),
        name=name or pid,
        name_fi=_clean_opt_str(d.get("name_fi")),
        brand=_clean_opt_str(d.get("brand")),
        vendor=_clean_opt_str(d.get("vendor")),
        category=_clean_opt_str(d.get("category")),
        sales_unit=sales_unit,
        content_amount=_clean_float(d.get("content_amount"), positive=True),
        content_unit=cu if isinstance(cu, str) and cu in ("L", "KG", "PCS") else None,
        claims=claims,
        labels=labels,
        delivery_reliability=rel,
        co2_kg_per_kg=_clean_float(d.get("co2_kg_per_kg"), non_negative=True),
        score=_clean_float(score),
    )


def get_rows(ids: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(ids, (list, tuple, set, frozenset)):
        return {}
    clean_ids = list(dict.fromkeys(x for x in ids if isinstance(x, str) and x.strip() and "\x00" not in x and len(x) <= 256))
    if not clean_ids:
        return {}
    raw_rows: list[dict[str, Any]] = []
    with _db_lock:
        con = _con()
        for i in range(0, len(clean_ids), 500):
            chunk = clean_ids[i:i + 500]
            fetched = con.execute(f"select * from products where id in ({','.join('?' * len(chunk))})", chunk).fetchall()
            for r in fetched:
                raw_rows.append(row_to_dict(r))
    out: dict[str, dict[str, Any]] = {}
    for d in raw_rows:
        try:
            from aimo.catalog.data_quality import apply as _dq
            _dq(d)
        except Exception:
            pass
        out[d["id"]] = d
    return out


def _filter_sql(claims: list[str], category: Optional[str], content_unit: Optional[str],
                free_of: Optional[list[str]] = None) -> tuple[str, list]:
    parsed = _parse_filters(claims, category, content_unit, free_of)
    if parsed is None:
        return " and 0 = 1", []
    clean_claims, clean_cat, clean_unit, clean_free = parsed
    where, args = [], []
    for g in clean_free:
        if re.fullmatch(r"[a-z]+", g):
            where.append(f"json_extract(p.allergens, '$.{g}') = 'free'")
    for c in clean_claims:
        where.append("p.id in (select id from claims where claim = ?)")
        args.append(c)
    if clean_cat:
        where.append("p.category = ?")
        args.append(clean_cat)
    if clean_unit:
        where.append("p.content_unit = ?")
        args.append(clean_unit)
    return (" and " + " and ".join(where)) if where else "", args


def fts_search(query: str, *, claims: list[str] | None = None, category: Optional[str] = None,
               content_unit: Optional[str] = None, limit: int = 20, pool: int = 400,
               free_of: Optional[list[str]] = None) -> list[tuple[str, float]]:
    """[(id, score)] best first.

    FTS5 (trigram, all names + brand + ingredients + claim words) recalls up to `pool` rows; then
    we re-score in Python: concept coverage in the product *name* (any language) dominates,
    implied claims add, unrequested product-type modifiers subtract, bm25 breaks ties.
    """
    if not isinstance(query, str) or "\x00" in query:
        return []
    lim = _clean_bound(limit, 1000)
    pool_n = _clean_bound(pool, 5000)
    if lim <= 0 or pool_n <= 0:
        return []
    parsed_filters = _parse_filters(claims, category, content_unit, free_of)
    if parsed_filters is None:
        return []
    clean_claims, clean_cat, clean_unit, clean_free = parsed_filters
    concepts = parse_query(query)
    fsql, fargs = _filter_sql(clean_claims, clean_cat, clean_unit, clean_free)
    bm: dict[str, float] = {}
    with _db_lock:
        con = _con()
        for mode in ("AND", "OR"):
            q = _fts_query(concepts, mode)
            if not q:
                break
            sql = (f"select f.id as id, bm25(fts, 0, 10.0, 10.0, 6.0, 3.0, 1.0, 4.0) as r from fts f "
                   f"join products p on p.id = f.id where fts match ? {fsql} order by r limit ?")
            try:
                fetched = [(r["id"], r["r"]) for r in con.execute(sql, [q, *fargs, pool_n]).fetchall()]
            except sqlite3.OperationalError:
                fetched = []
            for row_id, raw_r in fetched:
                rv = _clean_float(raw_r) or 0.0
                bm.setdefault(row_id, -rv * (1.0 if mode == "AND" else 0.5))
            if len(bm) >= lim or len(concepts) <= 1:
                break
        if not concepts and (clean_claims or clean_cat or clean_unit):
            browse = [r["id"] for r in con.execute(f"select p.id as id from products p where 1=1 {fsql} limit ?", [*fargs, lim]).fetchall()]
            return [(pid, 0.0) for pid in browse]
    if not bm:
        return []
    raw_top = max(bm.values())
    top = raw_top if (math.isfinite(raw_top) and raw_top > 1e-12) else 1.0
    rows = get_rows(list(bm))
    ql = " ".join(f.lower() for _c, forms in concepts for f in forms)
    wants_nonfood = query_wants_nonfood(query)
    results: dict[str, float] = {}
    for pid, d in rows.items():
        names = " ".join(x for x in (d.get("name_en"), d.get("name_fi"), d.get("name_sv")) if isinstance(x, str)).lower()
        hit = sum(1 for _c, forms in concepts if any(f.lower() in names for f in forms))
        score = 10.0 * hit / max(len(concepts), 1)
        raw_en = d.get("name_en") if isinstance(d.get("name_en"), str) and d.get("name_en") else (d.get("name_fi") if isinstance(d.get("name_fi"), str) else "")
        n_en = raw_en.lower()
        extra = sum(1 for m in VARIANT_MARKERS if m in n_en and m.strip() not in ql)
        score -= 3.0 * min(extra, 2)
        d_claims = d.get("claims") if isinstance(d.get("claims"), (list, tuple, set, frozenset)) else ()
        for c, _ in concepts:
            cl = IMPLIED_CLAIMS.get(c)
            if cl and cl in d_claims:
                score += 2.0
        score += 2.0 * bm[pid] / top
        score -= 0.01 * max(0, len(n_en) - 40)
        if not wants_nonfood:
            fc = food_class(d)
            score += 1.0 if fc == "food" else (-8.0 if fc == "nonfood" else 0.0)
        results[pid] = round(score, 4)
    return sorted(results.items(), key=lambda x: -x[1])[:lim]


def semantic_hits(query: str, *, claims: list[str] | None = None, category: Optional[str] = None,
                  content_unit: Optional[str] = None, k: int = 40) -> list[tuple[str, float]]:
    """Gemini-embedding recall with the same filters as FTS. [] if embeddings are not built/available."""
    if not isinstance(query, str) or "\x00" in query or not query.strip():
        return []
    k_n = _clean_bound(k, 500)
    if k_n <= 0:
        return []
    parsed_filters = _parse_filters(claims, category, content_unit)
    if parsed_filters is None:
        return []
    clean_claims, clean_cat, clean_unit, _ = parsed_filters
    try:
        from aimo.catalog.embeddings import available, semantic_search
        from aimo.catalog.netcheck import semantic_enabled
        if not semantic_enabled() or not available():
            return []
        raw = semantic_search(query[:4096], k=k_n * 3)
    except Exception:
        return []
    pairs: list[tuple[str, float]] = []
    for item in raw or []:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        pid, sim = item
        sim_f = _clean_float(sim)
        if isinstance(pid, str) and pid.strip() and "\x00" not in pid and sim_f is not None:
            pairs.append((pid, sim_f))
    if not pairs:
        return []
    rows = get_rows([pid for pid, _ in pairs])
    out = []
    for pid, sim_f in pairs:
        d = rows.get(pid)
        if not d:
            continue
        d_claims = d.get("claims") if isinstance(d.get("claims"), (list, tuple, set, frozenset)) else ()
        if clean_claims and not set(clean_claims) <= set(d_claims):
            continue
        if clean_cat and d.get("category") != clean_cat:
            continue
        if clean_unit and d.get("content_unit") != clean_unit:
            continue
        out.append((pid, sim_f))
        if len(out) >= k_n:
            break
    return out


def search(query: str, *, claims: list[str] | None = None, category: str | None = None,
           content_unit: str | None = None, limit: int = 20, semantic: bool = True) -> list[ProductSummary]:
    """Hybrid search: FTS (exact words, fi/en/sv synonyms) fused with Gemini semantic recall by
    weighted reciprocal rank fusion. FTS that matches every query concept in the name keeps
    priority; semantic recall rescues free-form queries ("dairy-free drink for coffee")."""
    if not isinstance(query, str) or "\x00" in query:
        return []
    lim = _clean_bound(limit, 500)
    if lim <= 0:
        return []
    if _parse_filters(claims, category, content_unit) is None:
        return []
    fts = fts_search(query, claims=claims, category=category, content_unit=content_unit, limit=lim * 2)
    sem = semantic_hits(query, claims=claims, category=category, content_unit=content_unit, k=lim * 2) if bool(semantic) else []
    if not sem:
        hits = fts[:lim]
    else:
        fused: dict[str, float] = {}
        full = {pid for pid, sc in fts if sc >= 9.0}
        for rank, (pid, _sc) in enumerate(fts):
            fused[pid] = fused.get(pid, 0) + 1.0 / (20 + rank) + (0.05 if pid in full else 0)
        for rank, (pid, _sim) in enumerate(sem):
            fused[pid] = fused.get(pid, 0) + 0.8 / (20 + rank)
        if not query_wants_nonfood(query):
            frows = get_rows(list(fused))
            for pid in fused:
                if pid in frows and food_class(frows[pid]) == "nonfood":
                    fused[pid] *= 0.3
        hits = sorted(((pid, sc * 100) for pid, sc in fused.items()), key=lambda x: -x[1])[:lim]
    rows = get_rows([h[0] for h in hits])
    out: list[ProductSummary] = []
    for pid, score in hits:
        if pid not in rows:
            continue
        try:
            out.append(summary_from_row(rows[pid], round(score, 4)))
        except ValueError:
            continue
    return out
