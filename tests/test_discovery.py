from fastapi.testclient import TestClient

from aimo.app import app


def test_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    c = TestClient(app)
    card = c.get("/.well-known/agent-card.json").json()
    assert card["endpoints"]["mcp"] == "/mcp"
    assert {"place_order", "get_audit_trail", "get_product"} <= {t["name"] for t in card["tools"]}
    assert card["auth"]["agent"]["name"] == "X-Agent-Key"
    assert len(card["policies"]["mandates"]) == 3
    k = c.get("/.well-known/jwks.json").json()["keys"][0]
    assert k["alg"] == "EdDSA" and k["kid"] and "d" not in k
