"""Independent, minimal reader of the raw Valio product JSON.

Used by the oracle (ground-truth labels) and by the naive baseline. It is deliberately NOT the catalog
team's normalizer: an independent re-implementation lets the wind tunnel catch bugs in the system under test.

Allergen codes are GS1 AllergenTypeCode values as they appear in
synkkaData.classifications[name=allergen|nonAllergen].values[].id with unit CONTAINS / MAY_CONTAIN / FREE_FROM
(verified by counting the data: AM 3160x CONTAINS, AW, UW, AE, ...). Mapping to EU14 groups:
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from aimo.config import PRODUCT_JSON, RELIABILITY_CSV, CO2_CSV
from aimo.models import EU14

# GS1 AllergenTypeCode -> EU14 key. Cereal and nut sub-codes roll up to their group.
GS1_TO_EU14: dict[str, str] = {
    "AW": "gluten", "UW": "gluten", "NR": "gluten", "GB": "gluten", "GO": "gluten", "GS": "gluten", "GK": "gluten",
    # GO (oats) is treated as gluten unless the product carries the FREE_FROM_GLUTEN claim (gluten-free oats).
    "AC": "crustaceans", "AE": "eggs", "AF": "fish", "AP": "peanuts", "AY": "soy",
    "AM": "milk", "ML": "milk",  # ML = lactose: CONTAINS lactose implies milk; FREE_FROM lactose does NOT imply milk-free
    "AN": "nuts", "SA": "nuts", "SH": "nuts", "SC": "nuts", "SW": "nuts", "ST": "nuts", "SP": "nuts",
    "SM": "nuts", "SR": "nuts",
    "BC": "celery", "BM": "mustard", "AS": "sesame", "AU": "sulphites", "NL": "lupin", "UM": "molluscs",
}
# Only the umbrella code proves FREE_FROM for the whole group.
GROUP_FREE_CODE = {"gluten": "AW", "nuts": "AN", "milk": "AM"}


@dataclass
class P:
    id: str
    name: str
    name_fi: str
    brand: str
    vendor: str
    category: str
    sales_unit: str
    claims: set[str]
    allergens: dict[str, str]           # EU14 key -> contains | may_contain | free | unknown (structured + ingredients)
    has_allergen_data: bool
    ingredients: str
    marketing: str
    name_amount: Optional[float]        # litres or kg parsed from the name (per base unit; see content())
    name_unit: Optional[str]            # "L" | "KG"
    structured_allergens: dict[str, str] = field(default_factory=dict)
    allergen_conflicts: list[str] = field(default_factory=list)   # structured says free, ingredients disagree
    reliability: Optional[float] = None
    co2: Optional[float] = None
    search_blob: str = field(default="", repr=False)


def _lang(items: list[dict] | None, lang: str) -> str:
    for it in items or []:
        if it.get("language") == lang:
            return it.get("value") or ""
    return ""


_AMT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml)\b", re.I)
_MULTI = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml)\b", re.I)
_FACT = {"kg": (1.0, "KG"), "g": (0.001, "KG"), "l": (1.0, "L"), "dl": (0.1, "L"), "cl": (0.01, "L"), "ml": (0.001, "L")}


def parse_amount(name: str) -> tuple[Optional[float], Optional[str]]:
    m = _MULTI.search(name)
    if m:
        f, u = _FACT[m.group(3).lower()]
        return int(m.group(1)) * float(m.group(2).replace(",", ".")) * f, u
    m = _AMT.search(name)
    if m:
        f, u = _FACT[m.group(2).lower()]
        return float(m.group(1).replace(",", ".")) * f, u
    return None, None


# Ingredient cross-check: EU FIC requires allergens to be emphasised; in this data they are UPPERCASE words.
ING_WORDS = {
    "nuts": ["ALMOND", "ALMONDS", "HAZELNUT", "HAZELNUTS", "CASHEW", "CASHEWS", "WALNUT", "WALNUTS", "PECAN",
             "PISTACHIO", "PISTACHIOS", "MACADAMIA", "MANTELI", "HASSELPÄHKINÄ", "CASHEWPÄHKINÄ", "SAKSANPÄHKINÄ"],
    "milk": ["MILK", "CREAM", "BUTTER", "WHEY", "LACTOSE", "CHEESE", "MILK PROTEIN", "SKIMMED MILK", "MAITO", "KERMA",
             "VOI", "HERA", "JUUSTO", "MAITOPROTEIINI"],
    "gluten": ["WHEAT", "RYE", "BARLEY", "SPELT", "GLUTEN", "VEHNÄ", "RUIS", "OHRA", "SPELTTI"],
    "eggs": ["EGG", "EGGS", "EGG YOLK", "KANANMUNA", "MUNA"],
    "soy": ["SOY", "SOYA", "SOIJA"],
    "peanuts": ["PEANUT", "PEANUTS", "MAAPÄHKINÄ"],
    "sesame": ["SESAME", "SEESAMI"],
    "mustard": ["MUSTARD", "SINAPPI", "SINAPINSIEMEN"],
    "celery": ["CELERY", "SELLERI"],
    "fish": ["FISH", "KALA"],
}
_ING_RX = {k: re.compile(r"(?<![A-ZÄÖÅ])(" + "|".join(map(re.escape, v)) + r")(?![A-ZÄÖÅ])") for k, v in ING_WORDS.items()}
_MAY_RX = re.compile(r"(may contain|traces? of|remains? from|voi sisältää|saattaa sisältää)", re.I)


def ingredient_allergens(text: str) -> dict[str, str]:
    """contains / may_contain per EU14 key from UPPERCASE allergen words; the part after 'may contain' counts
    as may_contain. Conservative: 'NOT PEANUTS' still flags peanuts as may_contain."""
    if not text:
        return {}
    # strip negations: "DOES NOT CONTAIN MILK, GLUTEN OR WHEAT", "LACTOSE-FREE", "Gluten free WHEAT starch"
    text = re.sub(r"(?i)(does not|does no|do not|doesn't|ei sisällä)\s+contain[^.]*", " ", text)
    text = re.sub(r"(?i)(does not|does no|do not|doesn't|ei sisällä)[^.]*", " ", text)
    text = re.sub(r"(?i)gluten[- ]?free\s+(wheat|vehnä)\w*", " ", text)
    text = re.sub(r"(?i)\b[\wäöå]+[- ]free\b", " ", text)
    m = _MAY_RX.search(text)
    main, tail = (text[:m.start()], text[m.start():]) if m else (text, "")
    out: dict[str, str] = {}
    for k, rx in _ING_RX.items():
        if rx.search(main):
            out[k] = "contains"
        elif rx.search(tail):
            out[k] = "may_contain"
    return out


_SEV = {"contains": 3, "may_contain": 2, "unknown": 1, "free": 0}


def _allergens(cl: dict[str, list]) -> tuple[dict[str, str], bool]:
    status: dict[str, str] = {}
    contains, may, free = set(), set(), set()
    gf_claim = any(v.get("id") == "FREE_FROM_GLUTEN" for v in cl.get("nutritionalClaim", []))
    for v in cl.get("allergen", []):
        code, unit = v.get("id"), (v.get("unit") or "").upper()
        key = GS1_TO_EU14.get(code)
        if not key:
            continue
        if code == "ML" and unit != "CONTAINS":
            continue
        if code == "GO" and gf_claim:
            continue
        (contains if unit == "CONTAINS" else may).add(key)
    free_codes = {v.get("id") for v in cl.get("nonAllergen", []) if (v.get("unit") or "").upper() == "FREE_FROM"}
    for key in EU14:
        if key in contains:
            status[key] = "contains"
        elif key in may:
            status[key] = "may_contain"
        else:
            code = GROUP_FREE_CODE.get(key) or next(c for c, k in GS1_TO_EU14.items() if k == key)
            status[key] = "free" if code in free_codes else "unknown"
    return status, bool(cl.get("allergen") or cl.get("nonAllergen"))


def _csv(path) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open(path) as f:
            next(f)
            for line in f:
                pid, _, v = line.strip().partition(",")
                try:
                    out[pid] = float(v)
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


@lru_cache(maxsize=1)
def catalog() -> dict[str, P]:
    raw = json.load(open(PRODUCT_JSON))
    rel, co2 = _csv(RELIABILITY_CSV), _csv(CO2_CSV)
    out: dict[str, P] = {}
    for x in raw:
        if x.get("deleted"):
            continue
        sd = x.get("synkkaData") or {}
        cl = {c.get("name"): c.get("values") or [] for c in sd.get("classifications", [])}
        name_en, name_fi = _lang(sd.get("names"), "en"), _lang(sd.get("names"), "fi")
        name = name_en or name_fi or x["id"]
        struct, has = _allergens(cl)
        amt, unit = parse_amount(name_en or name_fi)
        claims = {v.get("id") for v in cl.get("nutritionalClaim", []) if v.get("id")}
        ingr = _lang(sd.get("keyIngredients"), "en") or _lang(sd.get("keyIngredients"), "fi")
        ing = ingredient_allergens(ingr)
        if "FREE_FROM_GLUTEN" in claims and ing.get("gluten"):   # EU gluten-free claim (<20 ppm) may use GF wheat starch / barley malt
            ing.pop("gluten")
        al, conflicts = dict(struct), []
        for k, st in ing.items():
            if _SEV[st] > _SEV[al.get(k, "unknown")] or (al.get(k) == "free" and st != "free"):
                if al.get(k) == "free":
                    conflicts.append(f"{k}:structured_free_but_ingredients_{st}")
                al[k] = st
        mkt = _lang(sd.get("marketingTexts"), "en") or _lang(sd.get("marketingTexts"), "fi")
        p = P(id=x["id"], name=name, name_fi=name_fi, brand=sd.get("brand") or "", vendor=x.get("vendorName") or "",
              category=x.get("category") or "", sales_unit=x.get("salesUnit") or "", claims=claims, allergens=al,
              has_allergen_data=has, structured_allergens=struct, allergen_conflicts=conflicts, ingredients=ingr, marketing=mkt, name_amount=amt, name_unit=unit,
              reliability=rel.get(x["id"]), co2=co2.get(x["id"]))
        p.search_blob = f"{name_en} {name_fi} {_lang(sd.get('names'), 'sv')} {p.brand}".lower()
        out[p.id] = p
    return out


def search(terms: list[str], *, must_all: bool = False, limit: int = 200) -> list[P]:
    """Naive keyword search (any/all of the lowercase terms in names). Used for pool selection and the baseline."""
    terms = [t.lower() for t in terms if t]
    res = []
    for p in catalog().values():
        hits = [t in p.search_blob for t in terms]
        if (all(hits) if must_all else any(hits)):
            res.append(p)
    res.sort(key=lambda p: (-sum(t in p.search_blob for t in terms), p.id))
    return res[:limit]


def content_per_unit(p: P, offer_raw: dict | None = None) -> tuple[Optional[float], Optional[str]]:
    """Litres or kg in ONE sales unit. Best source: price ratio catalogPrice / comparisonPrice from the live API
    (both are net, same validity). Fallback: amount parsed from the name."""
    if offer_raw:
        cp, cmp, cu = offer_raw.get("unit_price"), offer_raw.get("comparison_price"), offer_raw.get("comparison_unit")
        if cp and cmp and cu in ("L", "KG") and cmp > 0:
            ratio = cp / cmp
            # snap to the name amount when it agrees within rounding (prices are rounded to cents)
            if p.name_amount and p.name_unit == cu and abs(ratio - p.name_amount) / max(p.name_amount, 1e-9) < 0.15:
                return p.name_amount, cu
            return round(ratio, 3), cu
    return p.name_amount, p.name_unit
