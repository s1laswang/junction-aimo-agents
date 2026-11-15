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
RULE_TYPES = {"scope", "quote_freshness", "spend_threshold", "period_spend", "rolling_aggregate", "price_drift",
              "category_scope", "substitution",
              "allergen_block", "delivery", "quantity_drift", "injection"}
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


def eur_short(x: float) -> str:
    return f"€{x:,.0f}" if abs(x - round(x)) < 0.005 else eur(x)


_ORDER_ACTIONS = ("order.placed", "order.pending_approval", "order.approved", "order.rejected",
                  "order.rejected_by_human", "order.expired", "order.failed")


def _order_history(c: Ctx) -> list[dict]:
    """This customer's earlier orders from the audit log, latest state per order:
    [{key, status, ts, total_net, lines (list|None), sanctioned (a human approved it)}].
    Lines come from data.lines (log_order), data.quote.lines, or the quote.created event of the quote."""
    if "orders" in c.cache:
        return c.cache["orders"]
    log = c.audit_log
    if log is None:
        from aimo.trust.audit import audit as log
    orders: dict[str, dict] = {}
    quotes: dict[str, list] = {}
    try:
        evs = []
        for a in _ORDER_ACTIONS:
            evs += [(a, e) for e in log.events_by_action(a)]
        evs.sort(key=lambda t: (_aware(t[1].ts), getattr(t[1], "seq", 0)))
        try:
            for e in log.events_by_action("quote.created"):
                q = (e.data or {}).get("quote") or {}
                if q.get("quote_id"):
                    quotes[q["quote_id"]] = q.get("lines") or []
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        c.cache["orders"] = []
        return []
    for action, e in evs:
        d = e.data or {}
        o = d.get("order") or {}
        cust = d.get("customer_id") or o.get("customer_id") or (d.get("quote") or {}).get("customer_id")
        qid = d.get("quote_id") or o.get("quote_id")
        if cust != c.quote.customer_id or qid == c.quote.quote_id:
            continue
        key = o.get("order_id") or d.get("order_id") or qid or getattr(e, "subject_id", None) or str(id(e))
        st = orders.setdefault(key, {"key": key, "quote_id": qid, "lines": None, "total_net": None, "sanctioned": False})
        st["status"] = action.split(".", 1)[1]
        st["ts"] = _aware(e.ts)
        if d.get("total_net") is not None:
            st["total_net"] = float(d["total_net"])
        lines = d.get("lines") or (d.get("quote") or {}).get("lines") or (quotes.get(qid) if qid else None)
        if lines:
            st["lines"] = lines
        if d.get("approver") or o.get("approved_by") or action == "order.approved":
            st["sanctioned"] = True
    out = list(orders.values())
    c.cache["orders"] = out
    return out


def _group(c: Ctx, product_id: str, name: str) -> tuple[str, str]:
    """Aggregation group of a line: a standing-order baseline it matches (e.g. all milk), else the
    catalog product_type when present, else the product itself. Returns (group key, label)."""
    p = c.product(product_id)
    for b in c.policy.get("standing_orders", []):
        if _matches(name or "", b.get("keywords", [])) or (p is not None and _matches(p.name_fi or "", b.get("keywords", []))):
            return f"baseline:{b['id']}", b.get("label", b["id"])
    pt = getattr(p, "product_type", None) if p is not None else None
    if pt:
        return f"type:{pt}", str(pt)
    return f"product:{product_id}", f"'{name}'"


def _line_volume(c: Ctx, ln: dict | Any) -> tuple[Optional[float], Optional[str]]:
    get = (lambda k: ln.get(k)) if isinstance(ln, dict) else (lambda k: getattr(ln, k, None))

    class _L:  # adapter for content_per_unit
        product_name = get("product_name") or ""
    amt, unit = content_per_unit(_L, c.product(get("product_id")) if get("product_id") else None)
    if amt is None and get("content_amount"):
        amt, unit = float(get("content_amount")), get("content_unit")
    return (amt * float(get("qty_sales_units") or 0), unit) if amt else (None, None)


def r_rolling_aggregate(c: Ctx, r: dict) -> None:
    """Split-order evasion: earlier unsanctioned orders in the window (auto-placed or still pending) plus
    this one are summed per product group and checked against the per-order auto-approve limit and the
    weekly volume baselines. Orders a human approved are not re-counted (that volume was sanctioned).
    If an earlier order carries no line detail, its total counts against every group (fail closed)."""
    days = int(r.get("window_days", 7))
    since = c.now - timedelta(days=days)
    role = c.role(r)
    spend_rule = next((x for x in c.policy["rules"] if x["type"] == "spend_threshold"), None)
    auto_max = float(spend_rule["auto_max"]) if spend_rule else None
    drift_rule = next((x for x in c.policy["rules"] if x["type"] == "quantity_drift"), None)
    max_factor = float(drift_rule.get("max_factor", 3)) if drift_rule else None
    prior = [o for o in _order_history(c) if o.get("status") in ("placed", "pending_approval")
             and not o["sanctioned"] and o.get("ts") and since <= o["ts"] <= c.now + timedelta(minutes=5)]
    if not prior:
        return c.hit(r["id"], "allow", f"No other unapproved orders in the last {days} days to add up.")
    cur: dict[str, dict] = {}
    for ln in c.quote.lines:
        g, label = _group(c, ln.product_id, ln.product_name)
        x = cur.setdefault(g, {"label": label, "spend": 0.0, "vol": 0.0, "unit": None})
        x["spend"] += ln.line_total
        v, u = _line_volume(c, ln)
        if v:
            x["vol"] += v
            x["unit"] = x["unit"] or u
    flagged = False
    blind = [o for o in prior if not o["lines"]]
    for g, x in cur.items():
        orders_n, p_spend, p_vol = set(), 0.0, 0.0
        for o in prior:
            if not o["lines"]:
                continue
            for pl in o["lines"]:
                pg, _ = _group(c, str(pl.get("product_id")), pl.get("product_name") or "")
                if pg == g:
                    orders_n.add(o["key"])
                    p_spend += float(pl.get("line_total") or 0)
                    v, u = _line_volume(c, pl)
                    if v and (x["unit"] is None or u == x["unit"]):
                        p_vol += v
        for o in blind:
            orders_n.add(o["key"])
            p_spend += float(o.get("total_net") or 0)
        if not orders_n:
            continue
        n = len(orders_n)
        together = f"Together with {n} order{'s' if n != 1 else ''} in the last {days} days"
        tot = p_spend + x["spend"]
        if auto_max is not None and c.quote.total_net <= auto_max and tot > auto_max:
            flagged = True
            c.hit(f"{r['id']}:{g.split(':', 1)[1]}", "needs_approval",
                  f"{together} this is {eur(tot)} of {x['label']}, above the {eur_short(auto_max)} limit"
                  + (" (earlier orders without line detail are counted in full)" if blind else "")
                  + f". Splitting an order does not avoid approval: a {role_text(role)} must approve it.", role)
            continue
        if max_factor and g.startswith("baseline:") and x["vol"]:
            b = next(b for b in c.policy.get("standing_orders", []) if f"baseline:{b['id']}" == g)
            base = float(b["weekly_qty"]) * days / 7
            vol = p_vol + x["vol"]
            if x["unit"] == b.get("unit") and vol > max_factor * base and x["vol"] <= max_factor * base:
                flagged = True
                c.hit(f"{r['id']}:{g.split(':', 1)[1]}", "needs_approval",
                      f"{together} this is {vol / base:.1f}× your usual weekly volume of {x['label']} "
                      f"({qty_text(vol)} {x['unit']} vs about {qty_text(base)} {x['unit']}). A {role_text(role)} must "
                      f"confirm the quantity.", role)
    if not flagged:
        c.hit(r["id"], "allow", f"Together with {len(prior)} earlier unapproved order(s) in the last {days} days, "
                                f"no product group goes over the auto-approve limit or the weekly volume baseline.")


def _last_paid(c: Ctx, pid: str, lookback_days: int) -> Optional[tuple[float, datetime]]:
    since = c.now - timedelta(days=lookback_days)
    best = None
    for o in _order_history(c):
        if o.get("status") not in ("placed", "approved") or not o["lines"] or not o.get("ts") or o["ts"] < since:
            continue
        for pl in o["lines"]:
            if str(pl.get("product_id")) == pid and pl.get("unit_price"):
                if best is None or o["ts"] > best[1]:
                    best = (float(pl["unit_price"]), o["ts"])
    return best


def r_price_drift(c: Ctx, r: dict) -> None:
    """Each line's unit price vs the last price this customer paid (audit order.placed), else the policy's
    reference_prices. The buyer agent's own reference (need.preferences.reference_unit_price) is untrusted
    and can only make the check stricter. > max_increase up -> needs approval; > reject_factor -> only an
    override role (e.g. general manager) may approve."""
    max_inc = float(r.get("max_increase", 0.15))
    factor = float(r.get("reject_factor", 2.0))
    tol = 0.005
    role = c.role(r)
    senior = r.get("over_factor_role") or (c.policy.get("override_roles") or [role])[0]
    refs = {str(k): float(v) for k, v in (c.policy.get("reference_prices") or {}).items()}
    agent_refs = {}
    if c.quote.need and isinstance((c.quote.need.preferences or {}).get("reference_unit_price"), dict):
        for k, v in c.quote.need.preferences["reference_unit_price"].items():
            try:
                agent_refs[str(k)] = float(v)
            except (TypeError, ValueError):
                pass
    flagged, checked = False, 0
    for ln in c.quote.lines:
        pid = ln.product_id
        cands = []
        lp = _last_paid(c, pid, int(r.get("lookback_days", 90)))
        if lp:
            cands.append((lp[0], f"since your last order on {lp[1]:%-d %b}"))
        elif pid in refs:
            cands.append((refs[pid], f"vs your agreed reference price {eur(refs[pid])}"))
        if pid in agent_refs and agent_refs[pid] > 0:
            if not cands or agent_refs[pid] < cands[0][0]:   # untrusted: only used when it is stricter
                cands.append((agent_refs[pid], f"vs the reference price {eur(agent_refs[pid])} the buyer reported"))
        if not cands or ln.unit_price <= 0:
            continue
        checked += 1
        ref, src = min(cands, key=lambda t: t[0])
        ratio = ln.unit_price / ref if ref > 0 else float("inf")
        rid = f"{r['id']}:{pid}"
        if ratio > factor + tol:
            flagged = True
            c.hit(rid, "needs_approval", f"Price of {_line_name(ln)} rose {ratio:.1f}× {src} ({eur(ref)} → "
                  f"{eur(ln.unit_price)}). That is more than {factor:g}×, so this would be refused unless a "
                  f"{role_text(senior)} approves it.", senior)
        elif ratio > 1 + max_inc + tol:
            flagged = True
            c.hit(rid, "needs_approval", f"Price of {_line_name(ln)} rose {100 * (ratio - 1):.0f}% {src} ({eur(ref)} → "
                  f"{eur(ln.unit_price)}), more than the {100 * max_inc:.0f}% this policy accepts. A {role_text(role)} "
                  f"must confirm the price.", role)
    if not flagged:
        c.hit(r["id"], "allow", f"No price rose more than {100 * max_inc:.0f}% against the last price paid or the "
                                f"reference ({checked} line(s) had a reference)." if checked else
              "No earlier price or reference price to compare with.")


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


def product_kind(p: Optional[Product]) -> Optional[str]:
    """Kind of product used to decide what a line could replace: the catalog's product_type when it has
    one, else the Valio Aimo category code (e.g. 10400 milk drinks, 10402 plant drinks)."""
    if p is None:
        return None
    pt = getattr(p, "product_type", None)
    return str(pt) if pt else (str(p.category) if p.category else None)


def _name_types(p: Product) -> Optional[frozenset]:
    """Catalog's name-based type markers (sour milk, flavoured, organic...), if the catalog is available."""
    try:
        from aimo.catalog.substitution import product_types
        return frozenset(product_types({"name_en": p.name or "", "name_fi": p.name_fi or ""}))
    except Exception:  # noqa: BLE001
        return None


def _excluded_allergens(c: Ctx) -> list[str]:
    ex = [a for r in c.policy["rules"] if r["type"] == "allergen_block" for a in r.get("excluded", [])]
    return list(dict.fromkeys(ex + list(c.quote.need.excluded_allergens if c.quote.need else [])))


def _refs(c: Ctx, ids: list[str]) -> dict[str, Optional[Product]]:
    """Products for reference ids (usual products), from the supplied products or the catalog (cached)."""
    cache = c.cache.setdefault("refs", {})
    missing = [i for i in ids if i not in cache and i not in c.products]
    if missing:
        cache.update(_catalog_lookup(missing))
    return {i: c.products.get(i) if c.products.get(i) is not None else cache.get(i) for i in ids}


def _same_kind(a: Optional[Product], b: Optional[Product]) -> bool:
    """Unknown kind on either side counts as the same kind (fail closed: it may be a substitution)."""
    ka, kb = product_kind(a), product_kind(b)
    return ka is None or kb is None or ka == kb


def _verify_best(c: Ctx, p: Optional[Product], refs: dict[str, Optional[Product]]) -> Optional[str]:
    """Catalog evidence that p is a best-tier substitute for one of refs: same brand, same kind, same
    name-type markers, every required claim kept, and confirmed free of every excluded allergen.
    Returns the reference id it was verified against, or None."""
    if p is None or not p.brand:
        return None
    need_claims = set(c.quote.need.required_claims) if c.quote.need else set()
    if not need_claims <= set(p.claims or []):
        return None
    for a in _excluded_allergens(c):
        st = _st(p.allergens.get(a, "unknown"))
        if st != "free" or str((p.allergen_source or {}).get(a, "")).startswith("inferred"):
            return None
    tp = _name_types(p)
    for rid, ref in refs.items():
        if ref is None or not ref.brand or ref.id == p.id:
            continue
        if ref.brand.strip().lower() != p.brand.strip().lower() or product_kind(ref) != product_kind(p):
            continue
        if ref.content_unit and p.content_unit and ref.content_unit != p.content_unit:
            continue
        tr = _name_types(ref)
        if tp is not None and tr is not None and tp != tr:
            continue
        return rid
    return None


def classify_line(c: Ctx, line) -> dict:
    """Decide server-side what a line is; the caller's tier is never trusted to loosen anything.
    Returns {"kind", "tier", "how", "replaces", "learned"} where kind is one of
      preferred      in the policy's preferred_products (the customer's usual products)
      alternative    in the policy's approved_alternatives (tier from the profile)
      learned        a swap a human approved earlier with "remember" (aimo.trust.learned)
      new_item       no usual product of this kind exists (nothing is being replaced)
      requested      the request names this product and the profile has no usual product of this kind
      substitution   replaces a usual (or requested) product; tier verified (best) or unverified (None)"""
    pid = line.product_id
    pol = c.policy
    prefs = sorted(_preferred_ids(pol))
    if pid in prefs:
        return {"kind": "preferred", "tier": "preferred", "how": "listed as a usual product in the customer profile"}
    alts = _approved_alternatives(pol)
    if pid in alts:
        return {"kind": "alternative", "tier": alts[pid], "how": "a customer-approved alternative in the profile"}
    p = c.product(pid)
    usual = _refs(c, prefs)
    replaces = [u for u in prefs if p is None or _same_kind(usual.get(u), p)]
    need_prefs = [x for x in (c.quote.need.preferred_product_ids if c.quote.need else []) if x and x != pid]
    if not replaces and c.quote.need and pid in (c.quote.need.preferred_product_ids or []):
        return {"kind": "requested", "tier": None, "how": "the product the request names"}
    if not replaces:
        replaces = need_prefs
    if not replaces:
        return {"kind": "new_item", "tier": None, "how": "no usual product of this kind in the customer profile"}
    refs = _refs(c, replaces)
    try:
        from aimo.trust.learned import is_learned
        keys = list(replaces) + [k for k in (product_kind(x) for x in refs.values()) if k] + \
            ([product_kind(p)] if product_kind(p) else [])
        lr = is_learned(c.quote.customer_id, pid, keys, now=c.now)
    except Exception:  # noqa: BLE001
        lr = None
    if lr:
        return {"kind": "learned", "tier": None, "how": "a swap approved earlier", "learned": lr, "replaces": replaces}
    if line.tier in ("better", "good", "rejected"):
        return {"kind": "substitution", "tier": line.tier, "how": "as reported by the agent", "replaces": replaces}
    ref = _verify_best(c, p, refs)
    if ref:
        return {"kind": "substitution", "tier": "best", "replaces": replaces,
                "how": f"same brand, kind, claims and allergen-safe as {ref} (catalog data)"}
    return {"kind": "substitution", "tier": None, "how": "not verified", "replaces": replaces}


def r_substitution(c: Ctx, r: dict) -> None:
    """One hit per line, with the final (strictest) outcome. See classify_line(): a usual product or a
    new item (nothing replaced) is fine; a substitute is judged by its VERIFIED tier; an unverifiable
    tier counts as `unverified` (default needs_approval); a claimed tier worse than the verified one is
    honoured; a learned swap is allowed."""
    tiers = r.get("tiers", {})
    role = c.role(r)
    unverified_out = r.get("unverified", "needs_approval")
    need_claims = list(c.quote.need.required_claims) if (c.quote.need and r.get("require_claims")) else []
    claims_txt = ", ".join(CLAIM_TEXT.get(x, x) for x in need_claims)
    noted = False
    rank = {"preferred": 0, "best": 1, "better": 2, "good": 3, "rejected": 9}
    for ln in c.quote.lines:
        parts: list[tuple[str, str]] = []   # (outcome, sentence)
        cl = classify_line(c, ln)
        kind, eff = cl["kind"], cl["tier"]
        if eff is not None and rank.get(ln.tier, 0) > rank.get(eff, 0) and kind in ("alternative", "substitution"):
            eff = ln.tier  # the caller says it is worse than we could verify: believe the stricter one
        quiet = False
        if kind == "preferred":
            parts.append(("allow", f"{_line_name(ln)} is the customer's usual product."))
            quiet = True
        elif kind in ("new_item", "requested"):
            if ln.tier == "rejected":
                parts.append(("reject", f"{_line_name(ln)} was ranked not acceptable by the substitution engine."))
            else:
                what = ("a new item: the customer profile has no usual product of this kind, so nothing is being "
                        "replaced" if kind == "new_item" else "the product named in the request, and the customer "
                        "profile has no usual product of this kind")
                parts.append(("allow", f"{_line_name(ln)} is {what}."))
                quiet = True
        elif kind == "learned":
            lr = cl["learned"]
            when = str(lr.get("created_at", ""))[:10]
            try:
                when = datetime.fromisoformat(lr["created_at"]).strftime("%-d %b %Y")
            except Exception:  # noqa: BLE001
                pass
            parts.append(("allow", f"{_line_name(ln)} replaces {', '.join(cl['replaces'])}. Allowed: you approved this "
                                   f"swap on {when} ({lr.get('approver')})."))
        elif eff is None:
            parts.append((unverified_out, f"Agent-supplied tier could not be verified; treating as substitution. "
                                          f"{_line_name(ln)} replaces {', '.join(cl.get('replaces') or [])} but is not "
                                          f"a usual product, an approved alternative or a catalog-verified best-tier "
                                          f"substitute (agent-supplied tier: "
                          + ("preferred or none" if ln.tier == "preferred" else ln.tier) + ")."))
        else:
            out = tiers.get(eff, "needs_approval")
            parts.append((out, f"{_line_name(ln)} is a substitute: {TIER_TEXT[eff]}, verified as {cl['how']}."))
        if "outside_preference" in ln.flags:
            parts.append((r.get("outside_preference", "needs_approval"),
                          "It is flagged as outside your standing preferences (brand, pack or spec)."))
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
        if quiet and final == "allow" and len(parts) == 1 and kind == "preferred":
            continue  # usual product, nothing to say per line
        noted = True
        tail = {"allow": "" if kind in ("new_item", "requested", "learned") else " Allowed automatically by this policy.",
                "needs_approval": f" A {role_text(role)} must confirm the swap.",
                "reject": " This policy does not allow it."}[final]
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
        base = float(b["weekly_qty"])
        f = total / base if base else float("inf")
        label = b.get("label", b["id"])
        if f > factor_max:
            c.hit(f"{r['id']}:{b['id']}", "needs_approval", f"This is {f:.1f}× your usual weekly volume of {label} "
                  f"({qty_text(total)} {unit} vs about {qty_text(base)} {unit} a week). A {role_text(role)} must confirm the quantity.", role)
        else:
            c.hit(f"{r['id']}:{b['id']}", "allow", f"{label.capitalize()} volume {qty_text(total)} {unit} is in line with your usual "
                  f"{qty_text(base)} {unit} a week ({f:.1f}×).")
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
    "period_spend": r_period_spend, "rolling_aggregate": r_rolling_aggregate, "price_drift": r_price_drift,
    "category_scope": r_category_scope, "substitution": r_substitution,
    "allergen_block": r_allergen_block, "delivery": r_delivery, "quantity_drift": r_quantity_drift,
    "injection": r_injection,
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
                  "trusted, and anything unverified is treated as a substitution.")
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
