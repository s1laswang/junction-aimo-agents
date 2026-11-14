"""plan_need: candidates -> live offers (top ~30 only) -> good-better-best -> greedy split.

Quantities are pure arithmetic on catalog/offer fields (never an LLM):
  units = ceil(remaining / content_per_sales_unit), rounded up to allowedLotSize,
  capped by available_amount (rounded down to the lot size).
"""
from __future__ import annotations

import math
import os
from typing import Any, Callable, Optional

from aimo.catalog.allergens import blocking
from aimo.catalog.textsearch import clean_query, fts_search, get_rows, parse_query, _con
from aimo.catalog.substitution import TIER_ORDER, rank_substitutes
from aimo.models import Candidate, Need, Offer, Plan, PlanLine

MAX_OFFER_IDS = 30


def effective_query(need: Need) -> tuple[str, str]:
    """(search query, source). need.product_query wins; allergen-exclusion mentions are stripped
    (they must never feed search or product-type inference). need.raw_text is only a weak
    fallback when the product_query yields nothing searchable (e.g. a mis-parsed 'by Wednesday')."""
    q1 = clean_query(need.product_query)
    if parse_query(q1) and fts_search(q1, claims=need.required_claims, limit=1):
        return q1, "product_query"
    if need.raw_text:
        q2 = clean_query(need.raw_text)
        if parse_query(q2) and fts_search(q2, claims=need.required_claims, limit=1):
            return " ".join(c for c, _ in parse_query(q2)), "raw_text"   # 'oat drink', not the whole sentence
    return q1 or need.product_query, "product_query"


def with_effective_query(need: Need) -> tuple[Need, str]:
    q, src = effective_query(need)
    return (need if q == need.product_query else need.model_copy(update={"product_query": q})), src


def find_candidates(need: Need, pool: int = 80) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    """Static candidate pool: text search (with required claims), the preferred products, products
    in the preferred products' category with the claims, supplier substitution rules, and
    (optionally) embedding neighbours."""
    rel: dict[str, float] = {}
    for pid, s in fts_search(need.product_query, claims=need.required_claims, content_unit=None, limit=pool):
        rel[pid] = s
    if need.excluded_allergens:  # make sure allergen-safe products (structured 'free') are in the pool
        for pid, s in fts_search(need.product_query, claims=need.required_claims, free_of=need.excluded_allergens,
                                 limit=pool):
            rel[pid] = max(rel.get(pid, -1e9), s) + 2.0
    pref_rows = get_rows(list(need.preferred_product_ids))
    con = _con()
    cats = {r.get("category") for r in pref_rows.values() if r.get("category")}
    for cat in cats:
        for pid, s in fts_search(need.product_query, claims=need.required_claims, category=cat, limit=pool):
            rel[pid] = max(rel.get(pid, -1e9), s) + 1.0  # same category as the usual product
    for pid in need.preferred_product_ids:
        rel.setdefault(pid, 100.0)
        for r in con.execute("select substitute from subs where product = ?", (pid,)):
            rel.setdefault(r[0], 5.0)
    try:  # Gemini semantic recall for the query itself (adds products FTS misses)
        from aimo.catalog.textsearch import semantic_hits
        for pid, sim in semantic_hits(need.product_query, claims=need.required_claims, k=30):
            rel.setdefault(pid, 6.0 * sim)
    except Exception:  # noqa: BLE001
        pass
    try:  # semantic neighbours of the usual product (Gemini embeddings), if the cache exists
        from aimo.catalog.embeddings import similar_products
        for pid in need.preferred_product_ids:
            for sid, sim in similar_products(pid, k=15):
                rel.setdefault(sid, 4.0 * float(sim))
    except Exception:  # noqa: BLE001
        pass
    rows = get_rows(list(rel))
    return rows, rel


def _static_priority(need: Need, d: dict[str, Any], rel: float) -> tuple:
    ok_claims = set(need.required_claims) <= set(d.get("claims") or [])
    ok_unit = d.get("content_unit") == need.unit
    safe = not blocking(d.get("allergens") or {}, need.excluded_allergens)
    return (0 if d["id"] in need.preferred_product_ids else 1, 0 if (ok_claims and ok_unit) else 1,
            0 if safe else 1, -rel)


def _round_lot(units: float, lot: Optional[float], up: bool = True) -> float:
    """Whole sales units, multiple of allowedLotSize (lot sizes like 0.01 mean 'any whole unit')."""
    step = 1.0
    if lot and lot >= 1:
        step = float(lot)
    q = units / step
    q = math.ceil(q - 1e-9) if up else math.floor(q + 1e-9)
    return q * step


def plan_need(need: Need, offers_fn: Optional[Callable[[list[str], Any], dict[str, Offer]]] = None,
              limit: int = 12) -> Plan:
    if offers_fn is None:
        from aimo.catalog.offers import get_offers as offers_fn  # type: ignore[no-redef]
    original = need
    need, qsrc = with_effective_query(need)
    rows, rel = find_candidates(need)
    order = sorted(rows.values(), key=lambda d: _static_priority(need, d, rel.get(d["id"], 0)))
    top_ids = [d["id"] for d in order[:MAX_OFFER_IDS]]
    for pid in need.preferred_product_ids:
        if pid in rows and pid not in top_ids:
            top_ids.append(pid)
    offers = _call_offers(offers_fn, top_ids, need) if top_ids else {}
    shortlist = {pid: rows[pid] for pid in top_ids}
    hints: dict[str, dict[str, str]] = {}
    if need.excluded_allergens:
        try:
            from aimo.catalog.gemini_allergens import cached_inference
            for pid in shortlist:
                h = cached_inference(pid)
                if h:
                    hints[pid] = h
        except Exception:  # noqa: BLE001
            pass
    full = rank_substitutes(need, offers, limit=200, rows=shortlist, relevance=rel, inferred_allergens=hints)
    cands = _trim(full, limit)
    plan = fill(need, cands)
    notes = []
    if need.product_query != original.product_query:
        notes.append(f"Searched the catalog for '{need.product_query}'"
                     + (" (taken from the request text because the parsed product phrase found nothing)"
                        if qsrc == "raw_text" else " (allergen mentions removed from the product phrase; they are"
                        " enforced as exclusions, not searched for)") + ".")
    advice = shortfall_advice(need, plan, full)
    plan = plan.model_copy(update={"need": original,
                                   "explanation": " ".join([*notes, plan.explanation, *advice]).strip()})
    if os.environ.get("AIMO_RECIPE_CHECK", "1") != "0":
        _recipe_checks(original, plan)
    return plan


def _trim(full: list[Candidate], limit: int) -> list[Candidate]:
    out, n_ok, n_rej = [], 0, 0
    for c in full:
        if c.tier == "rejected":
            if n_rej >= max(6, limit // 2):
                continue
            n_rej += 1
        else:
            if n_ok >= limit:
                continue
            n_ok += 1
        out.append(c)
    return out


def shortfall_advice(need: Need, plan: Plan, full: list[Candidate]) -> list[str]:
    """When the plan does not cover the need, say why in numbers and what a human can do."""
    if plan.shortfall <= 1e-9:
        return []
    rej = [c for c in full if c.tier == "rejected"]
    excl = sorted(set(need.excluded_allergens))

    def first(c: Candidate) -> str:
        return c.reasons[0] if c.reasons else ""

    n_cut = sum(1 for c in rej if "cutoff_passed" in c.flags)
    n_unav = sum(1 for c in rej if "unavailable" in c.flags or "sales_block" in c.flags)
    n_unknown = sum(1 for c in rej if "allergen_unknown" in c.flags)
    n_contains = sum(1 for c in rej if "contain per supplier data" in first(c) or "contains per supplier data" in first(c))
    n_claim = sum(1 for c in rej if "missing required claim" in first(c))
    out = [f"Why not covered: {len(full)} candidates evaluated, {len(full) - len(rej)} orderable"
           + (f" (covering {plan.covered_amount:g} {need.unit})" if plan.lines else "") + "."]
    bits = []
    if n_contains:
        bits.append(f"{n_contains} contain or may contain {', '.join(excl)} per supplier data")
    if n_unknown:
        bits.append(f"{n_unknown} have no structured allergen data for {', '.join(excl)} (unknown is never treated as safe)")
    if n_cut:
        bits.append(f"{n_cut} missed the order cut-off for {need.delivery_by:%a %d %b}")
    if n_unav:
        bits.append(f"{n_unav} are not available or sales-blocked for that date")
    if n_claim:
        bits.append(f"{n_claim} lack the required claim(s) {', '.join(need.required_claims)}")
    if bits:
        out.append("Rejected: " + "; ".join(bits) + ".")
    actions = []
    if n_cut or n_unav:
        actions.append("re-plan for a later delivery date (cut-off-blocked products become orderable)")
    if n_unknown:
        actions.append("have a qualified person confirm allergen status from the label for the 'unknown' candidates "
                       "(Gemini ingredient hints are shown in their reasons)")
    if plan.lines:
        actions.append(f"accept the partial plan and source the remaining {plan.shortfall:g} {need.unit} separately")
    actions.append("broaden the product phrase or relax a non-safety preference")
    out.append("What a human can do: " + "; ".join(actions) + ".")
    return out


def _recipe_checks(need: Need, plan: Plan, timeout_s: float = 8.0) -> None:
    """Gemini recipe-compatibility note for substitute lines vs the usual product (cached; flags only)."""
    if not need.preferred_product_ids:
        return
    from aimo.catalog.netcheck import gemini_online
    if not gemini_online():
        return
    orig = need.preferred_product_ids[0]
    subs = [l for l in plan.lines if l.product_id not in need.preferred_product_ids][:3]
    if not subs:
        return
    use = str((need.preferences or {}).get("use") or need.raw_text or need.product_query)
    try:
        from concurrent.futures import ThreadPoolExecutor, wait

        from aimo import gemini
        from aimo.catalog.gemini_allergens import recipe_compatible
        gemini.client()
        ex = ThreadPoolExecutor(max_workers=3)
        try:
            futs = {ex.submit(recipe_compatible, orig, l.product_id, use): l for l in subs}
            done, _ = wait(futs, timeout=timeout_s)
            for f in done:
                l = futs[f]
                try:
                    j = f.result()
                except Exception:  # noqa: BLE001
                    continue
                verdict = "compatible" if j.get("compatible") else "NOT compatible"
                l.reasons.append(f"Gemini recipe check vs usual product: {verdict} ({j.get('confidence')}): "
                                 f"{j.get('reason')} [inferred:gemini]")
                if not j.get("compatible"):
                    l.flags = sorted(set(l.flags) | {"recipe_check_failed"})
        finally:
            ex.shutdown(wait=False, cancel_futures=True)   # never block the plan on a slow LLM
    except Exception:  # noqa: BLE001
        return


def _call_offers(fn, ids: list[str], need: Need) -> dict[str, Offer]:
    try:
        return fn(ids, need.delivery_by, customer_id=need.customer_id)
    except TypeError:
        return fn(ids, need.delivery_by)


def fill(need: Need, cands: list[Candidate]) -> Plan:
    remaining = float(need.quantity)
    lines: list[PlanLine] = []
    from aimo.catalog.textsearch import get_rows as _gr
    lot = {pid: d.get("allowed_lot_size") for pid, d in _gr([c.product.id for c in cands]).items()}
    for c in sorted(cands, key=lambda c: (TIER_ORDER[c.tier], -c.rank_score)):
        if remaining <= 1e-9:
            break
        if c.tier == "rejected" or c.offer is None or c.offer.unit_price is None:
            continue
        content = c.product.content_amount
        if not content or content <= 0 or c.product.content_unit != need.unit:
            continue
        units = _round_lot(remaining / content, lot.get(c.product.id), up=True)
        flags = list(c.flags)
        avail = c.offer.available_amount
        if avail is not None and avail < units:
            units = _round_lot(avail, lot.get(c.product.id), up=False)
            if "partial_stock" not in flags:
                flags.append("partial_stock")
        if units <= 0:
            continue
        covered = round(units * content, 4)
        total = round(units * c.offer.unit_price, 2)
        reasons = [c.reasons[0]] if c.reasons else []
        reasons.append(f"{units:g} x {c.product.sales_unit} x {content:g} {need.unit} = {covered:g} {need.unit}; "
                       f"{units:g} x €{c.offer.unit_price:.2f} = €{total:,.2f}")
        lines.append(PlanLine(product_id=c.product.id, product_name=c.product.name, tier=c.tier,
                              qty_sales_units=units, sales_unit=c.product.sales_unit, covered_amount=covered,
                              unit_price=c.offer.unit_price, line_total=total, reasons=reasons, flags=sorted(set(flags))))
        remaining -= covered
    covered_total = round(sum(l.covered_amount for l in lines), 4)
    total_net = round(sum(l.line_total for l in lines), 2)
    shortfall = round(max(0.0, need.quantity - covered_total), 4)
    return Plan(need=need, lines=lines, candidates=cands, covered_amount=covered_total, shortfall=shortfall,
                total_net=total_net, explanation=explain(need, lines, cands, covered_total, shortfall, total_net))


def explain(need: Need, lines: list[PlanLine], cands: list[Candidate], covered: float, shortfall: float,
            total: float) -> str:
    parts = [f"Need: {need.quantity:g} {need.unit} of '{need.product_query}'"
             + (f" with {', '.join(need.required_claims)}" if need.required_claims else "")
             + (f", excluding {', '.join(need.excluded_allergens)}" if need.excluded_allergens else "")
             + f", delivered by {need.delivery_by:%a %d %b %Y}."]
    for c in cands:
        if c.product.id in need.preferred_product_ids and c.tier == "rejected":
            why = c.reasons[0].removeprefix("Rejected: ") if c.reasons else "not orderable"
            parts.append(f"Usual product {c.product.name} ({c.product.id}) cannot be used: {why}.")
    for l in lines:
        parts.append(f"{l.tier.upper()}: {l.qty_sales_units:g} {l.sales_unit} of {l.product_name} ({l.product_id}) "
                     f"covers {l.covered_amount:g} {need.unit} for €{l.line_total:,.2f}.")
    if not lines:
        parts.append("No orderable candidate met the hard constraints.")
    parts.append(f"Total {covered:g} {need.unit} for €{total:,.2f} net"
                 + (f"; shortfall {shortfall:g} {need.unit}." if shortfall > 0 else "."))
    n_rej = sum(1 for c in cands if c.tier == "rejected")
    if n_rej:
        parts.append(f"{n_rej} candidate(s) rejected; see candidates[].reasons.")
    return " ".join(parts)
