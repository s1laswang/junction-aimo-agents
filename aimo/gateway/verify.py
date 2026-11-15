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
    effective_excluded_allergens,
    normalize_allergens,
)
from aimo.catalog.screening_bridge import screen_reason
from aimo.models import Need, Offer, Product

log = logging.getLogger("aimo.gateway.verify")

# Server-side copy of the demo customers' usual products (the buyer keeps its own copy in
# aimo/buyer/profiles.py, which the gateway must not trust). Superseded by the trust policy key
# `preferred_products` as soon as it exists.
USUAL_PRODUCTS: dict[str, list[str]] = {
    "hotel-aurora": ["463038"],   # Valio Eila semi skimmed milk drink 10 l novobox lactose free (dispenser)
    "school-tampere": [],
    "hospital-meilahti": [],
}

# Dietary claims a substitute must keep when no explicit need is given.
DIETARY_CLAIMS = {"FREE_FROM_LACTOSE", "LOW_LACTOSE", "FREE_FROM_GLUTEN", "GLUTEN_FREE", "FREE_FROM_MILK",
                  "VEGAN"}

INFO_ISSUES = ("limited_stock", "terminal_unknown_qty")
DERIVED_FLAGS = ("cutoff_passed", "unavailable", "sales_block", "allergen_unknown", "outside_preference",
                 "allergen_inferred_hint", "content_from_price", "no_live_offer", "allergen_safe_alternative")

_GS1_SHORT_CODES = {c.lower() for c in CODE_TO_GROUP}
_PRODUCT_ALLERGEN_TERMS: dict[str, tuple[str, ...]] = {
    "butter": ("milk",),
    "voi": ("milk",),
    "smör": ("milk",),
    "cream": ("milk",),
    "kerma": ("milk",),
    "grädde": ("milk",),
    "cheese": ("milk",),
    "juusto": ("milk",),
    "whey": ("milk",),
    "hera": ("milk",),
    "buttermilk": ("milk",),
    "piimä": ("milk",),
    "yoghurt": ("milk",),
    "yogurt": ("milk",),
    "jogurtti": ("milk",),
    "quark": ("milk",),
    "rahka": ("milk",),
}
_QUERY_ALLERGEN_TERMS: dict[str, tuple[str, ...]] = {
    **{k: v for k, v in ALLERGEN_ALIASES.items() if k not in _GS1_SHORT_CODES and len(k) >= 3},
    **_PRODUCT_ALLERGEN_TERMS,
}
_FREE_CLAIM_RE = re.compile(
    r"\b(?:gluten|lactose|milk|dairy|nut|nuts|tree\s+nuts?|egg|eggs|soy|soya|wheat|peanut|peanuts|sesame)"
    r"\s*-?\s*free\b"
    r"|\b(?:gluteeniton\w*|glutenfri\w*|laktoositon\w*|laktosfri\w*|maidoton\w*|maidottom\w*|pähkinätön\w*)\b",
    re.IGNORECASE,
)
_NUT_BUTTER_RE = re.compile(
    r"\b(peanut|peanuts|almond|almonds|cashew|cashews|hazelnut|hazelnuts)\s+butter\b",
    re.IGNORECASE,
)
_NON_DAIRY_FALSE_FRIEND_RE = re.compile(
    r"\b(?:coconut\s+milk|(?:cocoa|shea)\s+butter|kookosmaito\w*|kaakaovoi\w*)\b",
    re.IGNORECASE,
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
    except Exception:  # noqa: BLE001  unknown customer / trust missing
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
    pol = _policy(customer_id)
    out: list[str] = []
    for r in pol.get("rules") or []:
        if isinstance(r, dict) and r.get("type") == "allergen_block":
            out.extend(r.get("excluded") or [])
    return normalize_allergens(out)


def excluded_allergens_in_query(query: str, excluded: Iterable[str]) -> list[str]:
    excl_list = normalize_allergens(excluded)
    excl = set(excl_list)
    if not excl or not query:
        return []
    try:
        from aimo.catalog.plan import _excluded_groups_named

        lit, imp = _excluded_groups_named(query, excl_list)
        out = list(dict.fromkeys([*lit, *imp]))
        if "milk" in excl and "milk" not in out and re.search(r"\bbuttermilk\b", query, re.IGNORECASE):
            if not _FREE_CLAIM_RE.search(query):
                out.append("milk")
        return out
    except Exception:
        pass
    try:
        from aimo.catalog.textsearch import clean_query

        cleaned = clean_query(query)
    except Exception:
        cleaned = query
    low = _FREE_CLAIM_RE.sub(" ", cleaned).lower()
    low = _NUT_BUTTER_RE.sub(r"\1", low)
    low = _NON_DAIRY_FALSE_FRIEND_RE.sub(" ", low)
    low = re.sub(r"\s+", " ", low).strip()
    if not low:
        return []
    hit: list[str] = []
    for phrase, groups in _QUERY_ALLERGEN_TERMS.items():
        matched = [g for g in groups if g in excl]
        if matched and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", low):
            for g in matched:
                if g not in hit:
                    hit.append(g)
    return hit


def need_names_excluded_allergen(need: Need | None, excluded: Iterable[str]) -> bool:
    if need is None:
        return False
    return bool(excluded_allergens_in_query(need.product_query or "", excluded))


def usual_matches_allergen_query(query: str, u_row: dict[str, Any]) -> bool:
    u_name = " ".join(str(u_row.get(k) or "") for k in ("name", "name_en", "name_fi", "name_sv")).lower()
    q_low = (query or "").lower()
    if re.search(r"\b(?:milk|maito\w*|mjölk\w*|dairy)\b", q_low) and not re.search(
        r"\b(?:butter|voi\b|smör|cheese|juusto|cream|kerma|grädde|yoghurt|yogurt|jogurtti|sour\s*milk|piimä|powder|jauhe)\b",
        q_low,
    ):
        return bool(
            u_row.get("content_unit") == "L"
            and re.search(r"\b(?:drink|juoma|dryck|oat|kaura\w*|havre\w*|soy|soija\w*)\b", u_name)
        )
    if re.search(r"\b(?:bread|leipä\w*|bröd\w*|gluten)\b", q_low) and not re.search(
        r"\b(?:flour|jauho\w*|mjöl\b|pasta)\b",
        q_low,
    ):
        return bool(re.search(r"\b(?:bread|leipä\w*|bröd\w*)\b", u_name))
    return False


def _same_kind(a: dict[str, Any], b: dict[str, Any], product_types: Callable) -> bool:
    """A usual product is the reference for a line when both are the same kind of product."""
    ta, tb = product_types(a), product_types(b)
    if ta and tb:
        return bool(ta & tb)
    return bool(a.get("category")) and a.get("category") == b.get("category")


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
    if _policy is _default_policy:
        excluded = effective_excluded_allergens(customer_id, caller_excl)
    else:
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
    except Exception as e:  # noqa: BLE001  catalog not built: conservative fallback
        log.warning("catalog ranking unavailable for quote verification: %s", e)

    for pid, qty in qty_by.items():
        o = offers.get(pid)
        flags: list[str] = []
        reasons: list[str] = []
        tier = "preferred" if pid in usual else None
        method = "usual_products" if tier else "fallback"
        reference: str | None = None
        row = rows.get(pid)
        row_blk = blocking(row.get("allergens") or {}, excluded, allergen_source=row.get("allergen_source")) if (row is not None and excluded) else []
        if rank is not None and row is not None:
            # usual products of the same kind are the reference (a milk standing order says nothing about butter)
            same_kind = [u for u in usual if u in rows and u != pid and _same_kind(rows[u], row, product_types)]
            reference = same_kind[0] if same_kind else None
            if need is not None:
                eff_query = (row.get("name") or pid) if (names_excluded and not row_blk and not same_kind) else need.product_query
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
            except Exception as e:  # noqa: BLE001
                log.warning("rank_substitutes failed for %s: %s", pid, e)
                c = None
            if c is not None:
                if pid not in usual or (c.tier == "rejected" and row_blk):
                    tier, method = c.tier, "catalog.rank_substitutes"
                flags += [f for f in c.flags if f in DERIVED_FLAGS]
                reasons += c.reasons[:3]
        if tier is None:
            tier = "good"
            flags.append("tier_unverified")
            reasons.append("Product could not be classified against the catalog; treated as a substitute (good).")
        if method == "fallback" and usual and pid not in usual:
            flags.append("outside_preference")  # kind unknown without the catalog: be conservative
        # live offer facts
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
        # allergen status straight from the catalog when the ranking did not run
        p = products.get(pid)
        p_blk = blocking(p.allergens, excluded, allergen_source=p.allergen_source) if (p is not None and excluded) else []
        if p_blk:
            if pid in usual:
                tier = "rejected"
            for g, st in p_blk:
                if st == "unknown" and "allergen_unknown" not in flags:
                    flags.append("allergen_unknown")
                elif st in ("contains", "may_contain") and f"{st}:{g}" not in flags:
                    flags.append(f"{st}:{g}")
        if names_excluded and tier != "rejected" and "allergen_unknown" not in flags and not p_blk and not row_blk:
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
