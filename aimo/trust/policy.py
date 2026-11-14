"""Policy engine: versioned YAML buyer mandates evaluated on every quote.

evaluate(quote, mandate) -> PolicyDecision. Each rule yields RuleHits with a plain-language message
for a procurement manager. Final outcome = strictest hit: reject > needs_approval > auto_approve.
Line-level hits use rule_id "<rule id>:<product_id>" so a UI or agent can drop just that line.

Product facts (allergens, claims, category) come from aimo.catalog.get_product(), imported lazily;
when it is missing the engine fails closed (unknown is never safe).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from aimo.models import AllergenStatus, Mandate, PolicyDecision, Product, Quote, RuleHit

POLICY_DIR = Path(__file__).parent / "policies"
SEVERITY = {"allow": 0, "needs_approval": 1, "reject": 2}
RULE_TYPES = {"scope", "quote_freshness", "spend_threshold", "period_spend", "category_scope", "substitution",
              "allergen_block", "delivery", "quantity_drift", "injection", "split_order", "price_jump"}
TIER_TEXT = {
    "preferred": "the customer's usual product",
    "best": "same brand and spec in a different pack (tier best)",
    "better": "same spec from a different brand (tier better)",
    "good": "meets only the hard requirements, e.g. a different fat % or pack (tier good)",
    "rejected": "ranked not acceptable by the substitution engine",
}
CLAIM_TEXT = {"FREE_FROM_LACTOSE": "lactose free", "FREE_FROM_GLUTEN": "gluten free", "LOW_LACTOSE": "low lactose",
              "VEGAN": "vegan", "VEGETARIAN": "vegetarian", "ORGANIC": "organic", "FREE_FROM_MILK": "milk free"}


# ------------------------------------------------------------------ formatting helpers
def eur(x: float) -> str:
    return f"€{x:,.2f}"


def role_text(role: Optional[str]) -> str:
    return (role or "approver").replace("_", " ")


def day_text(d: date) -> str:
    return d.strftime("%a %-d %b")


def qty_text(x: float) -> str:
    return f"{x:,.0f}" if abs(x - round(x)) < 1e-9 else f"{x:,.1f}"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_time(dt: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return _aware(dt).astimezone(ZoneInfo("Europe/Helsinki")).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return _aware(dt).strftime("%H:%M UTC")


def full_agent_id(customer_id: str, agent_id: str) -> str:
    return agent_id if "/" in agent_id else f"{customer_id}/{agent_id}"


# ------------------------------------------------------------------ loading
@lru_cache(maxsize=64)
def _load_policy_cached(policy_id: str, mtime: float) -> dict:
    path = POLICY_DIR / f"{policy_id}.yaml"
    pol = yaml.safe_load(path.read_text(encoding="utf-8"))
    _validate(pol, policy_id)
    return pol


def _validate(pol: dict, policy_id: str) -> None:
    for k in ("policy_id", "version", "customer", "rules"):
        if k not in pol:
            raise ValueError(f"policy {policy_id}: missing {k}")
    if pol["policy_id"] != policy_id:
        raise ValueError(f"policy file {policy_id}.yaml declares policy_id {pol['policy_id']}")
    seen = set()
    for r in pol["rules"]:
        if r.get("type") not in RULE_TYPES:
            raise ValueError(f"policy {policy_id}: unknown rule type {r.get('type')!r}")
        if not r.get("id") or r["id"] in seen:
            raise ValueError(f"policy {policy_id}: rule ids must be unique and non-empty")
        seen.add(r["id"])
        for k in ("on_contains", "on_may_contain", "on_unknown", "on_violation", "outcome", "outside_preference"):
            if k in r and r[k] not in SEVERITY:
                raise ValueError(f"policy {policy_id}: rule {r['id']} {k}={r[k]!r} not in {list(SEVERITY)}")


def load_policy(policy_id: str) -> dict:
    """Return the parsed, validated YAML policy (cached; reloaded when the file changes)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", policy_id or ""):
        raise KeyError(f"invalid policy id {policy_id!r}")
    path = POLICY_DIR / f"{policy_id}.yaml"
    if not path.exists():
        raise KeyError(f"unknown policy {policy_id!r}")
    return _load_policy_cached(policy_id, path.stat().st_mtime)


def policy_ids() -> list[str]:
    return sorted(p.stem for p in POLICY_DIR.glob("*.yaml"))


def planning_profile(customer_id: Optional[str]) -> dict:
    """What the planner must know from the customer's mandate before it searches: the policy's excluded
    allergens (hard block, even when the request does not repeat them), why, and the usual products.
    {} for an unknown customer (the planner then only uses what the request says)."""
    try:
        pol = load_policy(customer_id or "")
    except (KeyError, ValueError, OSError):
        return {}
    excl: list[str] = []
    why: list[str] = []
    for r in pol.get("rules") or []:
        if r.get("type") == "allergen_block":
            excl += [str(a) for a in r.get("excluded") or []]
            if r.get("reason") and r["reason"] not in why:
                why.append(str(r["reason"]))
    return {"policy_id": pol["policy_id"], "excluded_allergens": list(dict.fromkeys(excl)),
            "allergen_reason": "; ".join(why) or None,
            "usual_products": sorted(_preferred_ids(pol)),
            "approved_alternatives": _approved_alternatives(pol),
            "customer_name": (pol.get("customer") or {}).get("name") or customer_id}


def get_mandate(customer_id: str, agent_id: str) -> Mandate:
    """Mandate for (customer, agent). Agent may be given as "buyer-1" or "hotel-aurora/buyer-1".
    Unknown customer -> KeyError. Unknown agent of a known customer -> Mandate with no scopes, so every
    order is rejected with a clear message (a rogue or unregistered agent)."""
    pol = load_policy(customer_id)
    fid = full_agent_id(customer_id, agent_id)
    for a in pol.get("agents", []):
        if a["agent_id"] == fid:
            exp = a.get("expires_at")
            return Mandate(customer_id=customer_id, agent_id=fid, policy_id=pol["policy_id"],
                           scopes=list(a.get("scopes", [])),
                           expires_at=datetime.fromisoformat(str(exp)) if exp else None)
    return Mandate(customer_id=customer_id, agent_id=fid, policy_id=pol["policy_id"], scopes=[])


# ------------------------------------------------------------------ product lookup
def _catalog_lookup(ids: list[str]) -> dict[str, Optional[Product]]:
    out: dict[str, Optional[Product]] = {i: None for i in ids}
    try:
        from aimo.catalog import get_product  # built in parallel; tolerate absence
    except Exception:  # noqa: BLE001
        return out
    for i in ids:
        try:
            p = get_product(i)
            out[i] = Product.model_validate(p) if isinstance(p, dict) else p
        except Exception:  # noqa: BLE001  catalog not ready / no data: fail closed
            out[i] = None
    return out


@dataclass
class Ctx:
    quote: Quote
    mandate: Mandate
    policy: dict
    products: dict[str, Optional[Product]]
    now: datetime
    audit_log: Any = None
    hits: list[tuple[RuleHit, Optional[str]]] = field(default_factory=list)
    cache: dict = field(default_factory=dict)

    def hit(self, rule_id: str, outcome: str, message: str, role: Optional[str] = None) -> None:
        self.hits.append((RuleHit(rule_id=rule_id, outcome=outcome, message=message),
                          role if outcome == "needs_approval" else None))

    def product(self, pid: str) -> Optional[Product]:
        return self.products.get(pid)

    def role(self, rule: dict) -> str:
        return rule.get("approver_role") or (self.policy.get("approver_roles") or ["approver"])[0]


def _line_name(line) -> str:
    return f"'{line.product_name}' ({line.product_id})"


def _st(v: Any) -> str:
    return v.value if isinstance(v, AllergenStatus) else str(v or "unknown")


# ------------------------------------------------------------------ rules
def _integrity(c: Ctx) -> None:
    """Always-on: the quote must be internally consistent and, if signed, the signature must verify."""
    q = c.quote
    problems = []
    if not q.lines:
        problems.append("the quote has no lines")
    for ln in q.lines:
        if ln.qty_sales_units <= 0:
            problems.append(f"{_line_name(ln)} has a non-positive quantity")
        if ln.unit_price < 0:
            problems.append(f"{_line_name(ln)} has a negative price")
        if abs(ln.unit_price * ln.qty_sales_units - ln.line_total) > 0.01 + 1e-6 * abs(ln.line_total):
            problems.append(f"{_line_name(ln)} line total {eur(ln.line_total)} is not price × quantity")
    s = sum(ln.line_total for ln in q.lines)
    if abs(s - q.total_net) > 0.01 + 1e-6 * abs(s):
        problems.append(f"lines add up to {eur(s)} but the quote total says {eur(q.total_net)}")
    if q.signature:
        from aimo.trust.signing import verify_quote_detailed
        v = verify_quote_detailed(q)
        if not v["ok"]:
            problems.append("the quote signature does not verify (" + "; ".join(r for r in v["reasons"] if "expired" not in r) + ")")
    if problems:
        c.hit("quote_integrity", "reject", "The quote cannot be trusted: " + "; ".join(problems) + ".")
    else:
        signed = "signed by Valio Aimo and unmodified" if q.signature else "not yet signed"
        c.hit("quote_integrity", "allow", f"Quote arithmetic checks out ({len(q.lines)} line(s), {eur(q.total_net)}); {signed}.")


def r_scope(c: Ctx, r: dict) -> None:
    m, q = c.mandate, c.quote
    name = c.policy["customer"].get("name", q.customer_id)
    if m.customer_id != q.customer_id:
        return c.hit(r["id"], "reject", f"Agent {m.agent_id} acts for {m.customer_id}, but this quote is for {q.customer_id}.")
    if full_agent_id(q.customer_id, q.agent_id) != full_agent_id(m.customer_id, m.agent_id):
        return c.hit(r["id"], "reject", f"The quote was requested by agent {q.agent_id}, not by {m.agent_id} who holds this mandate.")
    if m.expires_at and _aware(m.expires_at) < c.now:
        return c.hit(r["id"], "reject", f"Agent {m.agent_id}'s mandate from {name} expired on {_aware(m.expires_at):%d %b %Y}.")
    if not m.scopes:
        return c.hit(r["id"], "reject", f"Agent {m.agent_id} is not registered with {name}, so it has no authority to order.")
    missing = [s for s in r.get("required_scopes", ["order:submit"]) if s not in m.scopes]
    if missing:
        return c.hit(r["id"], "reject", f"Agent {m.agent_id} may prepare quotes for {name} but is not allowed to submit orders "
                                        f"(missing permission {', '.join(missing)}).")
    c.hit(r["id"], "allow", f"Agent {m.agent_id} holds a valid mandate from {name} to submit orders.")


def r_quote_freshness(c: Ctx, r: dict) -> None:
    exp = _aware(c.quote.expires_at)
    if exp < c.now:
        return c.hit(r["id"], "reject", f"The price lock on this quote expired at {_local_time(exp)}. Prices may have changed; ask for a fresh quote.")
    max_age = r.get("max_age_minutes")
    if max_age and c.now - _aware(c.quote.created_at) > timedelta(minutes=max_age):
        return c.hit(r["id"], "reject", f"The quote is older than {max_age} minutes; ask for a fresh quote.")
    c.hit(r["id"], "allow", f"Prices are locked until {_local_time(exp)} (Helsinki time).")


def r_spend_threshold(c: Ctx, r: dict) -> None:
    t, auto, cap = c.quote.total_net, float(r["auto_max"]), float(r["approval_max"])
    role = c.role(r)
    if t <= auto:
        c.hit(r["id"], "allow", f"Order total {eur(t)} is within the {eur(auto)} auto-approve limit.")
    elif t <= cap:
        c.hit(r["id"], "needs_approval", f"Order total {eur(t)} is above the {eur(auto)} auto-approve limit, "
                                         f"so a {role_text(role)} must approve it.", role)
    else:
        c.hit(r["id"], "reject", f"Order total {eur(t)} exceeds the {eur(cap)} maximum an agent may order at once. "
                                 f"Split the order or buy outside the agent.")


def _period_total(c: Ctx, days: int) -> float:
    log = c.audit_log
    if log is None:
        from aimo.trust.audit import audit as log
    since = c.now - timedelta(days=days)
    total = 0.0
    try:
        for e in log.events_by_action("order.placed"):
            d = e.data or {}
            cust = d.get("customer_id") or (d.get("quote") or {}).get("customer_id")
            amt = d.get("total_net", (d.get("quote") or {}).get("total_net"))
            if cust == c.quote.customer_id and amt is not None and _aware(e.ts) >= since \
                    and d.get("quote_id") != c.quote.quote_id:
                total += float(amt)
    except Exception:  # noqa: BLE001
        return 0.0
    return total


def _history(c: Ctx, days: int) -> list[dict]:
    """Orders this customer placed in the last `days` days (newest last), from the audit log:
    [{"ts", "total", "auto" (placed without a human), "lines": [{product_id, product_name, qty, unit_price,
    line_total}] or None when the log does not say, "query": the request's product phrase or None}].
    Lines come from the order's quote.created event (joined by quote_id)."""
    key = ("history", days)
    if key in c.cache:
        return c.cache[key]
    log = c.audit_log
    if log is None:
        from aimo.trust.audit import audit as log
    since = c.now - timedelta(days=days)
    out: list[dict] = []
    try:
        quotes: dict[str, dict] = {}
        for e in log.events_by_action("quote.created"):
            qd = (e.data or {}).get("quote") or {}
            if qd.get("quote_id") and qd.get("customer_id") == c.quote.customer_id:
                quotes[qd["quote_id"]] = qd
        for e in log.events_by_action("order.placed"):
            d = e.data or {}
            cust = d.get("customer_id") or (d.get("quote") or {}).get("customer_id")
            if cust != c.quote.customer_id or _aware(e.ts) < since or d.get("quote_id") == c.quote.quote_id:
                continue
            qd = d.get("quote") or quotes.get(d.get("quote_id") or "") or {}
            raw = d.get("lines") or qd.get("lines")
            lines = None if raw is None else [
                {"product_id": str(x.get("product_id")), "product_name": x.get("product_name") or "",
                 "qty": float(x.get("qty_sales_units") or x.get("qty") or 0), "unit_price": x.get("unit_price"),
                 "line_total": float(x.get("line_total") or 0)} for x in raw]
            order = d.get("order") or {}
            auto = not d.get("approver") and not order.get("approved_by")
            out.append({"ts": _aware(e.ts), "total": float(d.get("total_net", qd.get("total_net")) or 0), "auto": auto,
                        "lines": lines, "query": ((qd.get("need") or {}).get("product_query") or None)})
    except Exception:  # noqa: BLE001  no log: no history (other rules still apply)
        out = []
    out.sort(key=lambda h: h["ts"])
    c.cache[key] = out
    return out


def _kind_keys(c: Ctx, product_id: str, name: str, query: Optional[str]) -> set[str]:
    """What makes two purchases 'the same thing' for split-order aggregation: the product, its standing-order
    bucket (keywords), its catalog product type + category, and the request's product phrase."""
    keys = {f"id:{product_id}"}
    if query:
        keys.add("q:" + re.sub(r"\s+", " ", query.lower()).strip())
    for b in c.policy.get("standing_orders", []):
        if _matches(name, b.get("keywords", [])):
            keys.add(f"so:{b['id']}")
    p = c.product(product_id)
    if p is not None and p.category:
        keys.add(f"cat:{p.category}")
    return keys


def r_split_order(c: Ctx, r: dict) -> None:
    """Split-order evasion: several orders of the same thing, each under the auto-approve limit, that add up
    to well over it within a rolling window (default 7 days, more than 2x the limit). Prior orders count only
    if they were placed without a human. When the log has no line detail for a prior order, it counts as the
    same thing if it was itself close to the limit (>= near_limit_share of it), the usual split signature."""
    days = int(r.get("window_days", 7))
    spend = next((x for x in c.policy["rules"] if x.get("type") == "spend_threshold"), None)
    if spend is None:
        return c.hit(r["id"], "allow", "No per-order spend limit, so no split-order check.")
    auto_max = float(spend["auto_max"])
    limit = float(r.get("max_factor", 2)) * auto_max
    near = float(r.get("near_limit_share", 0.6)) * auto_max
    q = c.quote
    if q.total_net > auto_max:
        return c.hit(r["id"], "allow", "This order is above the auto-approve limit on its own; the spend rule decides.")
    qquery = q.need.product_query if q.need else None
    mine = set().union(*[_kind_keys(c, ln.product_id, ln.product_name, qquery) for ln in q.lines]) if q.lines else set()
    prior, n = 0.0, 0
    for h in _history(c, days):
        if not h["auto"]:
            continue
        if h["lines"] is None:
            same = h["total"] >= near
            amt = h["total"] if same else 0.0
        else:
            amt = sum(x["line_total"] for x in h["lines"]
                      if _kind_keys(c, x["product_id"], x["product_name"], h["query"]) & mine)
            same = amt > 0
        if same:
            prior += amt
            n += 1
    total = prior + q.total_net
    role = r.get("approver_role") or spend.get("approver_role") or c.role(r)
    if n and total > limit:
        c.hit(r["id"], r.get("outcome", "needs_approval"),
              f"This is order {n + 1} of the same kind in {days} days placed without a human: together "
              f"{eur(total)}, more than {float(r.get('max_factor', 2)):g}x the {eur(auto_max)} auto-approve limit. It looks "
              f"like one large order split into small ones, so a {role_text(role)} must approve it.", role)
    else:
        c.hit(r["id"], "allow", f"No split-order pattern: {eur(total)} of the same kind in {days} days"
                                + (f" over {n + 1} orders." if n else "."))


def r_price_jump(c: Ctx, r: dict) -> None:
    """A line priced more than max_increase_pct (default 15%) above the last price this customer paid for
    the same product needs a human. Last paid price: the newest placed order in the audit log (lookback
    default 90 days). The buyer agent may also report a reference price (need.preferences.reference_unit_price,
    {product_id: price}); the lower of the two is used, so a reported price can only make the check stricter."""
    pct = float(r.get("max_increase_pct", 15))
    role = c.role(r)
    last: dict[str, tuple[float, str]] = {}
    for h in _history(c, int(r.get("lookback_days", 90))):
        for x in h["lines"] or []:
            if x.get("unit_price") is not None:
                last[x["product_id"]] = (float(x["unit_price"]), f"paid on {h['ts']:%d %b}")
    reported = ((c.quote.need.preferences or {}).get("reference_unit_price") if c.quote.need else None) or {}
    bad = False
    for ln in c.quote.lines:
        refs = [v for v in [last.get(ln.product_id)] if v]
        try:
            if ln.product_id in reported and float(reported[ln.product_id]) > 0:
                refs.append((float(reported[ln.product_id]), "reported by the buyer agent as the usual price"))
        except (TypeError, ValueError):
            pass
        if not refs:
            continue
        ref, src = min(refs, key=lambda t: t[0])
        if ln.unit_price > ref * (1 + pct / 100) + 1e-9:
            bad = True
            up = (ln.unit_price / ref - 1) * 100
            c.hit(f"{r['id']}:{ln.product_id}", r.get("outcome", "needs_approval"),
                  f"{_line_name(ln)} costs {eur(ln.unit_price)}, {up:.0f}% above the last price {eur(ref)} ({src}); "
                  f"more than the {pct:g}% this customer accepts without asking. A {role_text(role)} must confirm it.", role)
    if not bad:
        c.hit(r["id"], "allow", f"No line is more than {pct:g}% above the last price paid.")


def r_period_spend(c: Ctx, r: dict) -> None:
    days, limit = int(r.get("period_days", 7)), float(r["limit"])
    prior = _period_total(c, days)
    new = prior + c.quote.total_net
    label = "weekly" if days == 7 else f"{days}-day"
    if new > limit:
        out = r.get("outcome", "needs_approval")
        msg = f"This order would bring spending in the last {days} days to {eur(new)}, above the {eur(limit)} {label} budget"
        msg += f", so a {role_text(c.role(r))} must approve it." if out == "needs_approval" else "."
        c.hit(r["id"], out, msg, c.role(r))
    else:
        c.hit(r["id"], "allow", f"Spending in the last {days} days would be {eur(new)} of the {eur(limit)} {label} budget.")


def _cat_ok(cat: str, allowed: list) -> bool:
    for a in allowed:
        if isinstance(a, dict):
            if a.get("prefix") and cat.startswith(str(a["prefix"])):
                return True
            if a.get("label") and a["label"].lower() == cat.lower():
                return True
        elif cat.lower() == str(a).lower() or cat.startswith(str(a)):
            return True
    return False


def r_category_scope(c: Ctx, r: dict) -> None:
    allowed = r.get("allowed", [])
    labels = ", ".join(a.get("label", a.get("prefix")) if isinstance(a, dict) else str(a) for a in allowed)
    role, bad = c.role(r), False
    for ln in c.quote.lines:
        p = c.product(ln.product_id)
        cat = (p.category if p else None) or None
        if not cat:
            out = r.get("on_unknown", "needs_approval")
            if out != "allow":
                bad = True
                c.hit(f"{r['id']}:{ln.product_id}", out, f"Could not confirm the product category of {_line_name(ln)}; "
                      f"this agent may only buy {labels}." + (f" A {role_text(role)} must check it." if out == "needs_approval" else ""), role)
        elif not _cat_ok(str(cat), allowed):
            bad = True
            out = r.get("on_violation", "needs_approval")
            c.hit(f"{r['id']}:{ln.product_id}", out, f"{_line_name(ln)} (category {cat}) is outside this agent's purchasing scope "
                  f"({labels})." + (f" A {role_text(role)} must approve it." if out == "needs_approval" else ""), role)
    if not bad:
        c.hit(r["id"], "allow", f"All lines are within the allowed categories ({labels}).")


def _preferred_ids(pol: dict) -> set[str]:
    return {str(p["product_id"]) for p in pol.get("preferred_products", [])}


def _approved_alternatives(pol: dict) -> dict[str, str]:
    return {str(a["product_id"]): a.get("tier", "best") for a in pol.get("approved_alternatives", [])}


def verified_tier(c: Ctx, line) -> tuple[Optional[str], str]:
    """Decide server-side what a line really is; never trust the caller's tier.
    Returns (tier or None if it cannot be verified, how it was verified).
      preferred: product_id is in the policy's preferred_products (the customer's usual products)
      best/better/good: product_id is in the policy's approved_alternatives (customer-approved swaps)
      best: catalog shows same brand, category and unit as a preferred product and keeps all its claims
      None: anything else (missing tier defaults to "preferred" in the model, so it lands here too)"""
    pid = line.product_id
    if pid in _preferred_ids(c.policy):
        return "preferred", "listed as a usual product in the customer profile"
    alt = _approved_alternatives(c.policy)
    if pid in alt:
        return alt[pid], "a customer-approved alternative in the profile"
    if line.tier == "best":
        p = c.product(pid)
        if p is not None and p.brand:
            prefs = _catalog_lookup(sorted(_preferred_ids(c.policy) - set(c.products)))
            prefs.update({k: v for k, v in c.products.items() if k in _preferred_ids(c.policy)})
            for ref in prefs.values():
                if ref is None or not ref.brand:
                    continue
                if (ref.brand.lower() == p.brand.lower() and ref.category == p.category
                        and ref.content_unit == p.content_unit and set(ref.claims) <= set(p.claims)):
                    return "best", f"same brand, category and claims as the usual {ref.id} (catalog data)"
    return None, "not in the customer profile"


def _same_kind(a: dict, b: dict, product_types: Callable) -> bool:
    """A usual product is the reference for a line only when both are the same kind of product
    (a milk standing order says nothing about butter). Same rule as the gateway's quote verification."""
    ta, tb = product_types(a), product_types(b)
    if ta and tb:
        return bool(ta & tb)
    return bool(a.get("category")) and a.get("category") == b.get("category")


def catalog_verify(c: Ctx, line) -> dict:
    """Server-side classification of a line with the catalog's substitution engine (never the agent's label):
    {"tier", "reference" (usual product of the same kind, or None), "facts" (category/types/claims/unit),
    "reasons"}. tier None when the catalog or the request is not available. The request's product phrase,
    claims and allergens come from quote.need; the usual products come from this policy, not from the agent."""
    key = ("catalog_verify", line.product_id)
    if key in c.cache:
        return c.cache[key]
    out: dict = {"tier": None, "reference": None, "facts": None, "reasons": []}
    try:
        from aimo.catalog.substitution import product_types, rank_substitutes
        from aimo.catalog.textsearch import get_rows
        usual = sorted(_preferred_ids(c.policy))
        rows = get_rows([line.product_id, *usual])
        row = rows.get(line.product_id)
        if row:
            out["facts"] = {"category": row.get("category"), "types": sorted(product_types(row)),
                            "claims": sorted(row.get("claims") or []), "content_unit": row.get("content_unit")}
            same = [u for u in usual if u in rows and u != line.product_id and _same_kind(rows[u], row, product_types)]
            out["reference"] = same[0] if same else None
            need = c.quote.need
            if need is not None:
                ln_need = need.model_copy(update={"customer_id": c.quote.customer_id, "preferred_product_ids": same,
                                                  "preferences": {}, "delivery_by": c.quote.delivery_date})
                cands = rank_substitutes(ln_need, {}, limit=50, rows={k: rows[k] for k in [line.product_id, *same]},
                                         relevance={line.product_id: 1.0})
                cand = next((x for x in cands if x.product.id == line.product_id), None)
                if cand is not None:
                    out["tier"], out["reasons"] = cand.tier, cand.reasons[:2]
    except Exception:  # noqa: BLE001  no catalog: the line stays unverified (fail closed)
        pass
    c.cache[key] = out
    return out


def _request_names_excluded(c: Ctx) -> list[str]:
    """Excluded allergen groups that the request's product phrase itself names ('milk' at a milk-free school).
    Decided here from quote.need, never from the agent's line flags: whatever the agent planned for such a
    request is a replacement that a human confirms."""
    need = c.quote.need
    if need is None:
        return []
    excl = list(need.excluded_allergens)
    for r in c.policy.get("rules") or []:
        if r.get("type") == "allergen_block":
            excl += list(r.get("excluded") or [])
    try:
        from aimo.catalog.plan import _excluded_groups_named
        from aimo.catalog.substitution import normalize_allergens
        literal, implied = _excluded_groups_named(need.product_query, normalize_allergens(excl))
    except Exception:  # noqa: BLE001
        return []
    return literal + implied


def _learned(c: Ctx, line, reference: Optional[str], facts: Optional[dict]) -> Optional[dict]:
    try:
        from aimo.trust.learned import match
        return match(c.quote.customer_id, line.product_id, reference, facts)
    except Exception:  # noqa: BLE001
        return None


def r_substitution(c: Ctx, r: dict) -> None:
    """One hit per line, with the final (strictest) outcome. The tier is verified server-side: a line
    whose tier cannot be verified (missing, or a "preferred"/"best" claim that the profile and catalog do
    not back) is treated as a substitution (`unverified`, default needs_approval). A claimed tier worse
    than the verified one is honoured (it can only make the outcome stricter).

    Fewer pointless escalations (all still subject to claims here, and to the allergen, spend, split-order,
    price-jump and injection rules):
      - the catalog's own ranking verifies the tier server-side (same engine as planning);
      - `new_product`: the customer has no usual product of this kind and the catalog confirms the line is
        exactly what the request names (tier best). That is a first purchase, not a substitution;
      - learned swaps: a human approved this swap with "always allow" (aimo.trust.learned);
      - `auto_approve_alternatives_below_eur`: a verified-best or customer-approved alternative whose line
        total is below this amount is allowed even when its tier would need approval."""
    tiers = r.get("tiers", {})
    role = c.role(r)
    unverified_out = r.get("unverified", "needs_approval")
    small_eur = r.get("auto_approve_alternatives_below_eur")
    need_claims = list(c.quote.need.required_claims) if (c.quote.need and r.get("require_claims")) else []
    claims_txt = ", ".join(CLAIM_TEXT.get(x, x) for x in need_claims)
    noted = False
    rank = {"preferred": 0, "best": 1, "better": 2, "good": 3, "rejected": 9}
    named_excluded = _request_names_excluded(c)
    for ln in c.quote.lines:
        parts: list[tuple[str, str]] = []   # (outcome, sentence)
        vt, how = verified_tier(c, ln)
        claimed = ln.tier
        new_product, learned, profile_alt = False, None, vt is not None and how.startswith("a customer-approved")
        if vt != "preferred":
            cv = catalog_verify(c, ln)
            learned = _learned(c, ln, cv["reference"], cv["facts"])
            if vt is None and cv["tier"] in ("best", "better", "good"):
                vt = cv["tier"]
                if cv["reference"] is None and c.quote.need is not None and ln.product_id not in _preferred_ids(c.policy):
                    new_product = vt == "best"
                    how = ("catalog check: the customer has no usual product of this kind and the product matches "
                           "the request exactly" if new_product else
                           "catalog check: the customer has no usual product of this kind; only a partial match "
                           "for the request")
                else:
                    how = f"catalog check against the usual product {cv['reference']}"
        if vt is not None and rank.get(claimed, 0) > rank[vt]:
            vt = claimed  # the caller says it is worse than we could verify: believe the stricter one
            new_product = False
        if learned is not None and vt != "preferred":
            who = learned.get("approver") or "a human approver"
            parts.append(("allow", f"{_line_name(ln)} is a swap that {who} approved with 'always allow' on "
                                   f"{str(learned.get('created_at', ''))[:10]} ({learned.get('scope')})."))
            eff = vt or "best"
        elif vt is None:
            if claimed in ("better", "good", "rejected"):
                eff = claimed
                out = tiers.get(eff, "needs_approval")
                parts.append((out, f"{_line_name(ln)} is a substitute: {TIER_TEXT[eff]} (as reported by the agent)."))
            else:
                eff, out = None, unverified_out
                parts.append((out, f"Agent-supplied tier could not be verified; treating as substitution. "
                                   f"{_line_name(ln)} is not one of the customer's usual products or approved "
                                   f"alternatives (agent-supplied tier: "
                                   + ("preferred or none" if claimed == "preferred" else claimed) + ")."))
        elif new_product:
            eff = vt
            out = r.get("new_product", "needs_approval")
            parts.append((out, f"{_line_name(ln)} is a new product for this customer, not a substitution ({how})."))
        else:
            eff = vt
            out = tiers.get(eff, "allow" if eff == "preferred" else "needs_approval")
            if eff == "preferred":
                parts.append((out, f"{_line_name(ln)} is the customer's usual product."))
            else:
                if (out == "needs_approval" and small_eur is not None and ln.line_total < float(small_eur)
                        and (eff == "best" or profile_alt)):
                    out = "allow"
                    how += (f"; the line total {eur(ln.line_total)} is below the {eur(float(small_eur))} this customer "
                            f"lets the agent swap without asking")
                over = ("Agent-supplied tier could not be verified; treating as substitution. "
                        if rank.get(claimed, 0) < rank.get(eff, 0) else "")
                parts.append((out, f"{over}{_line_name(ln)} is a substitute: {TIER_TEXT[eff]}, verified as {how}."))
        if "outside_preference" in ln.flags and learned is None:
            parts.append((r.get("outside_preference", "needs_approval"),
                          "It is flagged as outside your standing preferences (brand, pack or spec)."))
        if "allergen_safe_alternative" in ln.flags or named_excluded:
            parts.append(("needs_approval", f"The request named '{c.quote.need.product_query if c.quote.need else ''}', "
                                            f"which normally contains {' and '.join(named_excluded) or 'an excluded allergen'} "
                                            "(excluded by this customer's policy); the agent planned an allergen-free "
                                            "replacement, which a human confirms."))
        if need_claims:
            p = c.product(ln.product_id)
            if p is None:
                parts.append(("needs_approval", f"Could not confirm that it is {claims_txt} as requested."))
            else:
                missing = [x for x in need_claims if x not in (p.claims or [])]
                if missing:
                    parts.append(("reject", f"It is not {', '.join(CLAIM_TEXT.get(x, x) for x in missing)}, "
                                            f"so it does not meet the request."))
        final = max((o for o, _ in parts), key=lambda o: SEVERITY[o])
        if eff == "preferred" and final == "allow" and len(parts) == 1:
            continue  # usual product, nothing to say per line
        noted = True
        tail = {"allow": " Allowed automatically by this policy.",
                "needs_approval": f" A {role_text(role)} must confirm the swap.",
                "reject": " This policy does not allow it."}[final]
        # keep the descriptive first sentence, then only the sentences that drive the final outcome
        text = [parts[0][1]] + [s for o, s in parts[1:] if o == final or final == "allow"]
        c.hit(f"{r['id']}:{ln.product_id}", final, " ".join(text) + tail, role)
    if not noted:
        msg = "All lines are the customer's usual products; no substitutions."
        if need_claims:
            msg += f" Required claims ({claims_txt}) are met."
        c.hit(r["id"], "allow", msg)


def r_allergen_block(c: Ctx, r: dict) -> None:
    excluded = list(dict.fromkeys(list(r.get("excluded", [])) + list(c.quote.need.excluded_allergens if c.quote.need else [])))
    if not excluded:
        return c.hit(r["id"], "allow", "This mandate has no allergen exclusions.")
    role = c.role(r)
    why = f" ({r['reason']})" if r.get("reason") else ""
    ex_text = " or ".join(excluded)
    bad = False
    for ln in c.quote.lines:
        p = c.product(ln.product_id)
        found: dict[str, list[str]] = {"contains": [], "may_contain": [], "unknown": []}
        inferred: list[str] = []
        for a in excluded:
            if p is None:
                st = "contains" if f"contains:{a}" in ln.flags else "may_contain" if f"may_contain:{a}" in ln.flags else "unknown"
            else:
                st = _st(p.allergens.get(a, "unknown"))
                src = (p.allergen_source or {}).get(a, "")
                if src.startswith("inferred") and st != "unknown":
                    inferred.append(a)
                    st = "unknown"  # AI-inferred is a hint for humans, never proof
            if st in found:
                found[st].append(a)
        rid = f"{r['id']}:{ln.product_id}"
        if found["contains"]:
            bad = True
            c.hit(rid, r.get("on_contains", "reject"), f"{_line_name(ln)} contains {', '.join(found['contains'])}, which is excluded "
                  f"for this customer{why}. The line must be removed.", role)
        elif found["may_contain"]:
            bad = True
            c.hit(rid, r.get("on_may_contain", "reject"), f"{_line_name(ln)} may contain traces of {', '.join(found['may_contain'])}, "
                  f"which is excluded for this customer{why}.", role)
        elif found["unknown"]:
            bad = True
            out = r.get("on_unknown", "reject")
            src = "no supplier allergen data" if p is None else "the supplier data does not state it"
            extra = (f" The {', '.join(inferred)} status was only AI-inferred from the ingredient text." if inferred else "")
            tail = {"reject": " Unknown is never treated as safe, so the line cannot be ordered.",
                    "needs_approval": f" Unknown is never treated as safe: a {role_text(role)} must check the label first.",
                    "allow": ""}[out]
            c.hit(rid, out, f"Allergen status of {', '.join(found['unknown'])} is unknown for {_line_name(ln)} ({src}).{extra}{tail}", role)
    if not bad:
        c.hit(r["id"], "allow", f"All lines are confirmed free of {ex_text} by structured supplier data.")


def r_delivery(c: Ctx, r: dict) -> None:
    q = c.quote
    bad = False
    try:
        from zoneinfo import ZoneInfo
        today = c.now.astimezone(ZoneInfo("Europe/Helsinki")).date()
    except Exception:  # noqa: BLE001
        today = c.now.date()
    if q.delivery_date < today:
        bad = True
        c.hit(r["id"], "reject", f"Delivery date {day_text(q.delivery_date)} is in the past.")
    if q.need and q.delivery_date > q.need.delivery_by:
        bad = True
        c.hit(r["id"], "reject", f"Delivery on {day_text(q.delivery_date)} is later than the requested {day_text(q.need.delivery_by)}.")
    for ln in q.lines:
        rid = f"{r['id']}:{ln.product_id}"
        if "cutoff_passed" in ln.flags:
            bad = True
            c.hit(rid, "reject", f"The order cut-off for {_line_name(ln)} has passed, so it cannot arrive by "
                  f"{day_text(q.delivery_date)}. Choose another product or date.")
        elif "unavailable" in ln.flags or "sales_block" in ln.flags:
            bad = True
            c.hit(rid, "reject", f"{_line_name(ln)} is not available for {day_text(q.delivery_date)}.")
    if not bad:
        msg = f"Delivery {day_text(q.delivery_date)}"
        msg += f" meets the requested date ({day_text(q.need.delivery_by)})" if q.need else " is in the future"
        c.hit(r["id"], "allow", msg + " and no line is past its order cut-off.")


_CONTENT_RX = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(l|ltr|dl|cl|ml|kg|g)\b", re.IGNORECASE)
_UNIT_FACTOR = {"l": ("L", 1), "ltr": ("L", 1), "dl": ("L", 0.1), "cl": ("L", 0.01), "ml": ("L", 0.001),
                "kg": ("KG", 1), "g": ("KG", 0.001)}


def content_per_unit(line, product: Optional[Product]) -> tuple[Optional[float], Optional[str]]:
    """Litres or kg in one sales unit: catalog first, else parsed from the product name."""
    if product and product.content_amount and product.content_unit:
        return float(product.content_amount), product.content_unit
    m = None
    for m in _CONTENT_RX.finditer(line.product_name or ""):
        pass  # last match is usually the pack size
    if not m:
        return None, None
    unit, f = _UNIT_FACTOR[m.group(3).lower()]
    return float(m.group(1) or 1) * float(m.group(2).replace(",", ".")) * f, unit


def _matches(text: str, keywords: list[str]) -> bool:
    t = (text or "").lower()
    return any(re.search(rf"\b{re.escape(k.lower())}", t) for k in keywords)


def r_quantity_drift(c: Ctx, r: dict) -> None:
    factor_max = float(r.get("max_factor", 3))
    role = c.role(r)
    q = c.quote
    any_match = False
    for b in c.policy.get("standing_orders", []):
        kws, unit = b.get("keywords", []), b.get("unit", "L")
        total, matched = 0.0, False
        for ln in q.lines:
            p = c.product(ln.product_id)
            if not (_matches(ln.product_name, kws) or (p and _matches(p.name_fi or "", kws))):
                continue
            amt, u = content_per_unit(ln, p)
            if amt and u == unit:
                total += amt * ln.qty_sales_units
                matched = True
        if not matched and q.need and q.need.unit == unit and _matches(q.need.product_query, kws):
            total, matched = q.need.quantity, True
        if not matched:
            continue
        any_match = True
        # rolling window: earlier orders of the same standing-order product count too (split orders)
        prior = 0.0
        for h in _history(c, int(r.get("window_days", 7))):
            for x in h["lines"] or []:
                p = c.product(x["product_id"])
                if not (_matches(x["product_name"], kws) or (p and _matches(p.name_fi or "", kws))):
                    continue
                amt, u = content_per_unit(type("L", (), {"product_name": x["product_name"]})(), p)
                if amt and u == unit:
                    prior += amt * x["qty"]
        base = float(b["weekly_qty"])
        f = (total + prior) / base if base else float("inf")
        label = b.get("label", b["id"])
        earlier = (f" including {qty_text(prior)} {unit} ordered earlier in the last {int(r.get('window_days', 7))} days"
                   if prior > 0 else "")
        if f > factor_max:
            c.hit(f"{r['id']}:{b['id']}", "needs_approval", f"This is {f:.1f}× your usual weekly volume of {label} "
                  f"({qty_text(total + prior)} {unit}{earlier} vs about {qty_text(base)} {unit} a week). "
                  f"A {role_text(role)} must confirm the quantity.", role)
        else:
            c.hit(f"{r['id']}:{b['id']}", "allow", f"{label.capitalize()} volume {qty_text(total + prior)} {unit}{earlier} is in "
                  f"line with your usual {qty_text(base)} {unit} a week ({f:.1f}×).")
    if not any_match:
        c.hit(r["id"], "allow", "No standing-order baseline covers these products, so there is no volume check.")


def r_injection(c: Ctx, r: dict) -> None:
    from aimo.trust.screening import screen_text
    role = c.role(r)
    flagged = False
    need_flags = (c.quote.need.preferences or {}).get("injection_flags") if c.quote.need else None
    if need_flags:
        flagged = True
        c.hit(r["id"], "needs_approval", f"The buyer agent reported suspicious instructions in untrusted text "
              f"({', '.join(map(str, need_flags))[:200]}). A {role_text(role)} should confirm the order was not influenced.", role)
    for ln in c.quote.lines:
        reasons = [f for f in ln.flags if "injection" in f]
        p = c.product(ln.product_id)
        if p is not None and p.marketing_text:
            s = screen_text(p.marketing_text, use_gemini=False)
            if s["suspicious"]:
                reasons += s["strong_reasons"]
        if reasons:
            flagged = True
            c.hit(f"{r['id']}:{ln.product_id}", "needs_approval", f"Supplier text for {_line_name(ln)} contains instructions "
                  f"aimed at AI agents ({', '.join(dict.fromkeys(reasons))}). The agent must not follow them; a {role_text(role)} "
                  f"should confirm this order was not influenced.", role)
    if not flagged:
        c.hit(r["id"], "allow", "No prompt-injection signals in the request or supplier text.")


RULES: dict[str, Callable[[Ctx, dict], None]] = {
    "scope": r_scope, "quote_freshness": r_quote_freshness, "spend_threshold": r_spend_threshold,
    "period_spend": r_period_spend, "category_scope": r_category_scope, "substitution": r_substitution,
    "allergen_block": r_allergen_block, "delivery": r_delivery, "quantity_drift": r_quantity_drift,
    "injection": r_injection, "split_order": r_split_order, "price_jump": r_price_jump,
}


# ------------------------------------------------------------------ evaluate
def _merge_duplicates(hits: list[tuple[RuleHit, Optional[str]]]) -> list[tuple[RuleHit, Optional[str]]]:
    """Exactly one row per rule_id (per rule per line): the strictest outcome, with the messages that
    led to it. Order of first appearance is kept."""
    groups: dict[str, list[tuple[RuleHit, Optional[str]]]] = {}
    for h, role in hits:
        groups.setdefault(h.rule_id, []).append((h, role))
    out = []
    for rid, g in groups.items():
        if len(g) == 1:
            out.append(g[0])
            continue
        worst = max(SEVERITY[h.outcome] for h, _ in g)
        top = [(h, role) for h, role in g if SEVERITY[h.outcome] == worst]
        msg = " ".join(dict.fromkeys(h.message for h, _ in top))
        out.append((RuleHit(rule_id=rid, outcome=top[0][0].outcome, message=msg), top[0][1]))
    return out


def evaluate(quote: Quote, mandate: Mandate, *, products: dict[str, Any] | None = None,
             now: datetime | None = None, audit_log: Any = None) -> PolicyDecision:
    """Evaluate a quote against the mandate's policy. `products` (id -> Product) overrides the catalog
    lookup (tests, or the gateway passing what it already loaded)."""
    pol = load_policy(mandate.policy_id)
    ids = list(dict.fromkeys(ln.product_id for ln in quote.lines))
    prods: dict[str, Optional[Product]] = {}
    if products is not None:
        for i, p in products.items():   # extra entries (e.g. the usual products) are kept as references
            prods[i] = Product.model_validate(p) if isinstance(p, dict) else p
        for i in ids:
            prods.setdefault(i, None)
        missing = [i for i in ids if prods.get(i) is None]
        if missing:
            prods.update({k: v for k, v in _catalog_lookup(missing).items() if v is not None})
    else:
        prods = _catalog_lookup(ids)
    c = Ctx(quote=quote, mandate=mandate, policy=pol, products=prods,
            now=_aware(now or datetime.now(timezone.utc)), audit_log=audit_log)
    _integrity(c)
    for rule in pol["rules"]:
        try:
            RULES[rule["type"]](c, rule)
        except Exception as e:  # noqa: BLE001  a crashing rule must never auto-approve
            c.hit(rule["id"], "needs_approval", f"Rule {rule['id']} could not be evaluated ({type(e).__name__}); "
                  f"a human must review this order.", c.role(rule))
    c.hits = _merge_duplicates(c.hits)
    worst = max((SEVERITY[h.outcome] for h, _ in c.hits), default=0)
    outcome = {0: "auto_approve", 1: "needs_approval", 2: "reject"}[worst]
    approver = None
    if outcome == "needs_approval":
        order = list(pol.get("approver_roles", []))
        roles = [role for h, role in c.hits if h.outcome == "needs_approval" and role]
        roles.sort(key=lambda x: order.index(x) if x in order else len(order))
        approver = roles[0] if roles else (order[0] if order else None)
    return PolicyDecision(outcome=outcome, approver_role=approver, hits=[h for h, _ in c.hits],
                          policy_id=pol["policy_id"], policy_version=str(pol["version"]))


def explain(decision: PolicyDecision) -> str:
    """One sentence for humans: what happens next and why."""
    bad = [h for h in decision.hits if h.outcome != "allow"]
    if decision.outcome == "auto_approve":
        return f"Auto-approved: all {len(decision.hits)} checks passed under policy {decision.policy_id} v{decision.policy_version}."
    if decision.outcome == "reject":
        rej = [h for h in bad if h.outcome == "reject"]
        return f"Rejected: {rej[0].message}" + (f" (+{len(rej) - 1} more)" if len(rej) > 1 else "")
    return (f"Needs approval by a {role_text(decision.approver_role)}: {len(bad)} rule(s) need a human "
            f"({', '.join(h.rule_id.split(':')[0] for h in bad)}).")


# ------------------------------------------------------------------ customers (for /v1/customers)
def _summary(pol: dict) -> list[str]:
    out = []
    for r in pol["rules"]:
        t, role = r["type"], role_text(r.get("approver_role") or (pol.get("approver_roles") or [None])[0])
        if t == "spend_threshold":
            out.append(f"Orders up to {eur(r['auto_max'])} are placed automatically; up to {eur(r['approval_max'])} need a {role}; "
                       f"larger orders are refused.")
        elif t == "period_spend":
            out.append(f"{r.get('period_days', 7)}-day budget {eur(r['limit'])}; going over it needs a {role}.")
        elif t == "category_scope":
            out.append("The agent may only buy: " + ", ".join(a.get("label", a.get("prefix")) if isinstance(a, dict) else str(a)
                                                            for a in r.get("allowed", [])) + ".")
        elif t == "substitution":
            tiers = r.get("tiers", {})
            auto = [k for k, v in tiers.items() if v == "allow" and k != "preferred"]
            appr = [k for k, v in tiers.items() if v == "needs_approval"]
            s = "Substitutes: " + (f"{'/'.join(auto)} tier allowed automatically; " if auto else "")
            s += (f"{'/'.join(appr)} tier need a {role}" if appr else "") + "."
            if r.get("require_claims"):
                s += " Required claims (e.g. lactose free) must be kept."
            s += (" Only the products in the customer profile count as usual; the agent's own tier label is never "
                  "trusted: the catalog re-checks it, and anything unverified is treated as a substitution.")
            if r.get("new_product") == "allow":
                s += " A first purchase that exactly matches the request (no usual product of that kind) is not a substitution."
            if r.get("auto_approve_alternatives_below_eur") is not None:
                s += (f" Verified-best or pre-approved alternatives under {eur(float(r['auto_approve_alternatives_below_eur']))}"
                      " a line are swapped without asking.")
            s += " Swaps a human approved with 'always allow' are remembered."
            out.append(s)
        elif t == "allergen_block":
            ex = r.get("excluded", [])
            if ex:
                unk = {"reject": "are refused", "needs_approval": f"need a {role}", "allow": "are allowed"}[r.get("on_unknown", "reject")]
                out.append(f"Never orders products that contain or may contain {' or '.join(ex)}; products with unknown "
                           f"allergen data {unk}.")
            else:
                out.append("Allergen exclusions come from each request; unknown allergen data is never treated as safe.")
        elif t == "delivery":
            out.append("Lines past their order cut-off or arriving after the needed date are refused.")
        elif t == "quantity_drift":
            bl = ", ".join(f"{b.get('label', b['id'])} {qty_text(b['weekly_qty'])} {b.get('unit', '')}/week"
                           for b in pol.get("standing_orders", []))
            out.append(f"Orders over {r.get('max_factor', 3)}× the usual weekly volume ({bl}) need a {role}.")
        elif t == "quote_freshness":
            out.append("Quotes are only honoured while their price lock is valid.")
        elif t == "scope":
            out.append("Only registered agents with the order:submit permission may order.")
        elif t == "injection":
            out.append(f"Orders touched by suspected prompt injection in supplier text need a {role}.")
        elif t == "split_order":
            out.append(f"Several small orders of the same thing within {r.get('window_days', 7)} days that add up to more than "
                       f"{r.get('max_factor', 2)}x the auto-approve limit need a {role} (split-order evasion).")
        elif t == "price_jump":
            out.append(f"A price more than {r.get('max_increase_pct', 15)}% above the last price paid needs a {role}.")
    return out


def list_customers() -> list[dict]:
    """Demo customers with agents, mandates and a human-readable policy summary."""
    out = []
    for pid in policy_ids():
        pol = load_policy(pid)
        cust = pol["customer"]
        excluded = sorted({a for r in pol["rules"] if r["type"] == "allergen_block" for a in r.get("excluded", [])})
        mandates = [get_mandate(cust["id"], a["agent_id"]).model_dump(mode="json") for a in pol.get("agents", [])]
        out.append({
            "customer_id": cust["id"], "name": cust.get("name"), "segment": cust.get("segment"),
            "description": cust.get("description"), "policy_id": pol["policy_id"], "policy_version": str(pol["version"]),
            "approver_roles": pol.get("approver_roles", []), "override_roles": pol.get("override_roles", []),
            "approvers": pol.get("approvers", []), "agents": pol.get("agents", []), "mandates": mandates,
            "excluded_allergens": excluded, "standing_orders": pol.get("standing_orders", []),
            "preferred_products": pol.get("preferred_products", []),
            "approved_alternatives": pol.get("approved_alternatives", []),
            "rules": [{"id": r["id"], "type": r["type"]} for r in pol["rules"]], "summary": _summary(pol),
        })
    return out
