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
from typing import Any, Callable, Iterable, Optional

import aimo.catalog.textsearch as _ts
from aimo.catalog.allergens import (
    PolicyAllergenBlock,
    blocking,
    effective_excluded_allergens,
    normalize_allergens,
    policy_allergen_block,
)
from aimo.catalog.screening_bridge import screen_reason
from aimo.catalog.textsearch import clean_query, fts_search, get_rows, parse_query, _con
from aimo.catalog.substitution import TIER_ORDER, rank_substitutes, validate_need
from aimo.models import Candidate, Need, Offer, Plan, PlanLine

MAX_OFFER_IDS = 30
SUPPLIER_TEXT_REASON = "Supplier text contains instructions aimed at AI agents"
_ORIG_GET_ROWS = get_rows
_ORIG_FTS_SEARCH = fts_search
_last_policy_dir: Any = None

_LITERAL_ALLERGEN_PHRASES: dict[str, tuple[str, ...]] = {
    "milk": ("milk",),
    "dairy": ("milk",),
    "cow's milk": ("milk",),
    "cows milk": ("milk",),
    "cow milk": ("milk",),
    "milk protein": ("milk",),
    "cow's milk protein": ("milk",),
    "maito": ("milk",),
    "mjölk": ("milk",),
    "maitotuotteet": ("milk",),
    "gluten": ("gluten",),
    "gluteeni": ("gluten",),
    "cereals containing gluten": ("gluten",),
    "nut": ("nuts",),
    "nuts": ("nuts",),
    "tree nut": ("nuts",),
    "tree nuts": ("nuts",),
    "pähkinä": ("nuts",),
    "pähkinät": ("nuts",),
    "nöt": ("nuts",),
    "nötter": ("nuts",),
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
    "soy": ("soy",),
    "soya": ("soy",),
    "soija": ("soy",),
    "soja": ("soy",),
    "soybean": ("soy",),
    "soybeans": ("soy",),
    "fish": ("fish",),
    "kala": ("fish",),
    "fisk": ("fish",),
    "sesame": ("sesame",),
    "sesame seed": ("sesame",),
    "sesame seeds": ("sesame",),
    "seesami": ("sesame",),
    "seesaminsiemenet": ("sesame",),
    "sesam": ("sesame",),
    "celery": ("celery",),
    "selleri": ("celery",),
    "mustard": ("mustard",),
    "sinappi": ("mustard",),
    "senap": ("mustard",),
    "lupin": ("lupin",),
    "lupins": ("lupin",),
    "lupine": ("lupin",),
    "lupiini": ("lupin",),
    "crustacean": ("crustaceans",),
    "crustaceans": ("crustaceans",),
    "äyriäiset": ("crustaceans",),
    "äyriäinen": ("crustaceans",),
    "kräftdjur": ("crustaceans",),
    "mollusc": ("molluscs",),
    "molluscs": ("molluscs",),
    "mollusk": ("molluscs",),
    "mollusks": ("molluscs",),
    "nilviäiset": ("molluscs",),
    "nilviäinen": ("molluscs",),
    "blötdjur": ("molluscs",),
    "shellfish": ("crustaceans", "molluscs"),
    "sulphite": ("sulphites",),
    "sulphites": ("sulphites",),
    "sulfite": ("sulphites",),
    "sulfites": ("sulphites",),
    "sulphur dioxide": ("sulphites",),
    "sulfur dioxide": ("sulphites",),
    "sulfiitit": ("sulphites",),
    "sulfiitti": ("sulphites",),
    "rikkidioksidi": ("sulphites",),
    "sulfit": ("sulphites",),
    "sulfiter": ("sulphites",),
}

_IMPLIED_ALLERGEN_PHRASES: dict[str, tuple[str, ...]] = {
    "rye": ("gluten",),
    "ruis": ("gluten",),
    "råg": ("gluten",),
    "wheat": ("gluten",),
    "vehnä": ("gluten",),
    "vete": ("gluten",),
    "barley": ("gluten",),
    "ohra": ("gluten",),
    "korn": ("gluten",),
    "spelt": ("gluten",),
    "speltti": ("gluten",),
    "kamut": ("gluten",),
    "cream": ("milk",),
    "kerma": ("milk",),
    "grädde": ("milk",),
    "butter": ("milk",),
    "voi": ("milk",),
    "smör": ("milk",),
    "cheese": ("milk",),
    "juusto": ("milk",),
    "whey": ("milk",),
    "hera": ("milk",),
    "yoghurt": ("milk",),
    "yogurt": ("milk",),
    "jogurtti": ("milk",),
    "quark": ("milk",),
    "rahka": ("milk",),
    "piimä": ("milk",),
    "sour milk": ("milk",),
    "almond": ("nuts",),
    "almonds": ("nuts",),
    "manteli": ("nuts",),
    "hazelnut": ("nuts",),
    "hazelnuts": ("nuts",),
    "hasselpähkinä": ("nuts",),
    "walnut": ("nuts",),
    "walnuts": ("nuts",),
    "saksanpähkinä": ("nuts",),
    "cashew": ("nuts",),
    "cashews": ("nuts",),
    "pistachio": ("nuts",),
    "pistachios": ("nuts",),
    "pecan": ("nuts",),
    "pecans": ("nuts",),
    "macadamia": ("nuts",),
}

_PLANT_WORDS = frozenset({
    "oat", "oats", "kaura", "havre", "soy", "soya", "soija", "soja",
    "almond", "almonds", "coconut", "rice", "plant", "vegan", "vegaani",
})
_NUT_BUTTER_WORDS = frozenset({
    "peanut", "peanuts", "almond", "almonds", "cashew", "cashews", "hazelnut", "cocoa", "shea",
})
_GLUTEN_EXCLUSION_RE = re.compile(
    r"\b(?:(?:gluten|gluteeni)[\s-]*free|gluteenit\w*|glutenfri\w*"
    r"|(?:no|without|free[\s-]+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:gluten\w*|gluteen\w*))\b",
    re.I,
)
_MILK_EXCLUSION_RE = re.compile(
    r"\b(?:(?:cow'?s\s+)?(?:milk|dairy|maito|mjölk)[\s-]*free|maidot\w*|mjölkfri\w*"
    r"|(?:no|without|free[\s-]+(?:from|of)|ei|ilman|utan|fri\s+från)\s+(?:cow'?s\s+)?(?:milk|dairy|maito\w*|mjölk))\b",
    re.I,
)


def _load_rows(ids: list[str], *, allow_missing: bool = True) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    fn = get_rows if get_rows is not _ORIG_GET_ROWS else _ts.get_rows
    try:
        return fn(ids)
    except FileNotFoundError:
        if allow_missing:
            return {}
        raise


def _run_fts(query: str, **kwargs: Any) -> list[tuple[str, float]]:
    fn = fts_search if fts_search is not _ORIG_FTS_SEARCH else _ts.fts_search
    return fn(query, **kwargs)


def _profile_usual_products(customer_id: Optional[str]) -> list[str]:
    global _last_policy_dir
    cid = (customer_id or "").strip()
    if not cid:
        return []
    try:
        from aimo.trust import policy as trust_policy

        if getattr(trust_policy, "POLICY_DIR", None) != _last_policy_dir:
            if hasattr(trust_policy, "_load_policy_cached"):
                trust_policy._load_policy_cached.cache_clear()
            _last_policy_dir = getattr(trust_policy, "POLICY_DIR", None)
        if hasattr(trust_policy, "planning_profile"):
            usual = (trust_policy.planning_profile(cid) or {}).get("usual_products") or []
            return [str(p) for p in usual if p]
        pol = trust_policy.load_policy(cid)
    except Exception:
        return []
    if not isinstance(pol, dict):
        return []
    return list(dict.fromkeys(
        str(p["product_id"])
        for p in (pol.get("preferred_products") or [])
        if isinstance(p, dict) and p.get("product_id")
    ))


def effective_query(need: Need) -> tuple[str, str]:
    """(search query, source). need.product_query wins; allergen-exclusion mentions are stripped
    (they must never feed search or product-type inference). need.raw_text is only a weak
    fallback when the product_query yields nothing searchable (e.g. a mis-parsed 'by Wednesday')."""
    q1 = clean_query(need.product_query)
    if parse_query(q1) and _run_fts(q1, claims=need.required_claims, limit=1):
        return q1, "product_query"
    if need.raw_text:
        q2 = clean_query(need.raw_text)
        if parse_query(q2) and _run_fts(q2, claims=need.required_claims, limit=1):
            return " ".join(c for c, _ in parse_query(q2)), "raw_text"
    return q1 or need.product_query, "product_query"


def with_customer_policy(need: Need) -> tuple[Need, list[str], Any]:
    pb = policy_allergen_block(need.customer_id)
    caller_excl = normalize_allergens(need.excluded_allergens)
    merged_excl = effective_excluded_allergens(need.customer_id, need.excluded_allergens)
    if pb.policy_id is None and not pb.excluded and (need.customer_id or "").strip():
        try:
            from aimo.trust import policy as trust_policy

            if hasattr(trust_policy, "planning_profile"):
                prof = trust_policy.planning_profile((need.customer_id or "").strip()) or {}
                if prof:
                    prof_excl = normalize_allergens(prof.get("excluded_allergens") or [])
                    pb = PolicyAllergenBlock(
                        customer_id=(need.customer_id or "").strip(),
                        excluded=tuple(prof_excl),
                        reason=prof.get("allergen_reason"),
                        policy_id=str(prof.get("policy_id") or (need.customer_id or "").strip()),
                    )
                    merged_excl = normalize_allergens([*caller_excl, *prof_excl])
        except Exception:
            pass
    added = [a for a in merged_excl if a not in caller_excl]
    updated = need.model_copy(update={"excluded_allergens": merged_excl}) if merged_excl != list(need.excluded_allergens) else need
    return updated, added, pb


def with_effective_query(need: Need) -> tuple[Need, str]:
    q, src = effective_query(need)
    return (need if q == need.product_query else need.model_copy(update={"product_query": q})), src


def find_candidates(need: Need, pool: int = 80) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    """Static candidate pool: text search (with required claims), the preferred products, products
    in the preferred products' category with the claims, supplier substitution rules, and
    (optionally) embedding neighbours."""
    rel: dict[str, float] = {}
    excl = effective_excluded_allergens(need.customer_id, need.excluded_allergens)
    for pid, s in _run_fts(need.product_query, claims=need.required_claims, content_unit=None, limit=pool):
        rel[pid] = s
    if excl:
        for pid, s in _run_fts(need.product_query, claims=need.required_claims, free_of=excl, limit=pool):
            rel[pid] = max(rel.get(pid, -1e9), s) + 2.0
    pref_rows = _load_rows(list(need.preferred_product_ids), allow_missing=False)
    try:
        con = _con()
    except FileNotFoundError:
        con = None
    cats = {r.get("category") for r in pref_rows.values() if r.get("category")}
    for cat in cats:
        for pid, s in _run_fts(need.product_query, claims=need.required_claims, category=cat, limit=pool):
            rel[pid] = max(rel.get(pid, -1e9), s) + 1.0
    for pid in need.preferred_product_ids:
        rel.setdefault(pid, 100.0)
        if con is not None:
            for r in con.execute("select substitute from subs where product = ?", (pid,)):
                rel.setdefault(r[0], 5.0)
    try:
        from aimo.catalog.textsearch import semantic_hits
        for pid, sim in semantic_hits(need.product_query, claims=need.required_claims, k=30):
            rel.setdefault(pid, 6.0 * sim)
    except Exception:
        pass
    try:
        from aimo.catalog.embeddings import similar_products
        for pid in need.preferred_product_ids:
            for sid, sim in similar_products(pid, k=15):
                rel.setdefault(sid, 4.0 * float(sim))
    except Exception:
        pass
    rows = _load_rows(list(rel), allow_missing=False)
    return rows, rel


def _static_priority(need: Need, d: dict[str, Any], rel: float, excl: Optional[list[str]] = None) -> tuple:
    ok_claims = set(need.required_claims) <= set(d.get("claims") or [])
    ok_unit = d.get("content_unit") == need.unit
    eff_excl = effective_excluded_allergens(need.customer_id, need.excluded_allergens) if excl is None else excl
    safe = not blocking(d.get("allergens") or {}, eff_excl, allergen_source=d.get("allergen_source"))
    return (0 if safe else 1, 0 if d["id"] in need.preferred_product_ids else 1,
            0 if (ok_claims and ok_unit) else 1, -rel)


def _round_lot(units: float, lot: Optional[float], up: bool = True) -> float:
    """Whole sales units, multiple of allowedLotSize (lot sizes like 0.01 mean 'any whole unit')."""
    if isinstance(units, bool) or not isinstance(units, (int, float)):
        raise ValueError("units must be a finite number")
    try:
        u = float(units)
    except OverflowError as e:
        raise ValueError("units must be a finite number") from e
    if not math.isfinite(u):
        raise ValueError("units must be a finite number")
    if u <= 0:
        return 0.0
    step = 1.0
    if lot is not None and not isinstance(lot, bool) and isinstance(lot, (int, float)):
        lf = float(lot)
        if math.isfinite(lf) and lf >= 1:
            step = lf
    q = u / step
    q = math.ceil(q - 1e-9) if up else math.floor(q + 1e-9)
    return q * step


def usual_products_for(need: Need) -> list[str]:
    if need.preferred_product_ids or any(
        (need.preferences or {}).get(k) for k in ("brand", "fat_pct", "organic", "pack")
    ):
        return []
    usual = _profile_usual_products(need.customer_id)
    if not usual:
        return []
    from aimo.catalog.substitution import missing_query_concepts

    eff_excl = effective_excluded_allergens(need.customer_id, need.excluded_allergens)
    rows = _load_rows(list(usual))
    out: list[str] = []
    for pid in usual:
        d = rows.get(pid)
        if not d:
            continue
        if d.get("content_unit") != need.unit or not set(need.required_claims) <= set(d.get("claims") or []):
            continue
        if blocking(d.get("allergens") or {}, eff_excl, allergen_source=d.get("allergen_source")):
            continue
        if missing_query_concepts(need.product_query, d):
            continue
        out.append(pid)
    return out


def _excluded_groups_named(query: str, excluded: list[str]) -> tuple[list[str], list[str]]:
    excl = normalize_allergens(excluded)
    if not excl or not query:
        return [], []
    has_gluten_free = bool(_GLUTEN_EXCLUSION_RE.search(query))
    has_milk_free = bool(_MILK_EXCLUSION_RE.search(query))
    cleaned = _GLUTEN_EXCLUSION_RE.sub(" ", clean_query(query))
    norm = re.sub(r"\u2019", "'", cleaned.lower())
    words = re.findall(r"[a-zåäö']+", norm)
    if not words:
        return [], []
    joined = " " + " ".join(words) + " "
    literal: set[str] = set()
    implied: set[str] = set()
    for phrase, groups in _LITERAL_ALLERGEN_PHRASES.items():
        if f" {phrase} " in joined:
            literal.update(groups)
    for phrase, groups in _IMPLIED_ALLERGEN_PHRASES.items():
        if phrase == "butter" and (set(words) & _NUT_BUTTER_WORDS):
            continue
        if f" {phrase} " in joined:
            implied.update(groups)
    if (set(words) & _PLANT_WORDS) or any(w.startswith(("kaura", "soija", "havre")) for w in words):
        literal.discard("milk")
        implied.discard("milk")
    if has_milk_free:
        implied.discard("milk")
    if has_gluten_free:
        implied.discard("gluten")
    return [g for g in excl if g in literal], [g for g in excl if g in implied and g not in literal]


@lru_cache(maxsize=4096)
def _screen_supplier_text(text: str) -> tuple[str, ...]:
    try:
        from aimo.trust.screening import screen_text

        s = screen_text(text, use_gemini=False)
    except Exception:
        from aimo.catalog.screening_bridge import _fallback_screen

        s = _fallback_screen(text)
    if not s.get("suspicious"):
        return ()
    return tuple(s.get("strong_reasons") or s.get("reasons") or ["suspicious"])


def mark_supplier_text(
    cands: list[Candidate],
    rows: dict[str, dict[str, Any]],
    texts: Optional[dict[str, str]] = None,
) -> None:
    for c in cands:
        row = rows.get(c.product.id) or {}
        if texts and c.product.id in texts:
            text = texts[c.product.id] or ""
        else:
            parts = [str(row.get("marketing_text") or "").strip(), str(row.get("ingredients_text") or "").strip()]
            text = "\n".join(x for x in parts if x)
        hits = _screen_supplier_text(text) if text.strip() else ()
        if hits and "supplier_text_injection_suspected" not in c.flags:
            c.flags = sorted(set([*c.flags, "supplier_text_injection_suspected"]))
            c.reasons.append(
                f"{SUPPLIER_TEXT_REASON}, flagged by prompt-injection screen ({', '.join(hits[:3])}); "
                f"ignored by the agent, and order requires human approval."
            )
            if not (c.injection_screen or {}).get("suspicious"):
                c.injection_screen = {"suspicious": True, "reasons": list(hits), "layers": ["regex"]}


def plan_need(need: Need, offers_fn: Optional[Callable[[list[str], Any], dict[str, Offer]]] = None,
              limit: int = 12) -> Plan:
    validate_need(need)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if offers_fn is None:
        from aimo.catalog.offers import get_offers
        offers_fn = get_offers
    original = need
    original_preferred = list(original.preferred_product_ids)
    policy_need, policy_added, pb = with_customer_policy(need)
    need, qsrc = with_effective_query(policy_need)
    usual = usual_products_for(need)
    if usual:
        need = need.model_copy(
            update={
                "preferred_product_ids": usual,
                "preferences": {**(need.preferences or {}), "usual_from_profile": usual},
            }
        )
    plan, full, late_notes = _plan_core(need, offers_fn, limit)
    notes: list[str] = []
    if policy_added:
        pid = pb.policy_id or need.customer_id
        notes.append(
            f"Customer policy ({pid}) excludes {', '.join(policy_added)}"
            + (f" ({pb.reason})" if pb.reason else "")
            + f". Policy {pid} excludes {', '.join(policy_added)} as a hard block."
        )
    if usual:
        names = ", ".join(f"{l.product_name} ({l.product_id})" for l in plan.lines if l.product_id in usual)
        notes.append(
            f"Started from the customer's usual product{'s' if len(usual) > 1 else ''} for this request"
            + (f": {names}." if names else f" ({', '.join(usual)}), which cannot be used here.")
        )
    literal, implied = _excluded_groups_named(need.product_query, need.excluded_allergens)
    if literal:
        alt_plan, alt_note = _allergen_safe_alternative(
            need, plan, offers_fn, limit, literal, original_preferred
        )
        if alt_plan is not None:
            plan, full, late_notes = alt_plan, list(alt_plan.candidates), []
        else:
            late_notes = []
            if plan.lines:
                sf = round(float(need.quantity), 4)
                plan = plan.model_copy(
                    update={
                        "lines": [],
                        "covered_amount": 0.0,
                        "total_net": 0.0,
                        "shortfall": sf,
                        "explanation": explain(need, [], plan.candidates, 0.0, sf, 0.0),
                    }
                )
        notes.append(alt_note)
    named = literal + implied
    if named:
        for l in plan.lines:
            if "allergen_safe_alternative" not in l.flags:
                l.flags = sorted(set(l.flags) | {"allergen_safe_alternative"})
                l.reasons.insert(
                    0,
                    f"The request names '{need.product_query}', which normally contains "
                    f"{' and '.join(named)} (excluded by policy). This product is {' and '.join(named)}-free "
                    f"per supplier data; a human confirms it is a suitable replacement.",
                )
        if implied and not literal:
            notes.append(
                f"'{need.product_query}' normally contains {' and '.join(implied)}, which this customer's "
                f"policy excludes: only products confirmed {' and '.join(implied)}-free are planned, and each "
                f"needs a human's confirmation."
            )
    if need.product_query != original.product_query:
        notes.append(f"Searched the catalog for '{need.product_query}'"
                     + (" (taken from the request text because the parsed product phrase found nothing)"
                        if qsrc == "raw_text" else " (allergen mentions removed from the product phrase; they are"
                        " enforced as exclusions, not searched for)") + ".")
    advice = shortfall_advice(need, plan, full) if plan.shortfall > 1e-9 else []
    cover = coverage_sentence(need, plan)
    out_need = (original.model_copy(update={"excluded_allergens": list(policy_need.excluded_allergens)})
                if list(policy_need.excluded_allergens) != list(original.excluded_allergens) else original)
    plan = plan.model_copy(update={"need": out_need, "explanation": " ".join(
        [*notes, cover, plan.explanation, *late_notes, *advice]).strip()})
    if os.environ.get("AIMO_RECIPE_CHECK", "1") != "0":
        _recipe_checks(need, plan)
    return plan


def _allergen_safe_alternative(
    need: Need,
    plan: Plan,
    offers_fn: Any,
    limit: int,
    named: list[str],
    original_preferred: Iterable[str] = (),
) -> tuple[Optional[Plan], str]:
    usual = _profile_usual_products(need.customer_id)
    rows = _load_rows(list(usual)) if usual else {}
    eff_excl = effective_excluded_allergens(need.customer_id, need.excluded_allergens)
    safe = [
        pid
        for pid in usual
        if pid in rows
        and rows[pid].get("content_unit") == need.unit
        and set(need.required_claims) <= set(rows[pid].get("claims") or [])
        and not blocking(
            rows[pid].get("allergens") or {},
            eff_excl,
            allergen_source=rows[pid].get("allergen_source"),
        )
    ]
    head = (
        f"'{need.product_query}' is itself {' and '.join(named)}, which this customer's policy excludes, "
        f"so no {' or '.join(named)} product is planned"
    )
    if not safe:
        return None, head + "; the shortfall is left for a human (no allergen-safe usual product on file)."
    alt_query = rows[safe[0]].get("name_en") or rows[safe[0]].get("name") or need.product_query
    alt_prefs = {k: v for k, v in (need.preferences or {}).items() if k != "usual_from_profile"}
    alt_need = need.model_copy(
        update={
            "preferred_product_ids": safe,
            "product_query": alt_query,
            "preferences": alt_prefs,
        }
    )
    alt, _full, _late = _plan_core(alt_need, offers_fn, limit)
    alt_lines = [l for l in alt.lines if l.product_id in safe]
    if not alt_lines:
        return (
            None,
            head + f"; the customer's allergen-safe usual product ({', '.join(safe)}) is not orderable for this date.",
        )
    for l in alt_lines:
        l.flags = sorted(set(l.flags) | {"allergen_safe_alternative"})
        l.reasons.insert(
            0,
            f"Allergen-safe alternative: the request asked for '{need.product_query}', which contains "
            f"{' and '.join(named)} (excluded by policy). This is the customer's usual "
            f"{' and '.join(named)}-free product; a human confirms the swap.",
        )
    orig_pref_set = set(original_preferred or ())
    merged_cands = list(alt.candidates)
    seen_ids = {c.product.id for c in merged_cands}
    for c in plan.candidates:
        if c.tier == "rejected" and c.product.id in orig_pref_set and c.product.id not in seen_ids:
            merged_cands.append(c)
            seen_ids.add(c.product.id)
    extra_rej = 0
    for c in plan.candidates:
        if c.tier == "rejected" and c.product.id not in seen_ids:
            merged_cands.append(c)
            seen_ids.add(c.product.id)
            extra_rej += 1
            if extra_rej >= 6:
                break
    covered = round(sum(l.covered_amount for l in alt_lines), 4)
    total_net = round(sum(l.line_total for l in alt_lines), 2)
    shortfall = round(max(0.0, need.quantity - covered), 4)
    alt = alt.model_copy(
        update={
            "need": need,
            "lines": alt_lines,
            "covered_amount": covered,
            "total_net": total_net,
            "shortfall": shortfall,
            "candidates": merged_cands,
            "explanation": explain(need, alt_lines, merged_cands, covered, shortfall, total_net),
        }
    )
    names = ", ".join(f"{l.product_name} ({l.product_id})" for l in alt_lines)
    return alt, head + f"; planned the customer's usual allergen-safe product instead: {names} (needs a human's confirmation)."


def _plan_core(need: Need, offers_fn: Any, limit: int) -> tuple[Plan, list[Candidate], list[str]]:
    rows, rel = find_candidates(need)
    eff_excl = list(need.excluded_allergens)
    order = sorted(rows.values(), key=lambda d: _static_priority(need, d, rel.get(d["id"], 0), excl=eff_excl))
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
        except Exception:
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


def _candidate_injection_reasons(c: Candidate, existing: list[str]) -> list[str]:
    found = [
        r for r in c.reasons
        if ("prompt-injection screen" in r or r.startswith(SUPPLIER_TEXT_REASON)) and r not in existing
    ]
    if found:
        return found
    if (c.injection_screen or {}).get("suspicious") or "supplier_text_injection_suspected" in c.flags:
        sr = screen_reason(c.injection_screen or {"suspicious": True, "reasons": []})
        if sr and sr not in existing:
            return [sr]
    return []


def _late_split(need: Need, plan: Plan, full: list[Candidate], shortlist: dict, rel: dict,
                hints: dict) -> tuple[Plan, list[str]]:
    """Cover a shortfall with products that are only blocked by date (cut-off / late availability).
    Lines after delivery_by always carry `late_option` + `delivery_date:<iso>`; they are added to the
    plan only when need.preferences['allow_split_delivery'] is true, otherwise reported as the
    earliest date that covers the rest."""
    validate_need(need)

    def _pos_num(v: Any) -> Optional[float]:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        try:
            fv = float(v)
        except OverflowError:
            return None
        return fv if (math.isfinite(fv) and fv > 0.0) else None

    def _score_key(v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return -1e9
        try:
            fv = float(v)
        except OverflowError:
            return -1e9
        return fv if math.isfinite(fv) else -1e9

    def _valid_avail(v: Any) -> tuple[bool, Optional[float]]:
        if v is None:
            return True, None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False, None
        try:
            af = float(v)
        except OverflowError:
            return (True, math.inf) if v > 0 else (False, None)
        if math.isnan(af) or af <= 0.0:
            return False, None
        return True, af

    shortfall_val = _pos_num(plan.shortfall)
    if shortfall_val is None or shortfall_val <= 1e-9:
        if isinstance(plan.shortfall, bool) or not isinstance(plan.shortfall, (int, float)):
            raise ValueError("plan.shortfall must be a finite number")
        try:
            sf_check = float(plan.shortfall)
        except OverflowError as e:
            raise ValueError("plan.shortfall must be a finite number") from e
        if not math.isfinite(sf_check) or sf_check < 0.0:
            raise ValueError("plan.shortfall must be a finite non-negative number")
        return plan, []
    from aimo.catalog.delivery import earliest_feasible, fmt, late_offer
    allow = bool((need.preferences or {}).get("allow_split_delivery"))
    late: dict[str, tuple[Candidate, date, bool]] = {}
    for c in full:
        if not _only_orderability(c) or c.offer is None:
            continue
        if _pos_num(c.offer.unit_price) is None:
            continue
        ok_av, _ = _valid_avail(c.offer.available_amount)
        if not ok_av or "availability_error" in (c.offer.issues or []) or "price_error" in (c.offer.issues or []):
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
    ranked = [c for c in ranked if c.tier != "rejected" and c.tier in TIER_ORDER and c.product.id in late]
    ranked.sort(key=lambda c: (late[c.product.id][1], TIER_ORDER[c.tier], -_score_key(c.rank_score)))
    remaining = shortfall_val
    loaded_lots = _load_rows([c.product.id for c in ranked])
    lots = {
        pid: (loaded_lots[pid].get("allowed_lot_size") if pid in loaded_lots else (shortlist.get(pid) or {}).get("allowed_lot_size"))
        for pid in (c.product.id for c in ranked)
    }
    new_lines: list[PlanLine] = []
    for c in ranked:
        if remaining <= 1e-9:
            break
        content = _pos_num(c.product.content_amount)
        unit_price = _pos_num(c.offer.unit_price) if c.offer is not None else None
        if content is None or unit_price is None or c.product.content_unit != need.unit:
            continue
        raw_units = remaining / content
        if not math.isfinite(raw_units) or raw_units <= 0.0:
            continue
        units = _round_lot(raw_units, lots.get(c.product.id), up=True)
        if not math.isfinite(units) or units <= 0.0:
            continue
        ok_avail, af = _valid_avail(c.offer.available_amount)
        if not ok_avail:
            continue
        if af is not None and math.isfinite(af) and af < units:
            units = _round_lot(af, lots.get(c.product.id), up=False)
        if not math.isfinite(units) or units <= 0.0:
            continue
        d, est = late[c.product.id][1], late[c.product.id][2]
        raw_cov = units * content
        raw_tot = units * unit_price
        if not (math.isfinite(raw_cov) and math.isfinite(raw_tot)):
            continue
        covered = round(raw_cov, 4)
        total = round(raw_tot, 2)
        flags = sorted(set([f for f in c.flags if f not in ("cutoff_passed",)] +
                           ["late_option", f"delivery_date:{d.isoformat()}"] + (["split_delivery"] if allow else [])))
        reasons = [c.reasons[0] if c.reasons else "",
                   f"Delivered {fmt(d)}, after the requested {fmt(need.delivery_by)}"
                   + (" (estimated date)" if est else "") + ("; split delivery allowed by the customer"
                                                             if allow else "; needs a human decision"),
                   f"{units:g} x {c.product.sales_unit} x {content:g} {need.unit} = {covered:g} {need.unit}; "
                   f"{units:g} x €{unit_price:.2f} = €{total:,.2f}"]
        reasons.extend(_candidate_injection_reasons(c, reasons))
        new_lines.append(PlanLine(product_id=c.product.id, product_name=c.product.name, tier=c.tier,
                                  qty_sales_units=units, sales_unit=c.product.sales_unit, covered_amount=covered,
                                  unit_price=unit_price, line_total=total, reasons=reasons, flags=flags,
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
    shortfall = round(max(0.0, float(need.quantity) - covered_total), 4)
    note = (f"Split delivery: {late_cov:g} {need.unit} arrive {fmt(d_all)} (late_option lines, flagged for review).")
    _ = first
    explanation = explain(need, lines, plan.candidates, covered_total, shortfall, total_net)
    return plan.model_copy(update={"lines": lines, "covered_amount": covered_total, "total_net": total_net,
                                   "shortfall": shortfall, "explanation": explanation}), [note]


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
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
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
    validate_need(need)

    def _pos_num(v: Any) -> Optional[float]:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        try:
            fv = float(v)
        except OverflowError:
            return None
        return fv if (math.isfinite(fv) and fv > 0.0) else None

    def _score_key(v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return -1e9
        try:
            fv = float(v)
        except OverflowError:
            return -1e9
        return fv if math.isfinite(fv) else -1e9

    def _valid_avail(v: Any) -> tuple[bool, Optional[float]]:
        if v is None:
            return True, None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False, None
        try:
            af = float(v)
        except OverflowError:
            return (True, math.inf) if v > 0 else (False, None)
        if math.isnan(af) or af <= 0.0:
            return False, None
        return True, af

    remaining = float(need.quantity)
    lines: list[PlanLine] = []
    excl = effective_excluded_allergens(need.customer_id, need.excluded_allergens)
    rows_by_id = _load_rows([c.product.id for c in cands])
    lot = {pid: d.get("allowed_lot_size") for pid, d in rows_by_id.items()}
    for c in sorted(cands, key=lambda c: (TIER_ORDER.get(c.tier, 9), -_score_key(c.rank_score))):
        if remaining <= 1e-9:
            break
        if c.tier == "rejected" or c.tier not in TIER_ORDER or c.offer is None:
            continue
        unit_price = _pos_num(c.offer.unit_price)
        if unit_price is None:
            continue
        if excl and ("allergen_unknown" in c.flags
                     or any(f.startswith(("contains:", "may_contain:"))
                            and set(normalize_allergens([f.split(":", 1)[1]])) & set(excl)
                            for f in c.flags)
                     or (c.product.id in rows_by_id and blocking(rows_by_id[c.product.id].get("allergens") or {}, excl,
                                                                 allergen_source=rows_by_id[c.product.id].get("allergen_source")))):
            continue
        content = _pos_num(c.product.content_amount)
        if content is None or c.product.content_unit != need.unit:
            continue
        raw_units = remaining / content
        if not math.isfinite(raw_units) or raw_units <= 0.0:
            continue
        units = _round_lot(raw_units, lot.get(c.product.id), up=True)
        if not math.isfinite(units) or units <= 0.0:
            continue
        flags = list(c.flags)
        ok_avail, af = _valid_avail(c.offer.available_amount)
        if not ok_avail:
            continue
        if af is not None and math.isfinite(af) and af < units:
            units = _round_lot(af, lot.get(c.product.id), up=False)
            if "partial_stock" not in flags:
                flags.append("partial_stock")
        if not math.isfinite(units) or units <= 0.0:
            continue
        raw_cov = units * content
        raw_tot = units * unit_price
        if not (math.isfinite(raw_cov) and math.isfinite(raw_tot)):
            continue
        covered = round(raw_cov, 4)
        total = round(raw_tot, 2)
        reasons = [c.reasons[0]] if c.reasons else []
        reasons.append(f"{units:g} x {c.product.sales_unit} x {content:g} {need.unit} = {covered:g} {need.unit}; "
                       f"{units:g} x €{unit_price:.2f} = €{total:,.2f}")
        reasons.extend(_candidate_injection_reasons(c, reasons))
        lines.append(PlanLine(product_id=c.product.id, product_name=c.product.name, tier=c.tier,
                              qty_sales_units=units, sales_unit=c.product.sales_unit, covered_amount=covered,
                              unit_price=unit_price, line_total=total, reasons=reasons,
                              flags=sorted(set(flags)), injection_screen=c.injection_screen))
        remaining -= covered
    covered_total = round(sum(l.covered_amount for l in lines), 4)
    total_net = round(sum(l.line_total for l in lines), 2)
    shortfall = round(max(0.0, float(need.quantity) - covered_total), 4)
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
    flagged = list(dict.fromkeys(
        f"{l.product_name} ({l.product_id})"
        for l in lines
        if (l.injection_screen or {}).get("suspicious") or "supplier_text_injection_suspected" in l.flags
    ))
    if flagged:
        parts.append(
            f"Prompt-injection screen: untrusted supplier text on {', '.join(flagged)} was flagged "
            f"and will require human approval."
        )
    return " ".join(parts)
