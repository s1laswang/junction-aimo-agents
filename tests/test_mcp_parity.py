import asyncio
import pytest

TOOLS = {"search_products", "parse_request", "get_offers", "request_quote", "place_order", "get_product",
         "get_order", "list_approvals", "decide_approval", "get_audit_trail", "get_mandate"}


def test_tool_parity(client):
    srv = pytest.importorskip("aimo.mcp_server")
    mcp = getattr(srv, "mcp", None) or getattr(srv, "server", None)
    if mcp is None:
        pytest.skip("no mcp object")
    listed = asyncio.run(mcp.list_tools())
    names = {t.name for t in listed}
    card = client.get("/.well-known/agent-card.json").json()
    card_names = {t["name"] if isinstance(t, dict) else t for t in card["tools"]}
    assert names == card_names == TOOLS
