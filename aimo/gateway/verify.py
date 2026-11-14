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
from typing import Any, Callable

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
                 "allergen_inferred_hint", "content_from_price", "no_live_offer")


def _policy(customer_id: str) -> dict:
    try:
        from aimo.trust import load_policy

        return load_policy(customer_id) or {}
    except Exception:  # noqa: BLE001  unknown customer / trust missing
        return {}


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
        if r.get("type") == "allergen_block":
            out.extend(r.get("excluded") or [])
    return list(dict.fromkeys(out))


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
    excluded = list(dict.fromkeys([*(need.excluded_allergens if need else []), *policy_excluded_allergens(customer_id)]))
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
        if rank is not None and row is not None:
            # usual products of the same kind are the reference (a milk standing order says nothing about butter)
            same_kind = [u for u in usual if u in rows and u != pid and _same_kind(rows[u], row, product_types)]
            reference = same_kind[0] if same_kind else None
            if need is not None:
                ln_need = need.model_copy(update={
                    "customer_id": customer_id, "preferred_product_ids": same_kind, "preferences": {},
                    "excluded_allergens": excluded, "delivery_by": delivery_date})
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
                if pid not in usual:
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
        if p is not None and excluded and "allergen_unknown" not in flags:
            if any((p.allergens.get(g) is None or getattr(p.allergens.get(g), "value", p.allergens.get(g)) == "unknown")
                   for g in excluded):
                flags.append("allergen_unknown")
        if p is not None:
            sc = screen(p)
            if sc.get("suspicious"):
                flags.append("supplier_text_injection_suspected")
                if "injected:inject_text" in (p.labels or []):
                    flags.append("injected:inject_text")
        out[pid] = {"tier": tier, "flags": list(dict.fromkeys(flags)), "reasons": reasons, "method": method,
                    "reference": reference}
    return out
