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

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

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


ALLERGEN_ALIASES: dict[str, tuple[str, ...]] = {
    **{g: (g,) for g in EU14},
    **{c: (g,) for c, g in CODE_TO_GROUP.items()},
    **{c.lower(): (g,) for c, g in CODE_TO_GROUP.items()},
    "dairy": ("milk",),
    "cow's milk": ("milk",),
    "cows milk": ("milk",),
    "cow milk": ("milk",),
    "milk protein": ("milk",),
    "cow's milk protein": ("milk",),
    "maito": ("milk",),
    "mjölk": ("milk",),
    "nut": ("nuts",),
    "nuts": ("nuts",),
    "tree nut": ("nuts",),
    "tree nuts": ("nuts",),
    "tree_nut": ("nuts",),
    "tree_nuts": ("nuts",),
    "pähkinä": ("nuts",),
    "pähkinät": ("nuts",),
    "nöt": ("nuts",),
    "nötter": ("nuts",),
    "almond": ("nuts",),
    "almonds": ("nuts",),
    "manteli": ("nuts",),
    "hazelnut": ("nuts",),
    "hazelnuts": ("nuts",),
    "hasselpähkinä": ("nuts",),
    "pistachio": ("nuts",),
    "pistachios": ("nuts",),
    "walnut": ("nuts",),
    "walnuts": ("nuts",),
    "saksanpähkinä": ("nuts",),
    "cashew": ("nuts",),
    "cashews": ("nuts",),
    "cashewpähkinä": ("nuts",),
    "pecan": ("nuts",),
    "pecans": ("nuts",),
    "macadamia": ("nuts",),
    "brazil nut": ("nuts",),
    "brazil nuts": ("nuts",),
    "queensland nut": ("nuts",),
    "peanut": ("peanuts",),
    "peanuts": ("peanuts",),
    "groundnut": ("peanuts",),
    "groundnuts": ("peanuts",),
    "maapähkinä": ("peanuts",),
    "maapähkinät": ("peanuts",),
    "jordnöt": ("peanuts",),
    "jordnötter": ("peanuts",),
    "egg": ("eggs",),
    "eggs": ("eggs",),
    "kananmuna": ("eggs",),
    "kanamuna": ("eggs",),
    "muna": ("eggs",),
    "munat": ("eggs",),
    "ägg": ("eggs",),
    "wheat": ("gluten",),
    "vehnä": ("gluten",),
    "vete": ("gluten",),
    "rye": ("gluten",),
    "ruis": ("gluten",),
    "råg": ("gluten",),
    "barley": ("gluten",),
    "ohra": ("gluten",),
    "korn": ("gluten",),
    "spelt": ("gluten",),
    "speltti": ("gluten",),
    "kamut": ("gluten",),
    "gluteeni": ("gluten",),
    "cereals containing gluten": ("gluten",),
    "soya": ("soy",),
    "soija": ("soy",),
    "soja": ("soy",),
    "soybean": ("soy",),
    "soybeans": ("soy",),
    "soya bean": ("soy",),
    "soya beans": ("soy",),
    "kala": ("fish",),
    "fisk": ("fish",),
    "crustacean": ("crustaceans",),
    "äyriäiset": ("crustaceans",),
    "äyriäinen": ("crustaceans",),
    "kräftdjur": ("crustaceans",),
    "mollusc": ("molluscs",),
    "mollusk": ("molluscs",),
    "mollusks": ("molluscs",),
    "nilviäiset": ("molluscs",),
    "nilviäinen": ("molluscs",),
    "blötdjur": ("molluscs",),
    "shellfish": ("crustaceans", "molluscs"),
    "sulfite": ("sulphites",),
    "sulfites": ("sulphites",),
    "sulphite": ("sulphites",),
    "sulphur dioxide": ("sulphites",),
    "sulfur dioxide": ("sulphites",),
    "sulphur dioxide / sulphites": ("sulphites",),
    "sulfiitit": ("sulphites",),
    "sulfiitti": ("sulphites",),
    "rikkidioksidi": ("sulphites",),
    "sulfit": ("sulphites",),
    "sulfiter": ("sulphites",),
    "sesame seeds": ("sesame",),
    "sesame seed": ("sesame",),
    "seesami": ("sesame",),
    "seesaminsiemenet": ("sesame",),
    "sesam": ("sesame",),
    "selleri": ("celery",),
    "sinappi": ("mustard",),
    "senap": ("mustard",),
    "lupins": ("lupin",),
    "lupine": ("lupin",),
    "lupiini": ("lupin",),
}

_POLICY_SEVERITY = {"allow": 0, "needs_approval": 1, "reject": 2}
_last_policy_dir: Any = None


def normalize_allergen(name: str) -> tuple[str, ...]:
    s = re.sub(r"\s+", " ", str(name or "").replace("\u2019", "'")).strip()
    if not s:
        return ()
    low = s.lower()
    if low in ALLERGEN_ALIASES:
        return ALLERGEN_ALIASES[low]
    spaced = re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", low)).strip()
    if spaced in ALLERGEN_ALIASES:
        return ALLERGEN_ALIASES[spaced]
    return (low,)


def normalize_allergens(excluded: Iterable[str]) -> list[str]:
    items = [excluded] if isinstance(excluded, str) else (excluded or ())
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        for g in normalize_allergen(raw):
            if g and g not in seen:
                seen.add(g)
                out.append(g)
    return out


@dataclass(frozen=True)
class PolicyAllergenBlock:
    customer_id: str = ""
    excluded: tuple[str, ...] = ()
    reason: Optional[str] = None
    on_contains: str = "reject"
    on_may_contain: str = "reject"
    on_unknown: str = "reject"
    policy_id: Optional[str] = None


def _strictest(outcomes: list[str], default: str = "reject") -> str:
    if not outcomes:
        return default
    return max(outcomes, key=lambda o: _POLICY_SEVERITY.get(o, 2))


def policy_allergen_block(customer_id: Optional[str]) -> PolicyAllergenBlock:
    global _last_policy_dir
    cid = (customer_id or "").strip()
    if not cid:
        return PolicyAllergenBlock(customer_id="")
    try:
        from aimo.trust import policy as trust_policy

        if trust_policy.POLICY_DIR != _last_policy_dir:
            if hasattr(trust_policy, "_load_policy_cached"):
                trust_policy._load_policy_cached.cache_clear()
            _last_policy_dir = trust_policy.POLICY_DIR
        pol = trust_policy.load_policy(cid)
    except (KeyError, ValueError, OSError, ImportError):
        return PolicyAllergenBlock(customer_id=cid)
    if not isinstance(pol, dict):
        return PolicyAllergenBlock(customer_id=cid)
    rules = [r for r in (pol.get("rules") or []) if isinstance(r, dict) and r.get("type") == "allergen_block"]
    pid = str(pol.get("policy_id") or cid)
    if not rules:
        return PolicyAllergenBlock(customer_id=cid, policy_id=pid)
    raw_excl: list[str] = []
    reasons: list[str] = []
    for r in rules:
        raw_excl.extend(r.get("excluded") or [])
        why = str(r.get("reason") or "").strip()
        if why and why not in reasons:
            reasons.append(why)
    return PolicyAllergenBlock(
        customer_id=cid,
        excluded=tuple(normalize_allergens(raw_excl)),
        reason="; ".join(reasons) if reasons else None,
        on_contains=_strictest([str(r.get("on_contains") or "reject") for r in rules]),
        on_may_contain=_strictest([str(r.get("on_may_contain") or "reject") for r in rules]),
        on_unknown=_strictest([str(r.get("on_unknown") or "reject") for r in rules]),
        policy_id=pid,
    )


def policy_excluded_allergens(customer_id: Optional[str]) -> list[str]:
    return list(policy_allergen_block(customer_id).excluded)


def effective_excluded_allergens(customer_id: Optional[str], excluded: Iterable[str] = ()) -> list[str]:
    extra = [excluded] if isinstance(excluded, str) else (excluded or ())
    return normalize_allergens([*extra, *policy_allergen_block(customer_id).excluded])


def blocking(
    allergens: dict[str, Any],
    excluded: Iterable[str],
    *,
    allergen_source: Optional[dict[str, str]] = None,
) -> list[tuple[str, str]]:
    amap = allergens or {}
    out: list[tuple[str, str]] = []
    for g in normalize_allergens(excluded):
        st = amap.get(g, "unknown")
        if hasattr(st, "value"):
            st = st.value
        if not st:
            st = "unknown"
        if allergen_source is not None and (allergen_source.get(g) or "").startswith("inferred") and st == "free":
            st = "unknown"
        if st != "free":
            out.append((g, st))
    return out
