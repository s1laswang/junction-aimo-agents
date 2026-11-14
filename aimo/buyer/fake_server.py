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
from contextlib import asynccontextmanager


def build_app(scenario: str = "happy", customer: str = "hotel-aurora"):
    from fastapi import FastAPI, HTTPException
    from mcp.server import MCPServer

    from aimo.buyer.fakes import FakeGateway
    from aimo.buyer.profiles import get_profile
    from aimo.buyer.runs import router

    gw = FakeGateway(customer, get_profile(customer)["agent_id"], scenario=scenario)
    srv = MCPServer("valio-aimo-trusted-trade", instructions="FAKE buyer dev gateway (aimo.buyer.fake_server)")

    @srv.tool()
    def search_products(query: str, claims: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Search the Valio Aimo catalog. Returns ProductSummary[]."""
        return gw.call("search_products", {"query": query, "claims": claims, "limit": limit})

    @srv.tool()
    def get_product(product_id: str) -> dict:
        """Full product incl. allergens and (untrusted) marketing text."""
        return gw.call("get_product", {"product_id": product_id})

    @srv.tool()
    def get_offers(product_ids: list[str], requested_date: str) -> list[dict]:
        """Live price and availability for a delivery date (ISO). Returns Offer[]."""
        return gw.call("get_offers", {"product_ids": product_ids, "requested_date": requested_date})

    @srv.tool()
    def plan_need(need: dict) -> dict:
        """Plan a Need: good-better-best ranking and split. Returns Plan."""
        return gw.call("plan_need", {"need": need})

    @srv.tool()
    def create_quote(delivery_date: str, lines: list[dict], need: dict | None = None) -> dict:
        """Create a signed quote from lines [{product_id, qty_sales_units, tier?}]. Returns Quote."""
        return gw.call("create_quote", {"delivery_date": delivery_date, "lines": lines, "need": need})

    @srv.tool()
    def place_order(quote_id: str, quote_hash: str) -> dict:
        """Submit a signed quote; policy decides placed / pending_approval / rejected. Returns Order."""
        return gw.call("place_order", {"quote_id": quote_id, "quote_hash": quote_hash})

    @srv.tool()
    def get_order_status(order_id: str) -> dict:
        """Current Order."""
        return gw.call("get_order_status", {"order_id": order_id})

    @srv.tool()
    def get_audit_trail(subject_id: str) -> list[dict]:
        """Audit events for a subject."""
        return gw.call("get_audit_trail", {"subject_id": subject_id})

    @srv.tool()
    def list_policies() -> dict:
        """Policies that apply to this customer."""
        return gw.call("list_policies", {})

    mcp_app = srv.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title="FAKE buyer dev gateway", lifespan=lifespan)
    app.include_router(router)

    @app.get("/v1/approvals")
    def approvals(status: str = "pending") -> list[dict]:
        return [{"order": o, "quote": gw.quotes.get(o["quote_id"]), "decision": o["decision"]} for o in gw.pending()]

    @app.post("/v1/approvals/{order_id}/decision")
    def decide(order_id: str, body: dict) -> dict:
        o = gw.orders.get(order_id)
        if not o:
            raise HTTPException(404, "unknown order")
        if body.get("quote_hash") and body["quote_hash"] != o["quote_hash"]:
            raise HTTPException(409, "approval is bound to a different quote hash")
        return gw.decide(order_id, body.get("decision", "approve"), body.get("approver", "human"), body.get("note", ""))

    @app.get("/v1/audit/report/{subject_id}")
    def report(subject_id: str) -> list[dict]:
        return gw.get_audit_trail(subject_id)

    app.mount("/", mcp_app)  # serves /mcp
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
