"""Run the Trusted Trade profile conformance suite against any wholesaler gateway.

    uv run python -m conformance --base http://127.0.0.1:8000
    uv run python -m conformance --base http://127.0.0.1:8820 --query bread --json out.json

Needs only httpx, cryptography and (for the MCP checks) the `mcp` SDK; it never imports the implementation it
tests. It places TEST ORDERS (one auto-approved or pending, one escalated to approval): run it against a demo or
staging gateway. Exit code = number of FAIL rows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from conformance import verify as V
from conformance.profile import (MCP_FORBIDDEN, MCP_TOOLS, OPENAPI_PATHS, ORDER_STATUSES, OTAP_ENDPOINTS, OTAP_FIELDS,
                                 PEPPOL_CODES, PROFILE)


class Suite:
    def __init__(self, base: str, customer: str, agent: str, quiet: bool = False):
        self.base, self.customer, self.agent, self.quiet = base.rstrip("/"), customer, agent, quiet
        self.rows: list[dict[str, Any]] = []
        self.h = httpx.Client(base_url=self.base, timeout=120,
                              headers={"X-Customer-Id": customer, "X-Agent-Id": agent})
        self.ctx: dict[str, Any] = {}

    def row(self, cid: str, name: str, ok: Optional[bool], detail: Any = "") -> bool:
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.rows.append({"id": cid, "check": name, "status": status, "detail": str(detail)[:400]})
        if not self.quiet:
            print(f"  {status}  {cid:<4} {name}" + (f"  ({str(detail)[:160]})" if detail and status != "PASS" else ""))
        return bool(ok)

    def guard(self, cid: str, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            self.row(cid, name, False, f"{type(e).__name__}: {e}")
            return None

    def get(self, path: str, **kw) -> httpx.Response:
        return self.h.get(path, **kw)

    def post(self, path: str, headers: Optional[dict] = None, **kw) -> httpx.Response:
        return self.h.post(path, headers=headers, **kw)


# ------------------------------------------------------------------ discovery
def check_discovery(s: Suite) -> None:
    r = s.get("/.well-known/otap.json")
    otap = r.json() if r.status_code == 200 else {}
    s.ctx["otap"] = otap
    s.row("D1", "otap.json served", r.status_code == 200, r.status_code)
    missing = [f for f in OTAP_FIELDS if f not in otap]
    s.row("D2", "otap.json has the profile fields", not missing, f"missing {missing}")
    s.row("D3", f"otap.json protocol is {PROFILE}", otap.get("protocol") == PROFILE, otap.get("protocol"))
    ep = otap.get("endpoints") or {}
    miss_ep = [e for e in OTAP_ENDPOINTS if e not in ep]
    s.row("D4", "otap.json lists the profile endpoints", not miss_ep, f"missing {miss_ep}")
    mcp = otap.get("mcp") or {}
    s.row("D5", "otap.json names a streamable-HTTP MCP endpoint", bool(mcp.get("url")) and
          mcp.get("transport") == "streamable-http", mcp)
    jr = httpx.get(otap.get("jwks_url") or s.base + "/.well-known/jwks.json", timeout=30)
    jwks = jr.json() if jr.status_code == 200 else {}
    s.ctx["jwks"] = jwks
    keys = jwks.get("keys") or []
    okp = [k for k in keys if k.get("kty") == "OKP" and k.get("crv") == "Ed25519" and k.get("x") and k.get("kid")]
    s.row("D6", "JWKS has Ed25519 OKP keys with kid", jr.status_code == 200 and okp and len(okp) == len(keys), keys)
    s.row("D7", "every kid is the RFC 7638 thumbprint", okp and all(V.thumbprint(k) == k["kid"] for k in okp),
          [(k.get("kid"), V.thumbprint(k)) for k in okp][:2])
    s.row("D8", "no private key material in JWKS", all("d" not in k for k in keys))
    orr = httpx.get(otap.get("openapi_url") or s.base + "/openapi.json", timeout=30)
    spec = orr.json() if orr.status_code == 200 else {}
    s.row("D9", "OpenAPI 3.x document served", str(spec.get("openapi", "")).startswith("3."), spec.get("openapi"))
    paths = set((spec.get("paths") or {}).keys())
    s.row("D10", "OpenAPI has the profile paths", not (set(OPENAPI_PATHS) - paths), sorted(set(OPENAPI_PATHS) - paths))


def check_mcp(s: Suite) -> None:
    url = (s.ctx.get("otap", {}).get("mcp") or {}).get("url") or s.base + "/mcp"
    # an otap.json built behind a proxy may advertise another host: keep its path on the base we test
    u = urlparse(url)
    if f"{u.scheme}://{u.netloc}" != s.base and u.path:
        url = s.base + u.path

    async def list_tools() -> list[Any]:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(headers={"X-Customer-Id": s.customer, "X-Agent-Id": s.agent})
        async with http:
            async with Client(streamable_http_client(url, http_client=http)) as c:
                return list((await c.list_tools()).tools)

    try:
        tools = asyncio.run(list_tools())
    except Exception as e:  # noqa: BLE001
        s.row("M1", "MCP tools/list over streamable HTTP", False, f"{type(e).__name__}: {e}")
        return
    by = {t.name: t for t in tools}
    s.ctx["mcp_tools"] = sorted(by)
    s.row("M1", "MCP tools/list over streamable HTTP", bool(tools), len(tools))
    missing = [n for n in MCP_TOOLS if n not in by]
    s.row("M2", "MCP has the profile tools", not missing, f"missing {missing}")
    bad = []
    for name, args in MCP_TOOLS.items():
        t = by.get(name)
        if t is None:
            continue
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        props, req = set((schema.get("properties") or {}).keys()), set(schema.get("required") or [])
        if schema.get("type") != "object" or not set(args) <= props or not set(args) <= req:
            bad.append(f"{name}: props={sorted(props)} required={sorted(req)}")
        if not (t.description or "").strip():
            bad.append(f"{name}: no description")
    s.row("M3", "MCP tool schemas declare the required arguments + descriptions", not bad, bad[:3])
    s.row("M4", "MCP exposes no approval tool (agents can never approve)", not [n for n in by if n in MCP_FORBIDDEN],
          [n for n in by if n in MCP_FORBIDDEN])


# ------------------------------------------------------------------ trade
def pick_product(s: Suite, query: str, day: date, explicit: Optional[str]) -> list[dict]:
    ids: list[str] = [explicit] if explicit else []
    if not ids:
        r = s.get("/v1/products/search", params={"q": query, "limit": 20})
        prods = r.json() if r.status_code == 200 else []
        s.row("T1", f"product search '{query}' returns products with id, name, sales_unit",
              bool(prods) and all(p.get("id") and p.get("name") and "sales_unit" in p for p in prods), r.status_code)
        ids = [p["id"] for p in prods]
    if not ids:
        return []
    r = s.post("/v1/offers", json={"product_ids": ids, "requested_date": day.isoformat(), "customer_id": s.customer})
    offers = r.json() if r.status_code == 200 else []
    s.row("T2", "offers carry price, availability and provenance", bool(offers) and all(
        "unit_price" in o and "available" in o and o.get("provenance") for o in offers), r.status_code)
    return [o for o in offers if o.get("available") and o.get("unit_price") and
            (o.get("available_amount") is None or o["available_amount"] >= 1)]


def make_quote(s: Suite, pid: str, qty: float, day: date) -> httpx.Response:
    return s.post("/v1/quotes", json={"customer_id": s.customer, "delivery_date": day.isoformat(),
                                      "lines": [{"product_id": pid, "qty_sales_units": qty}]})


def check_trade(s: Suite, query: str, day: date, product: Optional[str], approver: Optional[str],
                approver_role: Optional[str]) -> None:
    jwks = s.ctx.get("jwks") or {}
    orderable = pick_product(s, query, day, product)
    if not orderable:
        s.row("T3", "an orderable product was found for the delivery date", False, "none")
        return
    s.row("T3", "an orderable product was found for the delivery date", True)
    o = orderable[0]
    pid = o["product_id"]
    r = make_quote(s, pid, 1, day)
    q = r.json() if r.status_code == 200 else {}
    s.row("Q1", "POST /v1/quotes returns a quote with quote_hash, signature, kid, expires_at",
          r.status_code == 200 and all(q.get(k) for k in ("quote_id", "quote_hash", "signature", "kid", "expires_at")),
          r.text[:200] if r.status_code != 200 else "")
    if not q:
        return
    v = V.verify_quote(q, jwks)
    s.row("Q2", "quote signature verifies against JWKS (independent EdDSA check)", v["signature_valid"], v["reasons"])
    s.row("Q3", "quote_hash binds the quote content (recomputed)", v["hash_matches"], v["reasons"])
    tampered = dict(q, total_net=round(float(q.get("total_net") or 0) + 1, 2))
    s.row("Q4", "a tampered quote no longer verifies", not V.verify_quote(tampered, jwks)["ok"])
    try:
        hdr = json.loads(V.b64d(q["signature"].split(".")[0]))
    except Exception:  # noqa: BLE001
        hdr = {}
    s.row("Q5", "JWS header alg EdDSA, kid matches quote.kid", hdr.get("alg") == "EdDSA" and hdr.get("kid") == q.get("kid"), hdr)
    s.row("Q6", "price lock: expires_at after created_at", str(q.get("expires_at")) > str(q.get("created_at")),
          (q.get("created_at"), q.get("expires_at")))
    g = s.get(f"/v1/quotes/{q['quote_id']}")
    s.row("Q7", "GET /v1/quotes/{id} returns the same signed quote", g.status_code == 200 and
          g.json().get("quote_hash") == q["quote_hash"], g.status_code)

    wrong = s.post("/v1/orders", json={"quote_id": q["quote_id"], "quote_hash": "0" * 64})
    s.row("O1", "order with a wrong quote_hash is refused (409)", wrong.status_code == 409, wrong.status_code)
    intr = s.post("/v1/orders", json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
                  headers={"X-Agent-Id": "conformance-intruder"})
    s.row("O2", "another agent cannot submit this agent's quote (403)", intr.status_code == 403, intr.status_code)
    other = s.post("/v1/orders", json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]},
                   headers={"X-Customer-Id": "conformance-other-customer"})
    s.row("O3", "another customer cannot submit the quote (403)", other.status_code in (403, 404), other.status_code)
    r = s.post("/v1/orders", json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
    order = r.json() if r.status_code == 200 else {}
    s.row("O4", "the quoting agent's order is decided by policy", order.get("status") in ORDER_STATUSES and
          (order.get("decision") or {}).get("outcome") in ("auto_approve", "needs_approval", "reject"), r.text[:200])
    s.row("O5", "the intruder attempt did not consume the quote (order belongs to the quoting agent)",
          order.get("agent_id", "").split("/")[-1] == s.agent.split("/")[-1], order.get("agent_id"))
    s.row("O6", "decision hits are plain-language messages", bool(order.get("decision", {}).get("hits")) and all(
        h.get("message") and h.get("outcome") in ("allow", "needs_approval", "reject") for h in order["decision"]["hits"]))
    again = s.post("/v1/orders", json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
    s.row("O7", "order placement is idempotent per quote", again.status_code == 200 and
          again.json().get("order_id") == order.get("order_id"), again.status_code)
    s.ctx["orders"] = [order] if order else []
    peppol_check(s, order, "P1")

    # escalate to a pending approval: larger quantities, then the next orderable products
    pending, pq = (order, q) if order.get("status") == "pending_approval" else (None, None)
    seen: list[str] = []
    for cand_offer in orderable[:6]:
        if pending:
            break
        cpid, unit, stock = cand_offer["product_id"], float(cand_offer["unit_price"]), cand_offer.get("available_amount")
        qty = 1.0
        for target in (0, 600, 3500, 9000):
            qty = max(qty, math.ceil(target / unit)) if target else 1.0
            if stock is not None and qty > stock:
                qty = float(stock)
            r = make_quote(s, cpid, qty, day)
            if r.status_code != 200:
                seen.append(f"{cpid} x{qty:g}: quote {r.status_code}")
                break
            cand = r.json()
            rr = s.post("/v1/orders", json={"quote_id": cand["quote_id"], "quote_hash": cand["quote_hash"]})
            st = rr.json().get("status") if rr.status_code == 200 else rr.status_code
            seen.append(f"{cpid} x{qty:g}: {st}")
            if rr.status_code == 200:
                s.ctx["orders"].append(rr.json())
                if st == "pending_approval":
                    pending, pq = rr.json(), cand
                    break
                if st == "rejected":   # policy refuses this product outright: try the next one
                    break
    if not pending:
        s.row("A0", "an order reached pending_approval (escalated quantity)", False, "; ".join(seen)[:300])
        return
    s.row("A0", "an order reached pending_approval (escalated quantity)", True)
    role = pending["decision"].get("approver_role")
    inbox = s.get("/v1/approvals", params={"status": "pending"})
    s.row("A1", "pending order is in the approval inbox with its quote", inbox.status_code == 200 and any(
        (x.get("order") or {}).get("order_id") == pending["order_id"] and x.get("quote") for x in inbox.json()))
    dec = f"/v1/approvals/{pending['order_id']}/decision"
    ag = s.post(dec, json={"decision": "approve", "approver": f"agent:{s.customer}/{s.agent}", "quote_hash": pq["quote_hash"],
                           "note": "conformance: agent tries to approve"})
    s.row("A2", "an agent cannot approve (403)", ag.status_code == 403, ag.status_code)
    own = s.post(dec, json={"decision": "approve", "approver": s.agent, "quote_hash": pq["quote_hash"]})
    s.row("A3", "the buyer agent's own id cannot approve (403)", own.status_code == 403, own.status_code)
    who, wrole = approver, approver_role
    if not who:
        who, wrole = find_approver(s, role)
    if not who:
        s.row("A4", "approval bound to the quote hash (wrong hash -> 409)", None, "no approver known (--approver)")
        return
    # human requests: no agent identity, plus the operator's approver credential
    human = {"X-Agent-Id": "", "X-Customer-Id": "", "X-Approver-Pin": os.environ.get("AIMO_APPROVER_PIN", "")}
    nopin = s.post(dec, headers={**human, "X-Approver-Pin": ""},
                   json={"decision": "approve", "approver": who, "approver_role": wrole, "quote_hash": pq["quote_hash"]})
    s.row("A6", "a human without the approver credential cannot approve (401)", nopin.status_code == 401, nopin.status_code)
    if not human["X-Approver-Pin"]:
        s.row("A4", "approval bound to the quote hash (wrong hash -> 409)", None, "set AIMO_APPROVER_PIN to run A4/A5")
        return
    wrongh = s.post(dec, headers=human, json={"decision": "approve", "approver": who, "approver_role": wrole,
                                              "quote_hash": "f" * 64})
    s.row("A4", "approval bound to the quote hash (wrong hash -> 409)", wrongh.status_code == 409, wrongh.status_code)
    ok = s.post(dec, headers=human, json={"decision": "approve", "approver": who, "approver_role": wrole,
                                          "quote_hash": pq["quote_hash"], "note": "conformance suite approval"})
    placed = ok.json() if ok.status_code == 200 else {}
    s.row("A5", "a named human approver with the right role -> placed", placed.get("status") == "placed", ok.text[:200])
    peppol_check(s, placed, "P2")
    s.ctx["orders"].append(placed)
    s.ctx["approved_order"] = placed.get("order_id")


def find_approver(s: Suite, role: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    # approver ids are only shown to a signed-in human approver (the operator running this suite)
    r = s.h.get("/v1/customers", headers={"X-Approver-Pin": os.environ.get("AIMO_APPROVER_PIN", "")})
    if r.status_code != 200:
        return None, None
    for c in r.json():
        if c.get("customer_id") == s.customer:
            for a in c.get("approvers") or []:
                if a.get("role") == role:
                    return a.get("id"), a.get("role")
            for a in c.get("approvers") or []:
                if a.get("role") in (c.get("override_roles") or []):
                    return a.get("id"), a.get("role")
    return None, None


def peppol_check(s: Suite, order: dict, cid: str) -> None:
    if not order or order.get("status") not in ("placed", "rejected") or not order.get("response_code"):
        if order and order.get("status") == "placed":
            s.row(cid, "placed order carries a Peppol response code", False, order.get("response_code"))
        return
    codes = {l.get("code") for l in order.get("response_lines") or []}
    s.row(cid, "Peppol order response: response_code and line codes in AP/CA/RE",
          order["response_code"] in PEPPOL_CODES and codes and codes <= PEPPOL_CODES, (order["response_code"], codes))


# ------------------------------------------------------------------ audit
def check_audit(s: Suite) -> None:
    jwks = s.ctx.get("jwks") or {}
    r = s.get("/v1/audit/verify")
    v = r.json() if r.status_code == 200 else {}
    s.row("U1", "GET /v1/audit/verify reports an intact chain", v.get("ok") is True, v)
    oid = s.ctx.get("approved_order") or next((o.get("order_id") for o in s.ctx.get("orders", []) if o), None)
    if oid:
        evs = s.get("/v1/audit", params={"subject_id": oid}).json()
        s.row("U2", "the order's audit trail has signed, human-readable events", len(evs) >= 3 and all(
            e.get("summary") and e.get("actor") and e.get("signature") for e in evs), len(evs))
        ve = V.verify_events(evs, jwks)
        s.row("U3", "each event hash recomputes and its signature verifies (independent)", ve["ok"], ve["problems"])
        acts = {e.get("actor", "").split(":")[0] for e in evs}
        s.row("U4", "trail records agent and human/system actors", "agent" in acts and len(acts) >= 2, sorted(acts))
    full = s.get("/v1/audit").json()
    vf = V.verify_events(full, jwks, contiguous=True) if len(full) <= 20000 else {"ok": None, "problems": ["too long"]}
    s.row("U5", "the full chain links from genesis (prev_hash) and verifies (independent)", vf["ok"], vf["problems"])


def default_day(base: Optional[str] = None) -> date:
    """Next feasible weekday delivery >= 2 days out on the TARGET's clock (GET /healthz clock.now: AIMO_NOW or
    the replay snapshot's time), else on this machine's clock. Never a hard-coded date."""
    now = None
    if base:
        try:
            v = httpx.get(base.rstrip("/") + "/healthz", timeout=10).json().get("clock", {}).get("now")
            now = datetime.fromisoformat(v) if v else None
        except Exception:
            now = None
    hel = timezone(timedelta(hours=2))
    now = (now or datetime.now(timezone.utc)).astimezone(hel)
    d = now.date() + timedelta(days=2)
    while d.weekday() > 4 or now >= datetime.combine(d - timedelta(days=1), datetime.min.time(), hel) + timedelta(hours=17):
        d += timedelta(days=1)
    return d


def run(base: str, customer: str = "hotel-aurora", agent: str = "buyer-1", query: str = "lactose free milk",
        product: Optional[str] = None, day: Optional[date] = None, approver: Optional[str] = None,
        approver_role: Optional[str] = None, quiet: bool = False) -> dict[str, Any]:
    s = Suite(base, customer, agent, quiet)
    t0 = time.time()
    for name, fn in (("discovery", lambda: check_discovery(s)), ("mcp", lambda: check_mcp(s)),
                     ("trade", lambda: check_trade(s, query, day or default_day(base), product, approver, approver_role)),
                     ("audit", lambda: check_audit(s))):
        if not quiet:
            print(f"== {name}")
        s.guard(name[:1].upper() + "0", f"{name} checks ran", fn)
    fails = sum(1 for r in s.rows if r["status"] == "FAIL")
    return {"profile": PROFILE, "target": base, "name": s.ctx.get("otap", {}).get("name"),
            "customer": customer, "agent": agent, "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_s": round(time.time() - t0, 1), "mcp_tools": s.ctx.get("mcp_tools"),
            "summary": {"pass": sum(1 for r in s.rows if r["status"] == "PASS"), "fail": fails,
                        "skip": sum(1 for r in s.rows if r["status"] == "SKIP"), "total": len(s.rows)},
            "results": s.rows}


def table(rep: dict[str, Any]) -> str:
    out = [f"Trusted Trade conformance ({rep['profile']}) for {rep.get('name')} at {rep['target']}"]
    w = max(len(r["check"]) for r in rep["results"]) if rep["results"] else 10
    for r in rep["results"]:
        out.append(f"{r['status']:<4}  {r['id']:<4} {r['check'].ljust(w)}" +
                   (f"  {r['detail'][:120]}" if r["status"] != "PASS" and r["detail"] else ""))
    sm = rep["summary"]
    out.append(f"{sm['pass']}/{sm['total']} passed, {sm['fail']} failed, {sm['skip']} skipped ({rep['duration_s']} s)")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m conformance", description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="gateway base URL, e.g. http://127.0.0.1:8000")
    ap.add_argument("--customer", default="hotel-aurora")
    ap.add_argument("--agent", default="buyer-1")
    ap.add_argument("--query", default="lactose free milk", help="search term used to find an orderable product")
    ap.add_argument("--product", help="product id to quote (skips search)")
    ap.add_argument("--date", type=date.fromisoformat, help="delivery date (default: next weekday >= 2 days ahead)")
    ap.add_argument("--approver", help="human approver id (default: from GET /v1/customers)")
    ap.add_argument("--approver-role")
    ap.add_argument("--json", metavar="PATH", help="write the JSON report here (default conformance-<host>.json)")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    rep = run(a.base, a.customer, a.agent, a.query, a.product, a.date, a.approver, a.approver_role, a.quiet)
    print(table(rep))
    path = a.json or f"conformance-{urlparse(a.base).netloc.replace(':', '_')}.json"
    with open(path, "w") as f:
        json.dump(rep, f, indent=1)
    print(f"JSON report: {path}")
    return rep["summary"]["fail"]


if __name__ == "__main__":
    sys.exit(main())
