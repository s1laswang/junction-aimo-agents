"""GS1 allergen codes -> EU14 tri-state (+ unknown).

The schema file does NOT carry enum descriptions for allergen codes (verified: it only says the
`unit` tells CONTAINS or FREE_FROM). The code meanings below are the GS1 AllergenTypeCode list
(GDSN code list), which is what Synkka uses. Codes observed in the data that do not belong to an
EU14 group (e.g. NC cocoa, NM maize, NE peas, NK coriander, NW carrot, BF beef, BP pork,
PO poultry, SO soy?, CM, UN, PN, X99) are kept in `allergen_raw` but not mapped.

Policy (conservative):
- any member code CONTAINS        -> contains
- else any member code MAY_CONTAIN -> may_contain
- else the group's *head* code FREE_FROM (nonAllergen) or an explicit FREE_FROM_* claim -> free
- UNDECLARED, missing, or only sub-codes free (e.g. ML lactose free says nothing about milk) -> unknown
"""
from __future__ import annotations

from typing import Iterable

from aimo.models import EU14

# group -> (head codes that alone can prove FREE_FROM, all member codes)
GS1_GROUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "gluten": (("AW",), ("AW", "UW", "GB", "GO", "NR", "GS", "GK")),  # cereals containing gluten
    "crustaceans": (("AC",), ("AC",)),
    "eggs": (("AE",), ("AE",)),
    "fish": (("AF",), ("AF",)),
    "peanuts": (("AP",), ("AP",)),
    "soy": (("AY",), ("AY",)),
    "milk": (("AM",), ("AM", "ML")),                                     # ML = lactose
    "nuts": (("AN",), ("AN", "SA", "SH", "ST", "SW", "SC", "SP", "SM", "SR", "SQ")),
    "celery": (("BC",), ("BC",)),
    "mustard": (("BM",), ("BM",)),
    "sesame": (("AS",), ("AS",)),
    "sulphites": (("AU",), ("AU",)),
    "lupin": (("NL",), ("NL",)),
    "molluscs": (("UM",), ("UM",)),
}
CODE_TO_GROUP = {c: g for g, (_, members) in GS1_GROUPS.items() for c in members}

GS1_CODE_NAMES = {
    "AW": "cereals containing gluten", "UW": "wheat", "GB": "barley", "GO": "oats", "NR": "rye",
    "GS": "spelt", "GK": "kamut", "AC": "crustaceans", "AE": "eggs", "AF": "fish", "AP": "peanuts",
    "AY": "soybeans", "AM": "milk", "ML": "lactose", "AN": "tree nuts", "SA": "almond",
    "SH": "hazelnut", "ST": "pistachio", "SW": "walnut", "SC": "cashew", "SP": "pecan",
    "SM": "macadamia", "SR": "brazil nut", "SQ": "queensland nut", "BC": "celery", "BM": "mustard",
    "AS": "sesame", "AU": "sulphur dioxide / sulphites", "NL": "lupin", "UM": "molluscs",
}

# nutritionalClaim ids that prove a whole EU14 group free (when nothing says CONTAINS / MAY_CONTAIN)
CLAIM_FREE = {"FREE_FROM_GLUTEN": "gluten", "FREE_FROM_EGGS": "eggs", "FREE_FROM_SOY": "soy"}

# rough English/Finnish/Swedish keyword hints for each group (used only for the *inferred* path
# and for tests; never for structured status)
GROUP_KEYWORDS = {
    "milk": ["milk", "maito", "mjölk", "cream", "kerma", "butter", "voi", "cheese", "juusto", "whey", "hera", "lactose", "laktoosi"],
    "gluten": ["wheat", "vehnä", "rye", "ruis", "barley", "ohra", "oat", "kaura", "spelt", "gluten"],
    "eggs": ["egg", "kanamuna", "muna", "ägg"],
}


def derive(classifications: Iterable[dict]) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Return (allergens {group: status}, allergen_source {group: source}, raw {CODE: [units]}).

    Every EU14 key is present in the result: groups without data are "unknown" with source "none".
    """
    contains: dict[str, set[str]] = {}
    may: dict[str, set[str]] = {}
    free_codes: set[str] = set()
    claims: set[str] = set()
    raw: dict[str, list[str]] = {}
    for c in classifications or []:
        name = c.get("name")
        for v in c.get("values") or []:
            code, unit = v.get("id"), (v.get("unit") or "").upper()
            if name in ("allergen", "nonAllergen") and code:
                raw.setdefault(code, []).append(unit or "?")
                g = CODE_TO_GROUP.get(code)
                if unit == "CONTAINS" and g:
                    contains.setdefault(g, set()).add(code)
                elif unit == "MAY_CONTAIN" and g:
                    may.setdefault(g, set()).add(code)
                elif unit == "FREE_FROM":
                    free_codes.add(code)
                # UNDECLARED -> no information
            elif name == "nutritionalClaim" and code:
                claims.add(code)

    status: dict[str, str] = {}
    source: dict[str, str] = {}
    for g in EU14:
        heads, _members = GS1_GROUPS[g]
        if g in contains:
            status[g], source[g] = "contains", "structured"
        elif g in may:
            status[g], source[g] = "may_contain", "structured"
        elif any(h in free_codes for h in heads):
            status[g], source[g] = "free", "structured"
        elif any(CLAIM_FREE.get(cl) == g for cl in claims):
            status[g], source[g] = "free", "structured:claim"
        else:
            status[g], source[g] = "unknown", "none"
    return status, source, raw


def has_structured_data(source: dict[str, str]) -> bool:
    return any(s.startswith("structured") for s in source.values())


def blocking(allergens: dict[str, str], excluded: Iterable[str]) -> list[tuple[str, str]]:
    """Excluded allergen groups whose status is not provably free: [(group, status)]."""
    out = []
    for g in excluded:
        st = allergens.get(g, "unknown")
        if hasattr(st, "value"):
            st = st.value
        if st != "free":
            out.append((g, st))
    return out
