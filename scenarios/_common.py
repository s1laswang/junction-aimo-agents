"""Shared helpers for the acceptance scenarios (s0..s7) that run against OUR HTTP API.

Ported from Momin's junction26-valio `scenarios/_common.py` (the Run/check/guard pattern, next_monday,
next_delivery_day, the PASS/FAIL table), with credit; the client is rewritten for our /v1 contract
(X-Customer-Id / X-Agent-Id headers, Need -> /v1/plan, signed Quote, quote_hash orders, approvals by a named
human from the customer's directory).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

APPROVERS = {  # from aimo/trust/policies/*.yaml (approvers directory)
    "hotel-aurora": ("anna@hotel-aurora.example", "procurement_manager"),
    "school-tampere": ("liisa@school-tampere.example", "kitchen_manager"),
    "hospital-meilahti": None,  # filled from the policy at runtime (dietitian / procurement_manager)
}
SELLER = "valio-aimo/seller-1"


def parse_base() -> str:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AIMO_URL", "http://127.0.0.1:8810"))
    return ap.parse_args().base.rstrip("/")


class Run:
    """Momin's Run: named checks, PASS/FAIL lines, first failure as the reason."""

    def __init__(self, name: str, base: str, quiet: bool = False):
        self.name, self.base, self.quiet, self.results = name, base, quiet, []
        self.notes: list[str] = []
        if base:
            _BASES.append(base)

    def say(self, text: str) -> None:
        self.notes.append(text)
        if not self.quiet:
            print(f"  {text}")

    def check(self, label: str, ok: Any, detail: Any = "") -> bool:
        self.results.append((label, bool(ok), str(detail)[:300]))
        if not self.quiet:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" ({str(detail)[:200]})" if detail and not ok else ""))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r[1] for r in self.results)

    def reason(self) -> str:
        bad = [r for r in self.results if not r[1]]
        if bad:
            return f"{bad[0][0]}: {bad[0][2]}"[:160]
        return f"{len(self.results)} checks"

    def guard(self, fn: Callable[["Run"], None]) -> "Run":
        try:
            fn(self)
        except Exception as e:  # noqa: BLE001
            self.check("ran without error", False, f"{type(e).__name__}: {e}"[:300])
        return self


class Client:
    """Our /v1 API as one customer's agent (default buyer-1)."""

    def __init__(self, base: str, customer: str, agent: str = "buyer-1", faults: Optional[str] = None):
        h = {"X-Customer-Id": customer, "X-Agent-Id": agent}
        if faults:
            h["X-Aimo-Faults"] = faults
        self.customer, self.agent = customer, agent
        self.h = httpx.Client(base_url=base, headers=h, timeout=120)

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        return self.h.request(method, path, **kw)

    def j(self, method: str, path: str, **kw) -> Any:
        r = self.call(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def plan(self, query: str, qty: float, unit: str, day: date, claims=(), excluded=(), preferred=()) -> dict:
        need = {"customer_id": self.customer, "product_query": query, "quantity": qty, "unit": unit,
                "delivery_by": str(day), "required_claims": list(claims), "excluded_allergens": list(excluded),
                "preferred_product_ids": list(preferred), "raw_text": f"{qty:g} {unit} {query} by {day}"}
        return self.j("POST", "/v1/plan", json=need)

    def quote(self, lines: list[dict], day: date | str, need: Optional[dict] = None) -> httpx.Response:
        body = {"customer_id": self.customer, "delivery_date": str(day), "need": need,
                "lines": [{"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"]} for l in lines]}
        return self.call("POST", "/v1/quotes", json=body)

    def quote_ok(self, lines: list[dict], day: date | str, need: Optional[dict] = None, retries: int = 2) -> dict:
        """Quote; on 409 lines_unavailable drop the refused lines and retry (what a buyer agent does)."""
        lines = list(lines)
        for _ in range(retries + 1):
            r = self.quote(lines, day, need)
            if r.status_code == 409 and r.json().get("error") == "lines_unavailable":
                bad = {x["product_id"] for x in r.json()["detail"]["refused"]}
                lines = [l for l in lines if l["product_id"] not in bad]
                if not lines:
                    break
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"quote -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError("no orderable lines left to quote")

    def order(self, q: dict, quote_hash: Optional[str] = None) -> httpx.Response:
        return self.call("POST", "/v1/orders", json={"quote_id": q["quote_id"], "quote_hash": quote_hash or q["quote_hash"]})

    def approve(self, order: dict, quote_hash: str, approve: bool = True, note: str = "scenario") -> dict:
        who = approver_for(self, order["decision"].get("approver_role"))
        body = {"decision": "approve" if approve else "reject", "approver": who[0], "approver_role": who[1],
                "note": note, "quote_hash": quote_hash}
        # a human decides: no agent identity, plus the operator's approver credential
        human = {"X-Agent-Id": "", "X-Customer-Id": "", "X-Approver-Pin": os.environ.get("AIMO_APPROVER_PIN", "")}
        return self.j("POST", f"/v1/approvals/{order['order_id']}/decision", json=body, headers=human)

    def audit(self, subject_id: str) -> list[dict]:
        return self.j("GET", "/v1/audit", params={"subject_id": subject_id})


def approver_for(c: Client, role: Optional[str]) -> tuple[str, str]:
    """A human from the customer's approver directory with the needed role (aimo.trust policy YAML)."""
    from aimo.trust import load_policy

    pol = load_policy(c.customer)
    for a in pol.get("approvers", []):
        if role and a["role"] == role:
            return a["id"], a["role"]
    for a in pol.get("approvers", []):
        if a["role"] in (pol.get("override_roles") or []):
            return a["id"], a["role"]
    a = pol["approvers"][0]
    return a["id"], a["role"]


_BASES: list[str] = []


def sim_now(base: Optional[str] = None) -> datetime:
    from aimo.backend import clock

    b = base or (_BASES[-1] if _BASES else None)
    if b:
        try:
            v = httpx.get(b.rstrip("/") + "/healthz", timeout=10).json().get("clock", {}).get("now")
            dt = clock.parse(v)
            if dt is not None:
                return dt
        except Exception:
            pass
    return clock.parse(os.environ.get("AIMO_NOW")) or datetime.now(timezone.utc)


def sim_today(base: Optional[str] = None) -> date:
    from aimo.backend import clock

    return clock.today_local(sim_now(base))


def next_monday(today: Optional[date] = None) -> date:
    """The next feasible delivery day (Monday 16 Nov on the demo clock; the next weekday whose Stock cut-off
    is still ahead once Sunday 17:00 has passed). Kept under its old name for the scenarios."""
    from aimo.backend import clock

    if today is not None:
        return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return clock.next_delivery_day(sim_now())


def next_delivery_day(min_days: int = 2, today: Optional[date] = None) -> date:
    from aimo.backend import clock

    if today is not None:
        d = today + timedelta(days=min_days)
        while d.weekday() > 4:
            d += timedelta(days=1)
        return d
    return clock.next_delivery_day(sim_now(), min_days=min_days)


def next_weekday(wd: int, today: Optional[date] = None) -> date:
    d = today or sim_today()
    return d + timedelta(days=(wd - d.weekday()) % 7 or 7)


def verify_signed(base: str, quote: dict) -> dict:
    """Third-party check of a quote: recompute hash, verify the JWS against the server's JWKS."""
    from aimo.models import Quote
    from aimo.trust.signing import verify_quote_detailed

    jwks = httpx.get(base + "/.well-known/jwks.json", timeout=30).json()
    return verify_quote_detailed(Quote.model_validate(quote), jwks)


def allergen_status(c: Client, pid: str, groups: list[str]) -> dict[str, str]:
    p = c.j("GET", f"/v1/products/{pid}")
    return {g: (p.get("allergens") or {}).get(g, "unknown") for g in groups}


def table(rows: list[tuple[bool, str, str]]) -> tuple[str, int]:
    w = max([len(r[1]) for r in rows] + [8])
    lines = [f"{'PASS' if ok else 'FAIL'}  {n.ljust(w)}  {why}" for ok, n, why in rows]
    fails = sum(1 for r in rows if not r[0])
    lines.append(f"{len(rows) - fails}/{len(rows)} passed")
    return "\n".join(lines), fails


def main(name: str, fn: Callable[[Run], None]) -> None:
    base = parse_base()
    print(f"== {name} against {base}")
    r = Run(name, base).guard(fn)
    print(f"{name}: {'PASS' if r.ok else 'FAIL'}" + ("" if r.ok else f" - {r.reason()}"))
    sys.exit(0 if r.ok else 1)
