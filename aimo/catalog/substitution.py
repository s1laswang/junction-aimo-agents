"""Good-better-best substitution ranking.

Hard filters (fail -> tier "rejected", kept in the list with a readable reason):
  - every required claim present (e.g. FREE_FROM_LACTOSE)
  - no excluded allergen whose status is contains / may_contain / unknown (unknown is NEVER safe;
    a Gemini-inferred hint is shown but does not unlock the product)
  - same content-unit family as the need (L vs KG vs PCS)
  - same product type as the reference (plain milk vs sour milk / coffee drink / flavoured)
  - orderable for the delivery date (cut-off, sales block, no stock, late availability)

Tiers for the rest:
  preferred  the customer's preferred_product_ids
  best       same brand + same key spec (fat level, organic) as the reference, different pack
  better     same key spec, other brand
  good       meets only the hard constraints (e.g. different fat %)

Within a tier: rank_score = 0.5 price (€/L or €/kg, relative to cheapest) + 0.2 delivery
reliability + 0.1 CO2 + 0.1 pack suitability + 0.1 stock coverage.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from aimo.catalog import units as U
from aimo.catalog.allergens import blocking, effective_excluded_allergens, normalize_allergens
from aimo.catalog.textsearch import summary_from_row
from aimo.models import Candidate, Need, Offer

TIER_ORDER = {"preferred": 0, "best": 1, "better": 2, "good": 3, "rejected": 9}

# product-type markers: candidates must carry the same set as the reference product
TYPE_MARKERS: dict[str, list[str]] = {
    "sour milk": ["sour milk", "sourmilk", "piimä", "filmjölk", "acidofilus", "gefilus"],
    "coffee": ["coffee", "kahvi", "latte", "cafè", "café", "cafe ", "mocca", "cappuccino"],
    "flavoured": ["chocolate", "cocoa", "kaakao", "vanilla", "vanilja", "strawberry", "mansikka", "banana", "mint",
                  "flavour", "flavor"],
    "yoghurt": ["yoghurt", "yogurt", "jogurtti", "soygurt", "curdled"],
    "cream": [" cream", "kerma"],
    "plant-based": ["oat drink", "kauraj", "soy drink", "soijaj", "almond drink", "plant-based", "oddlygood"],
    "portion": ["portion", "100x2 cl", "annos"],
    "cheese": ["cheese", "juusto"],
    "protein": ["protein", "proteiini"],
    "crumbs": ["breadcrumb", "korppujauho", "panko", "croutons", "krutonki"],
    "condensed/powder": ["condensed", "kondensoitu", "powder", "jauhe", "evaporated"],
    "kitchen": ["keittiön", "kiehu", "cooking"],
}
ORGANIC_WORDS = ("organic", "luomu")


def product_types(d: dict[str, Any]) -> set[str]:
    n = " " + " ".join(x or "" for x in (d.get("name_en"), d.get("name_fi"))).lower() + " "
    return {t for t, words in TYPE_MARKERS.items() if any(w in n for w in words)}


def is_organic(d: dict[str, Any]) -> bool:
    n = (d.get("name_en") or d.get("name_fi") or "").lower()
    return "ORGANIC" in (d.get("claims") or []) or any(w in n for w in ORGANIC_WORDS)


def fat_class(f: Optional[float]) -> Optional[str]:
    if f is None:
        return None
    if f < 0.5:
        return "skimmed"
    if f < 2.0:
        return "semi-skimmed"
    if f < 3.2:
        return "3%"
    return "whole"


@dataclass
class Spec:
    brand: Optional[str] = None
    fat: Optional[float] = None
    organic: Optional[bool] = None
    types: set[str] = field(default_factory=set)
    name: Optional[str] = None
    pack_label: Optional[str] = None
    category: Optional[str] = None
    source: str = "none"   # preferred | preferences | top_hit | none  (what shapes the tiers)
    types_from: Optional[str] = None   # preferred | top_hit | None  (what the product-type check compares to)

    def describe(self, with_brand: bool = True) -> str:
        bits = []
        if self.brand and with_brand:
            bits.append(self.brand)
        if self.fat is not None:
            bits.append(f"{self.fat:g} % fat")
        if self.organic:
            bits.append("organic")
        return ", ".join(bits) or "no specific spec"


# Only these preference keys describe the product. Anything else in Need.preferences
# (parsed_by, delivery_date_assumed, standing_order_*, injection_flags ...) is ignored here.
SPEC_PREF_KEYS = ("brand", "fat_pct", "organic", "pack", "use")


def spec_preferences(need: Need) -> dict[str, Any]:
    prefs = need.preferences or {}
    return {k: prefs[k] for k in SPEC_PREF_KEYS if prefs.get(k) not in (None, "", [])}


def reference_spec(need: Need, rows: dict[str, dict[str, Any]], fallback_row: Optional[dict] = None) -> Spec:
    sp = spec_preferences(need)
    shaping = {k: v for k, v in sp.items() if k in ("brand", "fat_pct", "organic")}
    ref = None
    for pid in need.preferred_product_ids:
        if pid in rows and not blocking(rows[pid].get("allergens") or {}, need.excluded_allergens,
                                        allergen_source=rows[pid].get("allergen_source")):
            ref = rows[pid]
            break
    src = "preferred" if ref else "none"
    if ref is None and fallback_row is not None:
        # the closest match is ALWAYS the product-type reference when there is no usual product,
        # whatever the preferences say (preferences can only add brand / fat / organic)
        ref, src = fallback_row, "top_hit"
    spec = Spec(source=src, types_from=src if ref is not None else None)
    if ref is not None:
        spec.brand = ref.get("brand")
        spec.fat = ref.get("fat_pct")
        spec.organic = is_organic(ref)
        spec.types = product_types(ref)
        spec.name = ref.get("name")
        spec.category = ref.get("category")
        spec.pack_label = U.describe_pack(ref.get("content_amount"), ref.get("content_unit"), ref.get("sales_unit_size") or 1,
                                          ref.get("base_content_amount"), ref.get("sales_unit") or "")
    if shaping:
        if spec.source != "preferred":
            spec.source = "preferences"
            # a guessed top hit must not impose its own brand/fat/organic: only the stated ones count
            spec.brand, spec.fat, spec.organic, spec.category = None, None, None, None
            spec.name = None
        if shaping.get("brand"):
            spec.brand = str(shaping["brand"])
        if shaping.get("fat_pct") is not None:
            try:
                spec.fat = float(shaping["fat_pct"])
            except (TypeError, ValueError):
                pass
        if shaping.get("organic") is not None:
            spec.organic = bool(shaping["organic"])
    return spec


# words too generic to identify a product on their own ("oat drink" must not match "soft drink")
GENERIC_CONCEPTS = {"drink", "drinks", "product", "products", "food", "mix", "pack", "box", "bottle", "can",
                    "piece", "pieces", "portion", "item", "items", "stuff", "fresh", "frozen", "large", "small"}


def missing_query_concepts(query: str, d: dict[str, Any]) -> list[str]:
    """Distinguishing query concepts the product does not have. A concept is satisfied by the product
    name (any language, synonyms) or, for claim words like 'lactose free', by the product's claims."""
    from aimo.catalog.textsearch import IMPLIED_CLAIMS, clean_query, parse_query
    name_parts = (d.get("name_en") or d.get("name"), d.get("name_fi"), d.get("name_sv"))
    raw_names = " ".join(x or "" for x in name_parts).lower()
    cleaned_names = " ".join(clean_query(x).lower() for x in name_parts if x)
    claims = set(d.get("claims") or [])
    out = []
    for c, forms in parse_query(query):
        if c in GENERIC_CONCEPTS:
            continue
        if IMPLIED_CLAIMS.get(c):
            if IMPLIED_CLAIMS[c] in claims or any(f.lower() in raw_names for f in forms):
                continue
            out.append(c)
            continue
        if not any(f.lower() in cleaned_names for f in forms):
            out.append(c)
    return out


def _query_coverage(query: str, d: dict[str, Any]) -> float:
    from aimo.catalog.textsearch import IMPLIED_CLAIMS, clean_query, parse_query
    concepts = parse_query(query)
    if not concepts:
        return 1.0
    name_parts = (d.get("name_en") or d.get("name"), d.get("name_fi"), d.get("name_sv"))
    raw_names = " ".join(x or "" for x in name_parts).lower()
    cleaned_names = " ".join(clean_query(x).lower() for x in name_parts if x)
    hits = 0
    for c, forms in concepts:
        target = raw_names if IMPLIED_CLAIMS.get(c) else cleaned_names
        if any(f.lower() in target for f in forms):
            hits += 1
    return hits / len(concepts)


def _same_brand(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def _same_fat(a: Optional[float], b: Optional[float]) -> bool:
    """a = candidate, b = reference/spec. No fat requirement (b None) matches anything."""
    if b is None:
        return True
    if a is None:
        return False
    return abs(a - b) <= 0.3


def _fmt_money(x: float) -> str:
    return f"€{x:,.2f}"


def _d(dt) -> str:
    try:
        return dt.strftime("%a %d %b %H:%M")
    except Exception:  # noqa: BLE001
        return str(dt)


def effective_content(d: dict[str, Any], offer: Optional[Offer]) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Static content, overridden by the price-derived value when they disagree."""
    amt, unit = d.get("content_amount"), d.get("content_unit")
    if offer is not None:
        return U.reconcile_with_price(amt, unit, offer.unit_price, offer.comparison_price, offer.comparison_unit)
    return amt, unit, None


def price_per_unit(offer: Optional[Offer], content: Optional[float], unit: Optional[str], need_unit: str) -> Optional[float]:
    if offer is None or offer.unit_price is None:
        return None
    if offer.comparison_price and offer.comparison_unit == need_unit:
        return float(offer.comparison_price)
    if content and unit == need_unit:
        return offer.unit_price / content
    return None


def rank_substitutes(need: Need, offers: dict[str, Offer] | None = None, limit: int = 12, *,
                     rows: dict[str, dict[str, Any]] | None = None, relevance: dict[str, float] | None = None,
                     inferred_allergens: dict[str, dict[str, str]] | None = None) -> list[Candidate]:
    """Rank candidate rows for `need`. If `rows` is not given, candidates come from find_candidates()."""
    caller_excl = normalize_allergens(need.excluded_allergens)
    excl = effective_excluded_allergens(need.customer_id, need.excluded_allergens)
    if rows is not None and rows and caller_excl and all("allergen_source" not in d for d in rows.values()):
        excl = [a for a in excl if a in caller_excl or any(a in (d.get("allergens") or {}) for d in rows.values())]
    if excl != list(need.excluded_allergens):
        need = need.model_copy(update={"excluded_allergens": excl})
    if rows is None:
        from aimo.catalog.plan import find_candidates
        rows, relevance = find_candidates(need)
    offers = offers or {}
    relevance = relevance or {}
    inferred_allergens = inferred_allergens or {}
    top_hit = None
    # reference when there is no usual product: the most relevant row that itself passes the static
    # hard constraints (claims, allergens, unit family); allergen-unsafe rows never set the type.
    # Prefer a reference whose product type is what the query itself names ("lactose-free milk" -> plain
    # milk, not "Cafè milk"; "oat drink" -> plant-based).
    q_types = product_types({"name_en": " " + (need.product_query or "") + " "})
    for level in (2, 1, 0):
        for pid, _s in sorted(relevance.items(), key=lambda x: -x[1]):
            d0 = rows.get(pid)
            if d0 is None or not set(need.required_claims) <= set(d0.get("claims") or []):
                continue
            if level >= 1 and (blocking(d0.get("allergens") or {}, need.excluded_allergens,
                                        allergen_source=d0.get("allergen_source"))
                               or d0.get("content_unit") != need.unit
                               or missing_query_concepts(need.product_query, d0)):
                continue
            if level == 2 and product_types(d0) != q_types:
                continue
            top_hit = d0
            break
        if top_hit is not None:
            break
    spec = reference_spec(need, rows, top_hit)
    prefs = set(need.preferred_product_ids)

    scratch: list[dict[str, Any]] = []
    for pid, d in rows.items():
        offer = offers.get(pid)
        content, cunit, cnote = effective_content(d, offer)
        reasons: list[str] = []
        flags: list[str] = []
        reject: list[str] = []
        summary = summary_from_row(d, relevance.get(pid))
        if content is not None:
            summary.content_amount, summary.content_unit = content, cunit if cunit in ("L", "KG", "PCS") else None
        if cnote:
            flags.append("content_from_price")
        # ---- hard filters
        missing = [c for c in need.required_claims if c not in (d.get("claims") or [])]
        if missing:
            have_low = "LOW_LACTOSE" in (d.get("claims") or []) and "FREE_FROM_LACTOSE" in missing
            reject.append(f"missing required claim {', '.join(missing)}" + (" (only LOW_LACTOSE)" if have_low else ""))
        conflicts = d.get("allergen_conflicts") or {}
        if conflicts:
            flags.append("allergen_data_conflict")
        blk = blocking(d.get("allergens") or {}, need.excluded_allergens, allergen_source=d.get("allergen_source"))
        for g, st in blk:
            if st == "unknown" and g in conflicts:
                flags.append("allergen_unknown")
                reject.append(f"{g}: supplier data says free from {g} but the ingredients say "
                              f"'{conflicts[g].get('evidence', '')[:60]}' (data conflict; treated as unknown)")
                continue
            if st == "unknown":
                flags.append("allergen_unknown")
                hint = (inferred_allergens.get(pid) or {}).get(g)
                hint_txt = {"contains": f"Gemini reads the ingredients as containing {g}",
                            "may_contain": f"Gemini reads the ingredients as possibly containing {g}",
                            "not_found": f"Gemini found no {g} in the ingredients"}.get(hint or "", "")
                reject.append(f"{g}: no structured allergen data (unknown is never treated as safe)"
                              + (f"; hint: {hint_txt} (inferred:gemini, needs human confirmation)" if hint_txt else ""))
                if hint:
                    flags.append("allergen_inferred_hint")
            else:
                reject.append(f"{g}: {st.replace('_', ' ')} per supplier data")
        if cunit != need.unit:
            reject.append(f"sold by {cunit or 'unknown unit'}, need is in {need.unit}")
        ctypes = product_types(d)
        type_note = None
        if not prefs:
            # no usual product: the product itself must be what was asked for (no soft drink for "oat drink")
            miss = missing_query_concepts(need.product_query, d)
            if miss:
                reject.append(f"different product type: not a match for '{', '.join(miss)}' in '{need.product_query}'")
        if spec.types_from is not None and ctypes != spec.types:
            diff = sorted(ctypes ^ spec.types)
            if spec.types_from == "top_hit":
                # the reference is only our best guess, not the customer's usual product: demote, never reject
                type_note = f"different product type than the closest match ({', '.join(diff)})"
            else:
                reject.append(f"different product type ({', '.join(diff)})")
        if offer is not None:
            if offer.cutoff_passed:
                flags.append("cutoff_passed")
                reject.append(f"order cut-off passed ({_d(offer.closing_time)}) for delivery {need.delivery_by:%a %d %b}")
            elif offer.sales_block:
                flags.append("sales_block")
                reject.append("sales block: cannot be ordered currently")
            elif not offer.available:
                flags.append("unavailable")
                if offer.available_amount == 0:
                    reject.append("out of stock")
                elif offer.available_date and offer.available_date > need.delivery_by:
                    reject.append(f"earliest delivery {offer.available_date:%a %d %b}, after {need.delivery_by:%a %d %b}")
                else:
                    reject.append("not available for the requested date")
            if offer.unit_price is None:
                reject.append("no price available")
        else:
            flags.append("no_live_offer")
        # ---- tier
        same_brand = _same_brand(d.get("brand"), spec.brand)
        same_fat = _same_fat(d.get("fat_pct"), spec.fat)
        same_org = (spec.organic is None) or (is_organic(d) == bool(spec.organic))
        same_spec = same_fat and same_org
        same_cat = not spec.category or d.get("category") == spec.category
        coverage = _query_coverage(need.product_query, d)
        if pid in prefs:
            tier = "preferred"
        elif spec.source in ("top_hit", "none"):
            # no usual product on file: tier by how exactly the product matches the request
            tier = "best" if coverage >= 1.0 else ("better" if coverage >= 0.5 else "good")
            if type_note:
                tier = "good"
        elif spec.source == "preferences" and not prefs:
            want_brand = bool(spec.brand)
            if same_spec and (same_brand or not want_brand) and not type_note:
                tier = "best"
            elif same_spec and not type_note:
                tier = "better"
            else:
                tier = "good"
        elif same_brand and same_spec and same_cat:
            tier = "best"
        elif same_spec and same_cat:
            tier = "better"
        else:
            tier = "good"
        if reject:
            tier_if_ok = tier
            tier = "rejected"
        else:
            tier_if_ok = tier
        if prefs and pid not in prefs:
            flags.append("outside_preference")
        scratch.append(dict(pid=pid, d=d, offer=offer, content=content, cunit=cunit, cnote=cnote, reasons=reasons,
                            flags=flags, reject=reject, tier=tier, tier_if_ok=tier_if_ok, summary=summary,
                            same_brand=same_brand, same_fat=same_fat, same_org=same_org, same_cat=same_cat,
                            coverage=coverage, spec_source=spec.source, type_note=type_note))

    # ---- scores (relative within the eligible set)
    eligible = [x for x in scratch if x["tier"] != "rejected"]
    ppu = {x["pid"]: price_per_unit(x["offer"], x["content"], x["cunit"], need.unit) for x in scratch}
    prices = [ppu[x["pid"]] for x in eligible if ppu[x["pid"]]]
    min_p = min(prices) if prices else None
    co2s = [x["d"].get("co2_kg_per_kg") for x in eligible if x["d"].get("co2_kg_per_kg")]
    min_co2 = min(co2s) if co2s else None
    for x in scratch:
        d, offer, pid = x["d"], x["offer"], x["pid"]
        p = ppu[pid]
        price_s = (min_p / p) if (p and min_p) else 0.3
        rel = d.get("delivery_reliability")
        rel_s = rel if rel is not None else 0.85
        co2 = d.get("co2_kg_per_kg")
        co2_s = (min_co2 / co2) if (co2 and min_co2) else 0.7
        content = x["content"] or 0
        pack_s = 1.0
        if need.quantity >= 50 and content:
            pack_s = 1.0 if content >= 1 else (0.6 if content >= 0.25 else 0.3)
        cover_s = 1.0
        if offer is not None and offer.available_amount is not None and content:
            if offer.available_amount < 1e8:
                cov = offer.available_amount * content / need.quantity
                cover_s = min(1.0, cov)
                if cov < 1.0 and x["tier"] != "rejected":
                    x["flags"].append("partial_stock")
        x["score"] = round(0.5 * price_s + 0.2 * rel_s + 0.1 * co2_s + 0.1 * pack_s + 0.1 * cover_s
                           - (0.2 if x.get("type_note") else 0.0), 4)
        x["ppu"] = p
        x["reasons"].extend(_reasons(x, spec, need))

    scratch.sort(key=lambda x: (TIER_ORDER[x["tier"]], 0 if x["pid"] in prefs else 1, -x["score"],
                                -(relevance.get(x["pid"]) or 0)))
    out: list[Candidate] = []
    n_rej = 0
    for x in scratch:
        if x["tier"] == "rejected":
            # keep relevant rejections only: preferred, same-family products, or orderability problems
            type_mismatch = any(r.startswith("different product type") for r in x["reject"])
            allergen_hit = any(r.split(":")[0] in need.excluded_allergens for r in x["reject"])
            useful = (x["pid"] in prefs or allergen_hit
                      or (x["cunit"] == need.unit and not type_mismatch))
            if not useful or n_rej >= max(6, limit // 2):
                continue
            n_rej += 1
        elif sum(1 for c in out if c.tier != "rejected") >= limit:
            continue
        out.append(Candidate(product=x["summary"], offer=x["offer"], tier=x["tier"], rank_score=x["score"],
                             reasons=x["reasons"], flags=sorted(set(x["flags"]))))
    return out


def _reasons(x: dict[str, Any], spec: Spec, need: Need) -> list[str]:
    d, offer = x["d"], x["offer"]
    r: list[str] = []
    ref = spec.name or "the requested spec"
    pack = U.describe_pack(x["content"], x["cunit"], d.get("sales_unit_size") or 1, d.get("base_content_amount"),
                           d.get("sales_unit") or "")
    t = x["tier"] if x["tier"] != "rejected" else x["tier_if_ok"]
    if x["tier"] == "rejected":
        r.append("Rejected: " + "; ".join(x["reject"]))
    if x.get("spec_source") in ("top_hit", "none") and t != "preferred":
        q = need.product_query
        r.append({"best": f"Exact match for '{q}' (no usual product on file)",
                  "better": f"Close match for '{q}' (most of the requested words)",
                  "good": f"Partial match for '{q}'; meets the hard constraints"}[t]
                 + (f"; {x['type_note']}" if x.get("type_note") else ""))
    elif x.get("spec_source") == "preferences" and t != "preferred":
        want = spec.describe()
        r.append({"best": f"Matches '{need.product_query}' and your stated preference ({want})",
                  "better": f"Matches '{need.product_query}' and the spec; other brand ({d.get('brand') or 'unknown'}) "
                            f"than preferred {spec.brand}",
                  "good": f"Matches '{need.product_query}'; differs from your stated preference ({want})"}[t]
                 + (f"; {x['type_note']}" if x.get("type_note") else ""))
    elif t == "preferred":
        r.append("Your usual product")
    elif t == "best":
        bits = ["brand"]
        if spec.fat is not None:
            bits.append("fat level")
        if spec.organic:
            bits.append("organic status")
        r.append(f"Same {' and '.join(bits)} as your usual {ref}" + (f" ({spec.pack_label})" if spec.pack_label else "")
                 + "; different pack")
    elif t == "better":
        r.append(f"Same spec ({spec.describe(with_brand=False)}) as {ref}, other brand ({d.get('brand') or 'unknown'})")
    elif t == "good":
        diffs = []
        if spec.fat is not None and not x["same_fat"]:
            diffs.append(f"fat {d.get('fat_pct') if d.get('fat_pct') is not None else '?'} % vs {spec.fat:g} %")
        if not x["same_org"]:
            diffs.append("organic status differs")
        if not x["same_cat"]:
            diffs.append(f"different product category ({d.get('category')} vs {spec.category})")
        r.append("Meets the hard constraints" + (f"; differs: {', '.join(diffs)}" if diffs else ""))
    r.append(pack)
    if x["ppu"]:
        r.append(f"{_fmt_money(x['ppu'])}/{need.unit}" + (f" ({_fmt_money(offer.unit_price)} per {d.get('sales_unit')})" if offer and offer.unit_price else ""))
    if d.get("delivery_reliability") is not None:
        r.append(f"delivery reliability {d['delivery_reliability']:.2f}")
    else:
        r.append("delivery reliability: no data")
    if d.get("co2_kg_per_kg") is not None:
        r.append(f"CO2 {d['co2_kg_per_kg']:.2f} kg CO2e/kg (category average)")
    if need.required_claims and not any(m.startswith("missing required claim") for m in x["reject"]):
        r.append("has " + ", ".join(need.required_claims))
    if need.excluded_allergens and not any("allergen" in m or ":" in m for m in x["reject"]):
        r.append("free from " + ", ".join(need.excluded_allergens) + " per structured supplier data")
    if offer is not None:
        if offer.logistic_model:
            r.append(f"{offer.logistic_model} item" + (f", order by {_d(offer.closing_time)}" if offer.closing_time and not offer.cutoff_passed else ""))
        if offer.available_amount is not None and offer.available_amount < 1e8 and x["content"]:
            r.append(f"only {offer.available_amount:g} {d.get('sales_unit')} in stock (= {offer.available_amount * x['content']:g} {need.unit})"
                     if "partial_stock" in x["flags"] else f"{offer.available_amount:g} {d.get('sales_unit')} in stock")
    if x["cnote"]:
        r.append(x["cnote"])
    return r
