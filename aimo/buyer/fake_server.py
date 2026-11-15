"""Stand-in gateway for buyer development ONLY (until aimo.gateway lands on main).

Serves the 9 tools from aimo.buyer.fakes over real MCP streamable HTTP at /mcp, the approval
endpoints from docs/interfaces.md (GET /v1/approvals, POST /v1/approvals/{id}/decision) and the
buyer run router (/v1/agent/runs...). This also shows the gateway how to mount the buyer router
next to an MCP app.

    uv run python -m aimo.buyer.fake_server --port 8077 [--scenario stockout] [--customer hotel-aurora]
    uv run python -m aimo.buyer.run_demo --base-url http://localhost:8077 --transport mcp --auto-approve-as anna@aurora
"""
from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request


def build_app(scenario: str = "happy", customer: str = "hotel-aurora"):
    from mcp.server import MCPServer

    from aimo.buyer.fakes import FakeGateway
    from aimo.buyer.profiles import get_profile
    from aimo.buyer.runs import router

    gw = FakeGateway(customer, get_profile(customer)["agent_id"], scenario=scenario)
    srv = MCPServer("valio-aimo-trusted-trade", instructions="FAKE buyer dev gateway (aimo.buyer.fake_server)")

    @srv.tool()
    def search_products(query: str, claims: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Search the Valio Aimo catalog. Returns ProductSummary[]."""
        with gw._lock:
            return gw.call("search_products", {"query": query, "claims": claims, "limit": limit})

    @srv.tool()
    def get_product(product_id: str) -> dict:
        """Full product incl. allergens and (untrusted) marketing text."""
        with gw._lock:
            return gw.call("get_product", {"product_id": product_id})

    @srv.tool()
    def get_offers(product_ids: list[str], requested_date: str) -> list[dict]:
        """Live price and availability for a delivery date (ISO). Returns Offer[]."""
        with gw._lock:
            return gw.call("get_offers", {"product_ids": product_ids, "requested_date": requested_date})

    @srv.tool()
    def plan_need(need: dict) -> dict:
        """Plan a Need: good-better-best ranking and split. Returns Plan."""
        with gw._lock:
            return gw.call("plan_need", {"need": need})

    @srv.tool()
    def create_quote(delivery_date: str, lines: list[dict], need: dict | None = None) -> dict:
        """Create a signed quote from lines [{product_id, qty_sales_units, tier?}]. Returns Quote."""
        with gw._lock:
            return gw.call("create_quote", {"delivery_date": delivery_date, "lines": lines, "need": need})

    @srv.tool()
    def place_order(quote_id: str, quote_hash: str) -> dict:
        """Submit a signed quote; policy decides placed / pending_approval / rejected. Returns Order."""
        with gw._lock:
            return gw.call("place_order", {"quote_id": quote_id, "quote_hash": quote_hash})

    @srv.tool()
    def get_order_status(order_id: str) -> dict:
        """Current Order."""
        with gw._lock:
            return gw.call("get_order_status", {"order_id": order_id})

    @srv.tool()
    def get_audit_trail(subject_id: str) -> list[dict]:
        """Audit events for a subject."""
        with gw._lock:
            return gw.call("get_audit_trail", {"subject_id": subject_id})

    @srv.tool()
    def list_policies() -> dict:
        """Policies that apply to this customer."""
        with gw._lock:
            return gw.call("list_policies", {})

    mcp_app = srv.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title="FAKE buyer dev gateway", lifespan=lifespan)
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "fake": True, "scenario": scenario, "customer": customer}

    @app.get("/v1/audit/verify")
    def audit_verify() -> dict:
        with gw._lock:
            events = len(gw.audit)
        return {"ok": True, "events": events, "_fake": True}

    @app.get("/v1/approvals")
    def approvals(status: str = "pending") -> list[dict]:
        norm = (status or "").strip().lower()
        with gw._lock:
            if norm in ("", "pending", "pending_approval"):
                orders = gw.pending()
            elif norm == "all":
                orders = list(gw.orders.values())
            else:
                orders = [o for o in gw.orders.values() if o["status"] == norm]
            return [{"order": o, "quote": gw.quotes.get(o["quote_id"]), "decision": o["decision"]} for o in orders]

    @app.post("/v1/approvals/{order_id}/decision")
    async def decide(order_id: str, request: Request) -> dict:
        raw = await request.body()
        if not raw.strip():
            raise HTTPException(422, "JSON object body required")
        try:
            body = json.loads(
                raw,
                parse_constant=lambda _c: (_ for _ in ()).throw(ValueError("non-finite JSON constant")),
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(422, "invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(422, "JSON object body required")
        decision = body.get("decision", "approve")
        if not isinstance(decision, str) or decision not in ("approve", "reject"):
            raise HTTPException(422, "decision must be approve or reject")
        raw_approver = body.get("approver")
        if raw_approver is not None and not isinstance(raw_approver, str):
            raise HTTPException(422, "approver must be a string")
        approver = raw_approver.strip() if isinstance(raw_approver, str) and raw_approver.strip() else "human"
        raw_note = body.get("note")
        if raw_note is not None and not isinstance(raw_note, str):
            raise HTTPException(422, "note must be a string")
        note = raw_note if isinstance(raw_note, str) else ""
        raw_qh = body.get("quote_hash")
        if raw_qh is not None and not isinstance(raw_qh, str):
            raise HTTPException(422, "quote_hash must be a string")
        with gw._lock:
            o = gw.orders.get(order_id)
            if not o:
                raise HTTPException(404, "unknown order")
            if o["status"] != "pending_approval":
                raise HTTPException(409, "order is not pending approval")
            if raw_qh and raw_qh != o["quote_hash"]:
                raise HTTPException(409, "approval is bound to a different quote hash")
            return gw.decide(order_id, decision, approver, note)

    @app.get("/v1/audit/report/{subject_id}")
    def report(subject_id: str) -> list[dict]:
        with gw._lock:
            return list(gw.get_audit_trail(subject_id))

    app.mount("/", mcp_app)
    return app


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--scenario", default="happy")
    ap.add_argument("--customer", default="hotel-aurora")
    a = ap.parse_args()
    uvicorn.run(build_app(a.scenario, a.customer), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
