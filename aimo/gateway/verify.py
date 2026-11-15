"""Server-side line verification at quote time. Owner: gateway.

The quoting agent is untrusted. Its `tier` and `flags` are kept only as `provenance.client_claims`.
The gateway derives both itself from:
  - the customer's usual products, held server-side (trust policy `preferred_products` or
    `standing_orders[].product_id(s)`; until trust carries them, the registry below);
  - aimo.catalog's substitution ranking (same rules as plan_need: same brand+spec = best,
    same spec = better, else good; claim/allergen/type mismatches = rejected);
  - live offers (cut-off, availability, stock vs quantity);
  - trust's supplier-text screen (prompt injection).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterable

from aimo.catalog.allergens import (
    ALLERGEN_ALIASES,
    CODE_TO_GROUP,
    blocking,
    normalize_allergens,
)
from aimo.catalog.screening_bridge import screen_reason
from aimo.models import Need, Offer, Product

log = logging.getLogger("aimo.gateway.verify")

USUAL_PRODUCTS: dict[str, list[str]] = {
    "hotel-aurora": ["463038"],
    "school-tampere": [],
    "hospital-meilahti": [],
}

DIETARY_CLAIMS = {"FREE_FROM_LACTOSE", "LOW_LACTOSE", "FREE_FROM_GLUTEN", "GLUTEN_FREE", "FREE_FROM_MILK",
                  "VEGAN"}

INFO_ISSUES = ("limited_stock", "terminal_unknown_qty")
DERIVED_FLAGS = ("cutoff_passed", "unavailable", "sales_block", "allergen_unknown", "outside_preference",
                 "allergen_inferred_hint", "allergen_data_conflict", "content_from_price", "no_live_offer",
                 "allergen_safe_alternative")

_GS1_SHORT_CODES = {c.lower() for c in CODE_TO_GROUP}
_PRODUCT_ALLERGEN_TERMS: dict[str, tuple[str, ...]] = {
    "butter": ("milk",),
    "voi": ("milk",),
    "voita": ("milk",),
    "smör": ("milk",),
    "cream": ("milk",),
    "kerma": ("milk",),
    "kermaa": ("milk",),
    "kuohukerma": ("milk",),
    "ruokakerma": ("milk",),
    "vispikerma": ("milk",),
    "grädde": ("milk",),
    "vispgrädde": ("milk",),
    "matlagningsgrädde": ("milk",),
    "gräddfil": ("milk",),
    "cheese": ("milk",),
    "juusto": ("milk",),
    "juustoa": ("milk",),
    "ost": ("milk",),
    "whey": ("milk",),
    "hera": ("milk",),
    "vassle": ("milk",),
    "buttermilk": ("milk",),
    "sour milk": ("milk",),
    "piimä": ("milk",),
    "piimää": ("milk",),
    "filmjölk": ("milk",),
    "yoghurt": ("milk",),
    "yogurt": ("milk",),
    "jogurtti": ("milk",),
    "jogurttia": ("milk",),
    "quark": ("milk",),
    "rahka": ("milk",),
    "rahkaa": ("milk",),
    "kvarg": ("milk",),
}
_QUERY_ALLERGEN_TERMS: dict[str, tuple[str, ...]] = {
    **{k: v for k, v in ALLERGEN_ALIASES.items() if k not in _GS1_SHORT_CODES and len(k) >= 3},
    **_PRODUCT_ALLERGEN_TERMS,
}
_FREE_CLAIM_RE = re.compile(
    r"\b(?:gluten|lactose|milk|dairy|nut|nuts|tree\s+nuts?|egg|eggs|soy|soya|wheat|peanut|peanuts|sesame|fish)"
    r"\s*-?\s*free\b"
    r"|\b(?:gluteeniton\w*|glutenfri\w*|laktoositon\w*|laktosfri\w*|maidoton\w*|maidottom\w*|"
    r"mjölkfri\w*|pähkinätön\w*|pähkinättöm\w*|nötfri\w*|maapähkinätön\w*|jordnötsfri\w*|"
    r"munaton\w*|kananmunaton\w*|äggfri\w*|soijaton\w*|sojafri\w*|seesamiton\w*|sesamfri\w*)\b",
    re.IGNORECASE,
)
_GROUP_FREE_CLAIM_RES: dict[str, re.Pattern[str]] = {
    "milk": re.compile(
        r"\b(?:(?:cow'?s\s+)?(?:milk|dairy|maito|mjölk)\s*-?\s*free|maidoton\w*|maidottom\w*|mjölkfri\w*"
        r"|(?:no|without|free\s+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:cow'?s\s+)?(?:milk|dairy|maito\w*|mjölk\w*))\b",
        re.IGNORECASE,
    ),
    "gluten": re.compile(
        r"\b(?:(?:gluten|gluteeni|wheat)\s*-?\s*free|gluteenit\w*|glutenfri\w*"
        r"|(?:no|without|free\s+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:gluten\w*|gluteen\w*))\b",
        re.IGNORECASE,
    ),
    "nuts": re.compile(
        r"\b(?:(?:tree\s+)?nuts?\s*-?\s*free|pähkinätön\w*|pähkinättöm\w*|nötfri\w*"
        r"|(?:no|without|free\s+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:tree\s+)?(?:nuts?|pähkin\w*|nötter\w*))\b",
        re.IGNORECASE,
    ),
    "peanuts": re.compile(
        r"\b(?:peanuts?\s*-?\s*free|maapähkinätön\w*|jordnötsfri\w*"
        r"|(?:no|without|free\s+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:peanuts?|maapähkin\w*|jordnöt\w*))\b",
        re.IGNORECASE,
    ),
    "eggs": re.compile(
        r"\b(?:eggs?\s*-?\s*free|munaton\w*|kananmunaton\w*|äggfri\w*"
        r"|(?:no|without|free\s+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:eggs?|kananmun\w*|munia|ägg\w*))\b",
        re.IGNORECASE,
    ),
    "soy": re.compile(
        r"\b(?:soy(?:a)?\s*-?\s*free|soijaton\w*|sojafri\w*"
        r"|(?:no|without|free\s+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:soy(?:a)?|soija\w*|soja\w*))\b",
        re.IGNORECASE,
    ),
    "sesame": re.compile(
        r"\b(?:sesame\s*-?\s*free|seesamiton\w*|sesamfri\w*"
        r"|(?:no|without|free\s+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:sesame|seesam\w*|sesam\w*))\b",
        re.IGNORECASE,
    ),
}
_PEANUT_BUTTER_RE = re.compile(
    r"\b(?:peanuts?\s+(?:butter|voi\w*|smör\w*)|maapähkinävoi\w*|jordnötssmör\w*)\b",
    re.IGNORECASE,
)
_TREENUT_BUTTER_RE = re.compile(
    r"\b(?:(?:almonds?|cashews?|hazelnuts?|walnuts?|pistachios?|pecans?|macadamias?)\s+(?:butter|voi\w*|smör\w*)"
    r"|mantelivoi\w*|mandelsmör\w*|hasselpähkinävoi\w*|hasselnötssmör\w*|cashewpähkinävoi\w*|cashewvoi\w*"
    r"|cashewsmör\w*|saksanpähkinävoi\w*|pistaasivoi\w*)\b",
    re.IGNORECASE,
)
_SOY_PLANT_DAIRY_RE = re.compile(
    r"\b(?:soy(?:a)?\s+(?:milk|cream|yogh?urt|drink|cheese|butter)|soijamaito\w*|soijajuoma\w*|soijakerma\w*"
    r"|soijajogurtti\w*|sojamjölk\w*|sojadryck\w*|sojagrädde\w*|sojayoghurt\w*)\b",
    re.IGNORECASE,
)
_TREENUT_PLANT_DAIRY_RE = re.compile(
    r"\b(?:(?:almonds?|cashews?|hazelnuts?|walnuts?)\s+(?:milk|cream|yogh?urt|drink)|mantelimaito\w*"
    r"|mantelijuoma\w*|mandelmjölk\w*|mandeldryck\w*|hasselpähkinäjuoma\w*|hasselnötsdryck\w*"
    r"|cashewmaito\w*|cashewjuoma\w*)\b",
    re.IGNORECASE,
)
_NON_ALLERGEN_FALSE_FRIEND_RE = re.compile(
    r"\b(?:coconut\s*(?:milk|cream|butter|yogh?urt|water|oil)?|kookos\w*|kokos\w*"
    r"|(?:cocoa|kakao|kaakao|shea)\s*(?:butter|voi\w*|smör\w*)|kaakaovoi\w*|kakaosmör\w*|sheavoi\w*|sheasmör\w*"
    r"|(?:oat|oats|rice|plant|vegan)\s+(?:milk|cream|butter|yogh?urt|cheese)"
    r"|kauramaito\w*|kaurakerma\w*|kaurajogurtti\w*|kauravoi\w*"
    r"|havremjölk\w*|havregrädde\w*|havreyoghurt\w*|havresmör\w*"
    r"|riisimaito\w*|riisikerma\w*|rismjölk\w*|risgrädde\w*|kasvirasva\w*|kasvikerma\w*"
    r"|buckwheat\w*|tattari\w*|bovete\w*|ruiskauno\w*"
    r"|nutmeg\w*|muskottipähkinä\w*|muskotnöt\w*|muskot\w*|butternut\w*|butter\s+beans?|pine\s+nuts?"
    r"|pinjansiemen\w*|pinjenöt\w*|water\s+chestnuts?"
    r"|eggplants?|munakoiso\w*|äggplanta\w*"
    r"|oyster\s+mushrooms?|osterivinok\w*|ostronskivling\w*"
    r"|cream\s+of\s+tartar|lactic\s+acid|maitohap\w*|mjölksyr\w*|juustokumina\w*"
    r"|nöt(?:kött|färs|stek|buljong|bog|filé)\w*)\b",
    re.IGNORECASE,
)
_MULTILINGUAL_GROUP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "milk",
        re.compile(
            r"(?:\b(?:(?:kevyt|täys|rasvaton|luomu)?maito(?:a|juoma\w*)?|maitotuot\w*|maitojauhe\w*"
            r"|(?:lätt|hel|standard|mellan|eko)?mjölk(?:dryck\w*|pulver\w*)?"
            r"|(?:kuohu|ruoka|vispi|kahvi)?kerma(?:a)?|(?:visp|matlagnings|kaffe)?grädde|gräddfil\w*"
            r"|voi(?:ta|sula\w*|rasva\w*)?|smör\b|(?:färsk|smält|hård|get|mögel|grädd)?ost\b|hera\w*|vassle\w*"
            r"|piimä\w*|filmjölk\w*|jogurtti\w*|rahka\w*|kvarg\w*|buttermilk)\b|\w*juusto\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "gluten",
        re.compile(
            r"\b(?:gluteeni\w*|gluten\w*|vehnä\w*|ruis\w*|ohra\w*|speltti\w*|vete\w*|råg\w*|korn(?:mjöl\w*|gryn\w*)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "peanuts",
        re.compile(
            r"\b(?:peanuts?|groundnuts?|maapähkin(?:ä\w*|öi\w*)|jordnöt(?:ter\w*|s\w*)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "nuts",
        re.compile(
            r"\b(?:(?:tree\s+)?nuts?|(?:saksan|hassel|cashew|pekaani|para)?pähkin(?:ä\w*|öi\w*)"
            r"|mantel(?:i\w*|ei\w*)|pistaasi\w*"
            r"|(?:hassel|val|cashew|pekan|para|macadamia)?nöt(?:ter\w*)?"
            r"|mand(?:el\w*|lar\w*)|pistasch\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "eggs",
        re.compile(
            r"\b(?:eggs?|(?:kanan|kana)?mun(?:a\w*|ia)|ägg(?:ula\w*|vita\w*)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "soy",
        re.compile(r"\b(?:soy(?:a)?|soybeans?|soija\w*|soja\w*|tofu\w*|edamame)\b", re.IGNORECASE),
    ),
    (
        "fish",
        re.compile(
            r"\b(?:fish|salmon|tuna|cod|herring|kala(?:a)?|lohi|lohta|tonnikala\w*|silakka\w*"
            r"|fisk\w*|lax\w*|torsk\w*|sill\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sesame",
        re.compile(r"\b(?:sesame(?:\s+seeds?)?|tahini|seesam\w*|sesam\w*)\b", re.IGNORECASE),
    ),
    ("celery", re.compile(r"\b(?:celery|celeriac|selleri\w*)\b", re.IGNORECASE)),
    ("mustard", re.compile(r"\b(?:mustard|sinappi\w*|senap\w*)\b", re.IGNORECASE)),
    ("lupin", re.compile(r"\b(?:lupin(?:s|e)?|lupiini\w*)\b", re.IGNORECASE)),
    (
        "crustaceans",
        re.compile(r"\b(?:crustaceans?|shellfish|shrimps?|prawns?|äyriäi\w*|katkarap\w*|kräftdjur\w*|räk\w*)\b", re.IGNORECASE),
    ),
    (
        "molluscs",
        re.compile(r"\b(?:mollus[ck]s?|shellfish|mussels?|nilviäi\w*|simpuk\w*|blötdjur\w*)\b", re.IGNORECASE),
    ),
    (
        "sulphites",
        re.compile(r"\b(?:sulphites?|sulfites?|sulphur\s+dioxide|sulfur\s+dioxide|sulfiit\w*|rikkidioksidi\w*|sulfit\w*)\b", re.IGNORECASE),
    ),
)
_last_trust_policy_dir: Any = None


def _policy(customer_id: str) -> dict:
    global _last_trust_policy_dir
    try:
        from aimo.trust import load_policy
        from aimo.trust import policy as trust_policy

        if trust_policy.POLICY_DIR != _last_trust_policy_dir:
            if hasattr(trust_policy, "_load_policy_cached"):
                trust_policy._load_policy_cached.cache_clear()
            _last_trust_policy_dir = trust_policy.POLICY_DIR
        return load_policy(customer_id) or {}
    except Exception:
        return {}


_default_policy = _policy


def usual_products(customer_id: str) -> list[str]:
    pol = _policy(customer_id)
    ids: list[str] = []
    for x in pol.get("preferred_products") or []:
        ids.append(str(x.get("product_id") if isinstance(x, dict) else x))
    for so in pol.get("standing_orders") or []:
        for k in ("product_id", "product_ids"):
            v = so.get(k)
            if v:
                ids.extend(str(i) for i in (v if isinstance(v, list) else [v]))
    if not ids:
        ids = list(USUAL_PRODUCTS.get(customer_id, []))
    return list(dict.fromkeys(i for i in ids if i and i != "None"))


def policy_excluded_allergens(customer_id: str) -> list[str]:
    pol = _policy(customer_id or "")
    out: list[str] = []
    has_allergen_block = False
    for r in pol.get("rules") or []:
        if isinstance(r, dict) and r.get("type") == "allergen_block":
            has_allergen_block = True
            out.extend(r.get("excluded") or [])
    if out or (has_allergen_block and _policy is not _default_policy):
        return normalize_allergens(out)
    cid = (customer_id or "").strip()
    if not cid:
        return []
    try:
        from aimo.trust import policy as trust_policy

        if hasattr(trust_policy, "planning_profile"):
            prof = trust_policy.planning_profile(cid) or {}
            if prof.get("excluded_allergens"):
                return normalize_allergens(prof.get("excluded_allergens") or [])
    except Exception:
        pass
    return []


_LITERAL_MILK_RE = re.compile(
    r"\b(?:milk|dairy|(?:kevyt|täys|rasvaton|luomu)?maito(?:a|juoma\w*)?|"
    r"(?:lätt|hel|standard|mellan|eko)?mjölk(?:dryck\w*)?)\b",
    re.IGNORECASE,
)
_LITERAL_GLUTEN_RE = re.compile(r"\b(?:gluten|gluteeni\w*)\b", re.IGNORECASE)


def excluded_allergens_in_query(query: str, excluded: Iterable[str]) -> list[str]:
    excl_list = normalize_allergens(excluded)
    excl = set(excl_list)
    if not excl or not query or not str(query).strip():
        return []
    norm_q = re.sub(r"\u2019", "'", str(query))
    suppressed = {g for g, rx in _GROUP_FREE_CLAIM_RES.items() if rx.search(norm_q)}
    try:
        from aimo.catalog.textsearch import clean_query

        cleaned = clean_query(norm_q)
    except Exception:
        cleaned = norm_q
    low = _FREE_CLAIM_RE.sub(" ", cleaned).lower()
    low = _PEANUT_BUTTER_RE.sub(" peanut ", low)
    low = _TREENUT_BUTTER_RE.sub(" almond ", low)
    low = _SOY_PLANT_DAIRY_RE.sub(" soy ", low)
    low = _TREENUT_PLANT_DAIRY_RE.sub(" almond ", low)
    low = _NON_ALLERGEN_FALSE_FRIEND_RE.sub(" ", low)
    low = re.sub(r"\s+", " ", low).strip()
    if not low:
        return []
    if "milk" in suppressed and _LITERAL_MILK_RE.search(low):
        suppressed.discard("milk")
    if "gluten" in suppressed and _LITERAL_GLUTEN_RE.search(low):
        suppressed.discard("gluten")
    hit: list[str] = []
    try:
        from aimo.catalog.plan import _excluded_groups_named

        lit, imp = _excluded_groups_named(low, excl_list)
        for g in [*lit, *imp]:
            if g in excl and g not in suppressed and g not in hit:
                hit.append(g)
    except Exception:
        pass
    for phrase, groups in _QUERY_ALLERGEN_TERMS.items():
        matched = [g for g in groups if g in excl and g not in suppressed]
        if matched and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", low):
            for g in matched:
                if g not in hit:
                    hit.append(g)
    for group, rx in _MULTILINGUAL_GROUP_PATTERNS:
        if group in excl and group not in suppressed and group not in hit and rx.search(low):
            hit.append(group)
    return [g for g in excl_list if g in hit] if len(hit) > 1 else hit


def need_names_excluded_allergen(need: Need | None, excluded: Iterable[str]) -> bool:
    if need is None:
        return False
    return bool(excluded_allergens_in_query(need.product_query or "", excluded))


def usual_matches_allergen_query(query: str, u_row: dict[str, Any]) -> bool:
    if not query or not isinstance(u_row, dict):
        return False
    raw_q = re.sub(r"\u2019", "'", str(query))
    if (
        _PEANUT_BUTTER_RE.search(raw_q)
        or _TREENUT_BUTTER_RE.search(raw_q)
        or _NON_ALLERGEN_FALSE_FRIEND_RE.search(raw_q)
    ):
        return False
    try:
        from aimo.catalog.textsearch import clean_query

        cleaned_q = clean_query(raw_q)
    except Exception:
        cleaned_q = raw_q
    q_low = (cleaned_q or raw_q).lower()
    u_name = " ".join(str(u_row.get(k) or "") for k in ("name", "name_en", "name_fi", "name_sv")).lower()
    if _LITERAL_MILK_RE.search(q_low) and not re.search(
        r"(?:\b(?:butter|buttermilk|voi(?:ta)?\b|smör\b|cheese|(?:färsk|smält|hård|get|mögel|grädd)?ost\b"
        r"|cream|kerma\w*|grädde\w*|gräddfil\w*|yogh?urt|jogurtti\w*|sour\s*milk|piimä\w*|filmjölk\w*"
        r"|quark|rahka\w*|kvarg\w*|whey|hera\w*|vassle\w*|powder|jauhe\w*|maitojauhe\w*|mjölkpulver\w*"
        r"|condensed|kondensoitu\w*|ice\s*cream|jäätelö\w*|glass\b|chocolate|suklaa\w*|choklad\w*"
        r"|cocoa|kaakao\w*|kakao\w*|coffee|kahvi\w*|kaffe\w*|latte\b|coconut\w*|kookos\w*|kokos\w*)\b"
        r"|\w*juusto\w*)",
        q_low,
    ):
        return bool(
            u_row.get("content_unit") == "L"
            and re.search(r"\b(?:drink|juoma|dryck|oat|\w*kaura\w*|\w*havre\w*|soy|\w*soija\w*|\w*soja\w*)\b", u_name)
        )
    if re.search(r"(?:\b(?:bread|gluten|gluteeni\w*)\b|\w*leip(?:ä\w*|ää)\b|\w*bröd\w*)", q_low) and not re.search(
        r"(?:\b(?:flour|pasta\w*|spaghetti|macaroni|makaroni\w*|noodle\w*|nuudeli\w*|breadcrumbs?|crumbs?"
        r"|panko|croutons?|kruton\w*|porridge|oatmeal|puuro\w*|gröt\w*|flakes|hiutale\w*|flingor\w*"
        r"|muesli|mysli\w*|müsli\w*|cereals?|cheese|oat\s+drink|kaurajuoma\w*|havredryck\w*)\b"
        r"|\w*juusto\w*|\w*jauho\w*|\w*mjöl\b|ströbröd\w*)",
        q_low,
    ):
        return bool(
            u_row.get("content_unit") in ("KG", "PCS", None)
            and re.search(r"(?:\bbread\b|\w*leip(?:ä\w*|ää)\b|\w*bröd\w*)", u_name)
            and not re.search(r"(?:crumb|korppujauho|ströbröd|juusto)", u_name)
        )
    return False


def _same_kind(a: dict[str, Any], b: dict[str, Any], product_types: Callable) -> bool:
    """A usual product is the reference for a line when both are the same kind of product."""
    ta, tb = product_types(a), product_types(b)
    if ta and tb:
        return bool(ta & tb)
    return bool(a.get("category")) and a.get("category") == b.get("category")


def _record_allergen_blocks(
    record: Any,
    excluded: list[str],
) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, dict[str, str]]]:
    if not excluded:
        return [], {}, {}
    if record is None:
        return blocking({}, excluded), {}, {}
    if isinstance(record, dict):
        raw_allergens = dict(record.get("allergens") or {})
        raw_source = dict(record.get("allergen_source") or {})
        claims = list(record.get("claims") or [])
        ing_en = record.get("ingredients_text")
        ing_fi = record.get("ingredients_fi")
        rec_id = str(record.get("id") or "")
        rec_conflicts = dict(record.get("allergen_conflicts") or {})
    else:
        raw_allergens = dict(getattr(record, "allergens", None) or {})
        raw_source = dict(getattr(record, "allergen_source", None) or {})
        claims = list(getattr(record, "claims", None) or [])
        ing_en = getattr(record, "ingredients_text", None)
        ing_fi = getattr(record, "ingredients_fi", None)
        rec_id = str(getattr(record, "id", "") or "")
        rec_conflicts = {}
    if not rec_conflicts and (ing_en or ing_fi):
        try:
            from aimo.catalog.data_quality import conflicts as _dq_conflicts

            rec_conflicts = _dq_conflicts({
                "id": rec_id,
                "allergens": raw_allergens,
                "claims": claims,
                "ingredients_text": ing_en,
                "ingredients_fi": ing_fi,
            })
        except Exception:
            rec_conflicts = {}
    prod_allergens = dict(raw_allergens)
    prod_source = dict(raw_source)
    for g in rec_conflicts:
        prod_allergens[g] = "unknown"
        prod_source[g] = "contradiction"
    for g in excluded:
        src_g = str(prod_source.get(g) or "").strip()
        st_g = prod_allergens.get(g, "unknown")
        if hasattr(st_g, "value"):
            st_g = st_g.value
        if (src_g.startswith("contradiction") or src_g == "none") and st_g == "free":
            prod_allergens[g] = "unknown"
    blk = blocking(prod_allergens, excluded, allergen_source=prod_source)
    return blk, prod_source, rec_conflicts


def verify_lines(
    customer_id: str,
    delivery_date,
    qty_by: dict[str, float],
    offers: dict[str, Offer],
    need: Need | None,
    products: dict[str, Product],
    screen: Callable[[Product], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """{product_id: {"tier", "flags", "reasons", "method"}} for every product in qty_by."""
    usual = usual_products(customer_id)
    caller_excl = list(need.excluded_allergens) if need else []
    excluded = normalize_allergens([*caller_excl, *policy_excluded_allergens(customer_id)])
    names_excluded = need_names_excluded_allergen(need, excluded)
    out: dict[str, dict[str, Any]] = {}
    rows: dict[str, dict[str, Any]] = {}
    rank = None
    try:
        from aimo.catalog.substitution import product_types, rank_substitutes
        from aimo.catalog.textsearch import get_rows

        rows = get_rows(list(dict.fromkeys([*qty_by, *usual])))
        rank = rank_substitutes
    except Exception as e:
        log.warning("catalog ranking unavailable for quote verification: %s", e)

    blk_sev = {"contains": 3, "may_contain": 2, "unknown": 1}
    for pid, qty in qty_by.items():
        o = offers.get(pid)
        flags: list[str] = []
        reasons: list[str] = []
        tier = "preferred" if pid in usual else None
        method = "usual_products" if tier else "fallback"
        reference: str | None = None
        row = rows.get(pid)
        row_blk, row_src, row_conflicts = (
            _record_allergen_blocks(row, excluded) if (row is not None and excluded) else ([], {}, {})
        )
        p = products.get(pid)
        if excluded:
            if p is not None:
                p_blk, p_src, p_conflicts = _record_allergen_blocks(p, excluded)
            elif row is None:
                p_blk, p_src, p_conflicts = _record_allergen_blocks(None, excluded)
            else:
                p_blk, p_src, p_conflicts = [], {}, {}
        else:
            p_blk, p_src, p_conflicts = [], {}, {}
        if rank is not None and row is not None:
            same_kind = [
                u for u in usual
                if u in rows
                and u != pid
                and not (
                    (_record_allergen_blocks(rows[u], excluded)[0] if excluded else [])
                    or (_record_allergen_blocks(products[u], excluded)[0] if (excluded and u in products) else [])
                )
                and _same_kind(rows[u], row, product_types)
            ]
            reference = same_kind[0] if same_kind else None
            if need is not None:
                eff_query = (
                    (row.get("name") or pid)
                    if (names_excluded and not row_blk and not p_blk and not same_kind)
                    else need.product_query
                )
                ln_need = need.model_copy(update={
                    "customer_id": customer_id, "product_query": eff_query, "preferred_product_ids": same_kind,
                    "preferences": {}, "excluded_allergens": excluded, "delivery_by": delivery_date})
            else:
                ref = rows.get(same_kind[0]) if same_kind else None
                claims = sorted(DIETARY_CLAIMS & set((ref or row).get("claims") or []))
                unit = row.get("content_unit") if row.get("content_unit") in ("L", "KG", "PCS") else "PCS"
                ln_need = Need(customer_id=customer_id, product_query=row.get("name") or pid, required_claims=claims,
                               excluded_allergens=excluded, quantity=qty * float(row.get("content_amount") or 1),
                               unit=unit, delivery_by=delivery_date, preferred_product_ids=same_kind)
            try:
                cands = rank(ln_need, {pid: o} if o is not None else {}, limit=50,
                             rows={k: rows[k] for k in [pid, *same_kind]}, relevance={pid: 1.0})
                c = next((c for c in cands if c.product.id == pid), None)
            except Exception as e:
                log.warning("rank_substitutes failed for %s: %s", pid, e)
                c = None
            if c is not None:
                if pid not in usual or (c.tier == "rejected" and (row_blk or p_blk)):
                    tier, method = c.tier, "catalog.rank_substitutes"
                flags += [f for f in c.flags if f in DERIVED_FLAGS or f.startswith(("contains:", "may_contain:"))]
                reasons += c.reasons[:3]
        seen_blk: dict[str, str] = {}
        seen_src: dict[str, str] = {}
        for blk_list, src_map in ((p_blk, p_src), (row_blk, row_src)):
            for g, st in blk_list:
                if g not in seen_blk or blk_sev.get(st, 1) > blk_sev.get(seen_blk[g], 1):
                    seen_blk[g] = st
                    seen_src[g] = str(src_map.get(g) or "").strip()
                elif not seen_src.get(g) and src_map.get(g):
                    seen_src[g] = str(src_map.get(g) or "").strip()
        combined_blk = [(g, seen_blk[g]) for g in dict.fromkeys([*excluded, *seen_blk]) if g in seen_blk]
        if combined_blk:
            tier = "rejected"
            if method == "usual_products":
                method = "fallback"
            flags = [f for f in flags if f != "allergen_safe_alternative"]
            if (
                p_conflicts
                or row_conflicts
                or any(
                    (seen_src.get(g) or str((p_src.get(g) if g in p_src else row_src.get(g)) or "").strip()).startswith(
                        "contradiction"
                    )
                    for g, _ in combined_blk
                )
            ) and "allergen_data_conflict" not in flags:
                flags.append("allergen_data_conflict")
            for g, st in combined_blk:
                src_g = seen_src.get(g) or str((p_src.get(g) if g in p_src else row_src.get(g)) or "").strip()
                conf_g = p_conflicts.get(g) or row_conflicts.get(g)
                if st == "unknown":
                    if "allergen_unknown" not in flags:
                        flags.append("allergen_unknown")
                    if src_g.startswith("inferred") and "allergen_inferred_hint" not in flags:
                        flags.append("allergen_inferred_hint")
                elif st in ("contains", "may_contain"):
                    if f"{st}:{g}" not in flags:
                        flags.append(f"{st}:{g}")
                    if src_g.startswith("inferred") and "allergen_inferred_hint" not in flags:
                        flags.append("allergen_inferred_hint")
                if not any(r.startswith(f"{g}:") for r in reasons):
                    if conf_g:
                        ev = str(conf_g.get("evidence") or "")[:60]
                        if ev:
                            reasons.append(
                                f"{g}: supplier data says free from {g} but the ingredients say "
                                f"'{ev}' (data conflict; treated as unknown)"
                            )
                        else:
                            reasons.append(f"{g}: conflicting allergen data (treated as unknown, never safe)")
                    elif src_g.startswith("contradiction"):
                        reasons.append(f"{g}: conflicting allergen data ({src_g}; treated as unknown, never safe)")
                    elif st == "unknown":
                        if src_g.startswith("inferred"):
                            reasons.append(f"{g}: no structured allergen data ({src_g}; unknown is never treated as safe)")
                        elif p is None and row is None:
                            reasons.append(
                                f"{g}: product missing from catalog; allergen status unknown (unknown is never treated as safe)"
                            )
                        else:
                            reasons.append(f"{g}: no structured allergen data (unknown is never treated as safe)")
                    elif st in ("contains", "may_contain"):
                        if src_g.startswith("inferred"):
                            reasons.append(f"{g}: {st.replace('_', ' ')} ({src_g})")
                        else:
                            reasons.append(f"{g}: {st.replace('_', ' ')} per supplier data")
                    else:
                        reasons.append(f"{g}: {st.replace('_', ' ')} blocks excluded allergen")
        if tier is None:
            tier = "good"
            flags.append("tier_unverified")
            reasons.append("Product could not be classified against the catalog; treated as a substitute (good).")
        if method == "fallback" and usual and pid not in usual:
            flags.append("outside_preference")
        if o is not None:
            if o.cutoff_passed:
                flags.append("cutoff_passed")
            if o.sales_block:
                flags.append("sales_block")
            if not o.available:
                flags.append("unavailable")
            if o.available_amount is not None and o.available_amount < qty:
                flags.append("partial_stock")
            flags += [i for i in o.issues if i in INFO_ISSUES or i.startswith("injected:")]
        if names_excluded and tier != "rejected" and "allergen_unknown" not in flags and not combined_blk:
            flags.append("allergen_safe_alternative")
        injection_screen: dict[str, Any] | None = None
        if p is not None:
            sc = screen(p)
            injection_screen = {"suspicious": bool(sc.get("suspicious")), "reasons": list(sc.get("reasons") or [])}
            if sc.get("suspicious"):
                flags.append("supplier_text_injection_suspected")
                if "injected:inject_text" in (p.labels or []):
                    flags.append("injected:inject_text")
                sr = screen_reason(sc)
                if sr and sr not in reasons:
                    reasons.append(sr)
        out[pid] = {"tier": tier, "flags": list(dict.fromkeys(flags)), "reasons": reasons, "method": method,
                    "reference": reference, "injection_screen": injection_screen}
    return out
