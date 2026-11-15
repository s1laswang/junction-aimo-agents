"""plan_need: candidates -> live offers (top ~30 only) -> good-better-best -> greedy split.

Quantities are pure arithmetic on catalog/offer fields (never an LLM):
  units = ceil(remaining / content_per_sales_unit), rounded up to allowedLotSize,
  capped by available_amount (rounded down to the lot size).
"""
from __future__ import annotations

import math
import os
import re
from datetime import date
from functools import lru_cache
from typing import Any, Callable, Optional

from aimo.catalog.allergens import blocking
from aimo.catalog.textsearch import clean_query, fts_search, get_rows, parse_query, _con
from aimo.catalog.substitution import TIER_ORDER, rank_substitutes
from aimo.models import Candidate, Need, Offer, Plan, PlanLine

MAX_OFFER_IDS = 30
SUPPLIER_TEXT_REASON = "Supplier text contains instructions aimed at AI agents"


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


def usual_products_for(need: Need) -> list[str]:
    """The customer's usual products (policy `preferred_products`) that are what this request asks for:
    same unit, every required claim, no excluded allergen, and the product phrase matches the product.
    Used only when the request names no usual product itself, so 'oat drink' at the school picks the
    school's usual oat drink instead of the cheapest exact match (fewer pointless substitutions)."""
    if need.preferred_product_ids or any((need.preferences or {}).get(k) for k in ("brand", "fat_pct", "organic", "pack")):
        return []   # the request says what it wants (a usual product, or a brand/spec): that wins over the profile
    try:
        from aimo.trust.policy import planning_profile
        usual = planning_profile(need.customer_id).get("usual_products") or []
    except Exception:  # noqa: BLE001
        return []
    if not usual:
        return []
    from aimo.catalog.substitution import missing_query_concepts
    out = []
    for pid, d in get_rows(list(usual)).items():
        if d.get("content_unit") != need.unit or not set(need.required_claims) <= set(d.get("claims") or []):
            continue
        if blocking(d.get("allergens") or {}, need.excluded_allergens) or missing_query_concepts(need.product_query, d):
            continue
        out.append(pid)
    return out


def _excluded_groups_named(query: str, excluded: list[str]) -> tuple[list[str], list[str]]:
    """Excluded allergen groups the product phrase itself names: (literal, implied). Literal: the group's own
    name or alias ('milk', 'dairy', 'maito'). Implied: a food that normally is that allergen ('cream',
    'butter', 'cheese' for milk; 'wheat flour' for gluten). Plant-based phrases ('oat drink') name nothing."""
    from aimo.catalog.allergens import GROUP_KEYWORDS
    from aimo.catalog.substitution import normalize_allergens
    words = re.findall(r"[a-zåäö']+", (query or "").lower())
    literal = set(normalize_allergens(words)) | set(normalize_allergens([" ".join(words)]))
    implied = {g for g, kws in GROUP_KEYWORDS.items() if any(w in kws for w in words)}
    if {"oat", "kaura", "havre", "soy", "soija", "almond", "coconut", "rice", "plant", "vegan"} & set(words):
        implied.discard("milk")    # 'oat cream', 'soy milk': the phrase already asks for a milk-free product
        literal.discard("milk")
    return [g for g in excluded if g in literal], [g for g in excluded if g in implied and g not in literal]


@lru_cache(maxsize=4096)
def _screen_supplier_text(text: str) -> tuple[str, ...]:
    """Strong prompt-injection signals in supplier-written text (trust's regex screen; no LLM call)."""
    try:
        from aimo.trust.screening import screen_text
        s = screen_text(text, use_gemini=False)
    except Exception:  # noqa: BLE001
        return ()
    return tuple(s.get("strong_reasons") or s.get("reasons") or ["suspicious"]) if s.get("suspicious") else ()


def mark_supplier_text(cands: list[Candidate], rows: dict[str, dict[str, Any]],
                       texts: Optional[dict[str, str]] = None) -> None:
    """Put the injection-screen result on each candidate whose supplier text tries to instruct an agent:
    flag `supplier_text_injection_suspected` (same name as the gateway quote flag) plus a reason. The text is never followed; the policy's injection rule
    sends any order with such a line to a human. `texts` overrides the catalog text (e.g. live product reads)."""
    for c in cands:
        text = (texts or {}).get(c.product.id) or (rows.get(c.product.id) or {}).get("marketing_text") or ""
        hits = _screen_supplier_text(text) if text.strip() else ()
        c.injection_screen = {"suspicious": bool(hits), "reasons": list(hits[:5]), "screened": bool(text.strip())}
        if hits and "supplier_text_injection_suspected" not in c.flags:
            c.flags.append("supplier_text_injection_suspected")
            c.reasons.append(SUPPLIER_TEXT_REASON + " (" + ", ".join(hits[:3])
                             + "); ignored by the agent, and an order with this line goes to a human.")


def plan_need(need: Need, offers_fn: Optional[Callable[[list[str], Any], dict[str, Offer]]] = None,
              limit: int = 12) -> Plan:
    if offers_fn is None:
        from aimo.catalog.offers import get_offers as offers_fn  # type: ignore[no-redef]
    from aimo.catalog.substitution import policy_allergens, with_policy_allergens
    original = need
    need, policy_added = with_policy_allergens(need)
    need, qsrc = with_effective_query(need)
    usual = usual_products_for(need)
    if usual:
        need = need.model_copy(update={"preferred_product_ids": usual,
                                       "preferences": {**(need.preferences or {}), "usual_from_profile": usual}})
    plan, full, late_notes = _plan_core(need, offers_fn, limit)
    notes = []
    if policy_added:
        _excl, why, pid = policy_allergens(need.customer_id)
        notes.append(f"Customer policy {pid} excludes {', '.join(policy_added)}" + (f" ({why})" if why else "")
                     + "; enforced as a hard block even though the request did not say so.")
    if usual:
        names = ", ".join(f"{l.product_name} ({l.product_id})" for l in plan.lines if l.product_id in usual)
        notes.append(f"Started from the customer's usual product{'s' if len(usual) > 1 else ''} for this request"
                     + (f": {names}." if names else f" ({', '.join(usual)}), which cannot be used here."))
    literal, implied = _excluded_groups_named(need.product_query, need.excluded_allergens)
    if literal:
        alt_plan, alt_note = _allergen_safe_alternative(need, plan, offers_fn, limit, literal)
        if alt_plan is not None:
            plan, full, late_notes = alt_plan, list(alt_plan.candidates), []
        notes.append(alt_note)
    named = literal + implied
    if named:
        for l in plan.lines:
            if "allergen_safe_alternative" not in l.flags:
                l.flags = sorted(set(l.flags) | {"allergen_safe_alternative"})
                l.reasons.insert(0, f"The request names '{need.product_query}', which normally contains "
                                    f"{' and '.join(named)} (excluded by policy). This product is {' and '.join(named)}-free "
                                    f"per supplier data; a human confirms it is a suitable replacement.")
        if implied and not literal:
            notes.append(f"'{need.product_query}' normally contains {' and '.join(implied)}, which this customer's "
                         f"policy excludes: only products confirmed {' and '.join(implied)}-free are planned, and each "
                         f"needs a human's confirmation.")
    if need.product_query != original.product_query:
        notes.append(f"Searched the catalog for '{need.product_query}'"
                     + (" (taken from the request text because the parsed product phrase found nothing)"
                        if qsrc == "raw_text" else " (allergen mentions removed from the product phrase; they are"
                        " enforced as exclusions, not searched for)") + ".")
    advice =shortfall_advice(need, plan, full) if plan.shortfall > 1e-9 else []
    cover = coverage_sentence(need, plan)
    out_need = original if not policy_added else original.model_copy(
        update={"excluded_allergens": list(need.excluded_allergens)})
    plan = plan.model_copy(update={"need": out_need, "explanation": " ".join(
        [*notes, cover, plan.explanation, *late_notes, *advice]).strip()})
    if os.environ.get("AIMO_RECIPE_CHECK", "1") != "0":
        _recipe_checks(need, plan)
    return plan


def _allergen_safe_alternative(need: Need, plan: Plan, offers_fn, limit: int,
                               named: list[str]) -> tuple[Optional[Plan], str]:
    """The request names a product the customer's policy excludes ('milk' at a milk-free school). Never
    plan it. If the customer has a usual product that is free of the excluded groups, in the same unit and
    with the required claims, plan that instead, flagged `allergen_safe_alternative` (a human confirms the
    swap); otherwise say so and leave the shortfall explicit."""
    try:
        from aimo.trust.policy import planning_profile
        usual = planning_profile(need.customer_id).get("usual_products") or []
    except Exception:  # noqa: BLE001
        usual = []
    rows = get_rows(list(usual)) if usual else {}
    safe = [pid for pid, d in rows.items() if d.get("content_unit") == need.unit
            and set(need.required_claims) <= set(d.get("claims") or [])
            and not blocking(d.get("allergens") or {}, need.excluded_allergens)]
    head = (f"'{need.product_query}' is itself {' and '.join(named)}, which this customer's policy excludes, "
            f"so no {' or '.join(named)} product is planned")
    if not safe:
        return None, head + "; the shortfall is left for a human (no allergen-safe usual product on file)."
    alt_need = need.model_copy(update={"preferred_product_ids": safe,
                                       "product_query": rows[safe[0]].get("name_en") or rows[safe[0]].get("name") or ""})
    alt, _full, _late = _plan_core(alt_need, offers_fn, limit)
    alt_lines = [l for l in alt.lines if l.product_id in safe]
    if not alt_lines:
        return None, head + f"; the customer's allergen-safe usual product ({', '.join(safe)}) is not orderable for this date."
    for l in alt_lines:
        l.flags = sorted(set(l.flags) | {"allergen_safe_alternative"})
        l.reasons.insert(0, f"Allergen-safe alternative: the request asked for '{need.product_query}', which contains "
                            f"{' and '.join(named)} (excluded by policy). This is the customer's usual "
                            f"{' and '.join(named)}-free product; a human confirms the swap.")
    covered = round(sum(l.covered_amount for l in alt_lines), 4)
    alt = alt.model_copy(update={"need": need, "lines": alt_lines, "covered_amount": covered,
                                 "total_net": round(sum(l.line_total for l in alt_lines), 2),
                                 "shortfall": round(max(0.0, need.quantity - covered), 4),
                                 "candidates": [*alt.candidates, *[c for c in plan.candidates if c.tier == "rejected"][:6]]})
    names = ", ".join(f"{l.product_name} ({l.product_id})" for l in alt_lines)
    return alt, head + f"; planned the customer's usual allergen-safe product instead: {names} (needs a human's confirmation)."


def _plan_core(need: Need, offers_fn, limit: int) -> tuple[Plan, list[Candidate]]:
    """Candidates -> offers -> ranking -> greedy fill (+ late split). `need` already carries the policy's
    allergen exclusions and the effective query."""
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
    mark_supplier_text(full, shortlist)
    cands = _trim(full, limit)
    plan = fill(need, cands)
    late_notes: list[str] = []
    if plan.shortfall > 1e-9:
        plan, late_notes = _late_split(need, plan, full, shortlist, rel, hints)
    return plan, full, late_notes


_ORDERABILITY = ("order cut-off passed", "earliest delivery", "not available for the requested date")


def _only_orderability(c: Candidate) -> bool:
    if c.tier != "rejected" or not c.reasons or not c.reasons[0].startswith("Rejected: "):
        return False
    parts = c.reasons[0].removeprefix("Rejected: ").split("; ")
    return bool(parts) and all(any(x.startswith(o) for o in _ORDERABILITY) for x in parts)


def _late_split(need: Need, plan: Plan, full: list[Candidate], shortlist: dict, rel: dict,
                hints: dict) -> tuple[Plan, list[str]]:
    """Cover a shortfall with products that are only blocked by date (cut-off / late availability).
    Lines after delivery_by always carry `late_option` + `delivery_date:<iso>`; they are added to the
    plan only when need.preferences['allow_split_delivery'] is true, otherwise reported as the
    earliest date that covers the rest."""
    from aimo.catalog.delivery import earliest_feasible, fmt, late_offer
    allow = bool((need.preferences or {}).get("allow_split_delivery"))
    late: dict[str, tuple[Candidate, date, bool]] = {}
    for c in full:
        if not _only_orderability(c) or c.offer is None:
            continue
        d, est = earliest_feasible(c.offer)
        if d is None or d <= need.delivery_by and not c.offer.cutoff_passed:
            continue
        late[c.product.id] = (c, d, est)
        if "late_option" not in c.flags:
            c.flags.append("late_option")
            c.reasons.append(f"late option: earliest delivery {fmt(d)}" + (" (estimated from the product's lead time;"
                             " the API's closing time does not move with the requested date)" if est else ""))
    if not late:
        return plan, []
    last = max(d for _c, d, _e in late.values())
    late_need = need.model_copy(update={"delivery_by": last})
    late_offers = {pid: late_offer(c.offer, d) for pid, (c, d, _e) in late.items()}
    ranked = rank_substitutes(late_need, late_offers, limit=50, rows={p: shortlist[p] for p in late if p in shortlist},
                              relevance=rel, inferred_allergens=hints)
    ranked = [c for c in ranked if c.tier != "rejected"]
    ranked.sort(key=lambda c: (late[c.product.id][1], TIER_ORDER[c.tier], -c.rank_score))
    remaining = plan.shortfall
    from aimo.catalog.textsearch import get_rows as _gr
    lots = {pid: d.get("allowed_lot_size") for pid, d in _gr([c.product.id for c in ranked]).items()}
    new_lines: list[PlanLine] = []
    for c in ranked:
        if remaining <= 1e-9:
            break
        content = c.product.content_amount
        if not content or c.product.content_unit != need.unit or c.offer is None or c.offer.unit_price is None:
            continue
        units = _round_lot(remaining / content, lots.get(c.product.id), up=True)
        if c.offer.available_amount is not None and c.offer.available_amount < units:
            units = _round_lot(c.offer.available_amount, lots.get(c.product.id), up=False)
        if units <= 0:
            continue
        d, est = late[c.product.id][1], late[c.product.id][2]
        covered = round(units * content, 4)
        total = round(units * c.offer.unit_price, 2)
        flags = sorted(set([f for f in c.flags if f not in ("cutoff_passed",)] +
                           ["late_option", f"delivery_date:{d.isoformat()}"] + (["split_delivery"] if allow else [])))
        reasons = [c.reasons[0] if c.reasons else "",
                   f"Delivered {fmt(d)}, after the requested {fmt(need.delivery_by)}"
                   + (" (estimated date)" if est else "") + ("; split delivery allowed by the customer"
                                                              if allow else "; needs a human decision"),
                   f"{units:g} x {c.product.sales_unit} x {content:g} {need.unit} = {covered:g} {need.unit}; "
                   f"{units:g} x €{c.offer.unit_price:.2f} = €{total:,.2f}"]
        new_lines.append(PlanLine(product_id=c.product.id, product_name=c.product.name, tier=c.tier,
                                  qty_sales_units=units, sales_unit=c.product.sales_unit, covered_amount=covered,
                                  unit_price=c.offer.unit_price, line_total=total, reasons=reasons, flags=flags,
                                  injection_screen=c.injection_screen))
        remaining -= covered
    if not new_lines:
        return plan, []
    late_cov = sum(l.covered_amount for l in new_lines)
    first = new_lines[-1]
    d_all = max(late[l.product_id][1] for l in new_lines)
    if not allow:
        note = (f"Earliest date that covers the remaining {plan.shortfall:g} {need.unit}: {fmt(d_all)}"
                f" ({', '.join(f'{l.covered_amount:g} {need.unit} of {l.product_name} ({l.product_id})' for l in new_lines)})"
                f"{' (estimated)' if any(late[l.product_id][2] for l in new_lines) else ''}. Not added to the plan: "
                f"re-plan for {fmt(d_all)} or allow split delivery.")
        return plan, [note]
    lines = [*plan.lines, *new_lines]
    covered_total = round(sum(l.covered_amount for l in lines), 4)
    total_net = round(sum(l.line_total for l in lines), 2)
    shortfall = round(max(0.0, need.quantity - covered_total), 4)
    note = (f"Split delivery: {late_cov:g} {need.unit} arrive {fmt(d_all)} (late_option lines, flagged for review).")
    _ = first
    return plan.model_copy(update={"lines": lines, "covered_amount": covered_total, "total_net": total_net,
                                   "shortfall": shortfall}), [note]


def _line_date(need: Need, l: PlanLine) -> date:
    for f in l.flags:
        if f.startswith("delivery_date:"):
            return date.fromisoformat(f.split(":", 1)[1])
    return need.delivery_by


def coverage_sentence(need: Need, plan: Plan) -> str:
    """'1000 L: 600 L Mon 16 Nov (Valio Eila 1 L), 400 L Thu 19 Nov (...); because only 600 L are in stock for Mon 16 Nov.'"""
    from aimo.catalog.delivery import fmt
    if not plan.lines:
        return f"{need.quantity:g} {need.unit}: nothing orderable by {fmt(need.delivery_by)}."
    parts = []
    for l in plan.lines:
        d = _line_date(need, l)
        tag = ", late option" if "late_option" in l.flags else ""
        parts.append(f"{l.covered_amount:g} {need.unit} {fmt(d)} ({l.product_name}{tag})")
    why = []
    for l in plan.lines:
        if "partial_stock" in l.flags and _line_date(need, l) == need.delivery_by:
            why.append(f"only {l.qty_sales_units:g} {l.sales_unit} of {l.product_id} in stock for {fmt(need.delivery_by)}")
    if plan.shortfall > 1e-9:
        why.append(f"{plan.shortfall:g} {need.unit} not covered")
    return (f"{need.quantity:g} {need.unit}: " + ", ".join(parts)
            + (f"; because {', '.join(why)}" if why else "") + ".")


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
        reasons += [r for r in c.reasons[1:] if r.startswith(SUPPLIER_TEXT_REASON)]
        reasons.append(f"{units:g} x {c.product.sales_unit} x {content:g} {need.unit} = {covered:g} {need.unit}; "
                       f"{units:g} x €{c.offer.unit_price:.2f} = €{total:,.2f}")
        lines.append(PlanLine(product_id=c.product.id, product_name=c.product.name, tier=c.tier,
                              qty_sales_units=units, sales_unit=c.product.sales_unit, covered_amount=covered,
                              unit_price=c.offer.unit_price, line_total=total, reasons=reasons, flags=sorted(set(flags)),
                              injection_screen=c.injection_screen))
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
    flagged = list(dict.fromkeys(f"{l.product_name} ({l.product_id})" for l in lines
                                 if (l.injection_screen or {}).get("suspicious")
                                 or "supplier_text_injection_suspected" in l.flags))
    if flagged:
        parts.append(f"Prompt-injection screen: untrusted supplier text on {', '.join(flagged)} was flagged and "
                     f"ignored; an order with this line needs a human approver.")
    return " ".join(parts)
