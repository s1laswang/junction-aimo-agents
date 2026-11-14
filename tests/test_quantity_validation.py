"""Quantity 0 / negative must be a clean 4xx (422), never a 500 (bug: /v1/plan with quantity 0
raised ZeroDivisionError in the planner). Offline: uses the recorded fixtures from test_gateway."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.gateway import service

from test_gateway import H, FixtureClient  # noqa: E402  (tests/ is on sys.path under pytest rootdir)

NEED = {"customer_id": "hotel-aurora", "product_query": "lactose-free milk", "required_claims": ["FREE_FROM_LACTOSE"],
        "unit": "L", "delivery_by": "2026-11-16"}
IDENT = {"agent_id": "buyer-1", "customer_id": "hotel-aurora"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    bc.set_client(FixtureClient())
    yield
    bc.set_client(None)


@pytest.fixture
def api():
    from aimo.gateway.app import create_app
    # Uncaught catalog errors (missing product JSON) propagate so tests/conftest.py can skip them.
    return TestClient(create_app(with_mcp=False), headers={"X-Approver-Pin": "test-pin"}, raise_server_exceptions=True)


@pytest.mark.parametrize("qty", [0, -5])
def test_plan_quantity_not_positive_is_422(api, qty):
    r = api.post("/v1/plan", headers=H, json={**NEED, "quantity": qty})
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"][-1] == "quantity"


def test_quote_line_qty_zero_is_422(api):
    r = api.post("/v1/quotes", headers=H, json={"delivery_date": "2026-11-16",
                                                 "lines": [{"product_id": "464306", "qty_sales_units": 0}]})
    assert r.status_code == 422


def test_quote_with_need_quantity_zero_is_422(api):
    r = api.post("/v1/quotes", headers=H, json={"delivery_date": "2026-11-16", "need": {**NEED, "quantity": 0},
                                                 "lines": [{"product_id": "464306", "qty_sales_units": 10}]})
    assert r.status_code == 422


def test_service_and_mcp_plan_need_quantity_zero_is_service_error_422():
    with pytest.raises(service.ServiceError) as ei:
        service.plan_need({**NEED, "quantity": 0}, identity=IDENT)
    assert ei.value.status == 422 and ei.value.code == "invalid_need"

    from mcp import Client
    from aimo.gateway.mcp_server import build_mcp

    async def run():
        async with Client(build_mcp()) as c:
            try:
                res = await c.call_tool("plan_need", {"need": {**NEED, "quantity": 0}})
            except Exception as e:  # noqa: BLE001  some SDK versions raise on isError
                return True, str(e)
            return (bool(getattr(res, "is_error", False) or getattr(res, "isError", False)),
                    " ".join(getattr(x, "text", "") for x in res.content))

    is_error, text = asyncio.run(run())
    assert is_error and ("invalid_need" in text or "greater than 0" in text) and "ZeroDivision" not in text


def test_positive_quantity_still_plans(api):
    r = api.post("/v1/plan", headers=H, json={**NEED, "quantity": 10})
    assert r.status_code == 200, r.text
    assert json.loads(r.text)["need"]["quantity"] == 10
