from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

import aimo.backend.catalog_store as cs
import aimo.catalog as cat
from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import clock
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME
from aimo.buyer.profiles import mandate_for
from aimo.buyer import tools as buyer_tools
from aimo.buyer.tools import (
    FakeTransport,
    InProcessTransport,
    ToolError,
    _ensure_replay_ready,
    _has_forced_replay,
    _has_replay,
    connect,
)
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.models import Quote
from aimo.trust import signing

DEAD_MCP = "http://127.0.0.1:9/mcp"
DELIVERY_MON = "2026-11-16"


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    data_dir = tmp_path / "data"
    state_db = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    ledger_dir = tmp_path / "ledger"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.delenv("AIMO_OFFLINE", raising=False)
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_BUYER_TRANSPORT", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_ENV", raising=False)
    monkeypatch.delenv("AIMO_NOW", raising=False)

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(gemini, "LEDGER_DIR", ledger_dir)

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(bc, "_client", None)
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    monkeypatch.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(cs, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()

    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini API must not be called in keyless replay transport tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)

    try:
        yield tmp_path
    finally:
        bc.set_client(None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        con = store._conns.pop(str(state_db), None)
        if con is not None:
            con.close()


def test_file_hygiene_no_comments_or_dashes() -> None:
    for path in (Path(__file__), Path(buyer_tools.__file__)):
        src = path.read_text(encoding="utf-8")
        assert "\x23" not in src
        assert "\u2014" not in src
        assert "\u2013" not in src


def test_keyless_auto_replay_selects_inproc_and_places_order_end_to_end(
    isolated_env: Path,
) -> None:
    assert not config.PRODUCT_JSON.exists()
    assert config.AIMO_API_KEY == ""
    assert config.OFFLINE is False
    assert replay.mode() == "auto"
    assert replay.replay_dir() == replay.BUNDLED_DIR
    assert _has_forced_replay() is True
    assert _has_replay() is True

    mandate = mandate_for("hotel-aurora")
    transport = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate,
    )
    try:
        assert isinstance(transport, InProcessTransport)
        assert transport.kind == "inproc"

        need_payload = {
            "customer_id": "hotel-aurora",
            "product_query": "lactose-free milk",
            "required_claims": ["FREE_FROM_LACTOSE"],
            "excluded_allergens": [],
            "quantity": 20.0,
            "unit": "L",
            "delivery_by": DELIVERY_MON,
        }
        plan = transport.call("plan_need", {"need": need_payload})
        assert isinstance(plan, dict)
        assert len(plan["lines"]) >= 1
        assert plan["shortfall"] == 0
        assert isinstance(bc._client, replay.ReplayClient)
        assert bc._client.dir == replay.BUNDLED_DIR
        assert cat.catalog_source() == "fixture-subset"

        quote_lines = [
            {
                "product_id": ln["product_id"],
                "qty_sales_units": ln["qty_sales_units"],
                "tier": ln["tier"],
            }
            for ln in plan["lines"]
        ]
        quote = transport.call(
            "create_quote",
            {"delivery_date": DELIVERY_MON, "lines": quote_lines, "need": need_payload},
        )
        assert quote["quote_id"].startswith("Q-")
        assert quote["quote_hash"]
        assert quote["signature"]
        assert quote["provenance"]["served_from"] == "replay"

        quote_obj = Quote.model_validate(quote)
        detailed = signing.verify_quote_detailed(quote_obj, now=clock.now())
        assert detailed["ok"] is True
        assert detailed["signature_valid"] is True
        assert detailed["hash_matches"] is True
        assert detailed["expired"] is False

        order = transport.call(
            "place_order",
            {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]},
        )
        assert order["order_id"].startswith("ORD-")
        assert order["quote_id"] == quote["quote_id"]
        assert order["quote_hash"] == quote["quote_hash"]
        assert order["customer_id"] == "hotel-aurora"
        assert order["decision"]["policy_id"]

        if order["status"] == "pending_approval":
            decide_fn = getattr(service, "decide_approval", service.decide)
            decided = decide_fn(
                order["order_id"],
                decision="approve",
                approver="anna@hotel-aurora.example",
                quote_hash=order["quote_hash"],
                note="Approved in keyless auto transport test",
                approver_role="procurement_manager",
            )
            order = decided if isinstance(decided, dict) else decided.model_dump(mode="json")

        assert order["status"] == "placed"
        assert order["response_code"] == "AP"
        assert order["provenance"]["served_from"] == "replay"

        trail = transport.call("get_audit_trail", {"subject_id": order["order_id"]})
        assert trail["verify"]["ok"] is True
        assert trail["verify"]["checked"] >= 3
        assert len(trail["events"]) >= 2
    finally:
        transport.close()


def test_keyless_auto_replay_buyer_agent_places_human_approved_order_on_real_gateway(
    isolated_env: Path,
) -> None:
    assert replay.mode() == "auto"
    transport = connect(
        "auto",
        agent_id="hotel-aurora/buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    stop = threading.Event()
    decide_fn = getattr(service, "decide_approval", service.decide)

    def approve_worker() -> None:
        while not stop.is_set():
            for item in service.list_approvals("pending", "hotel-aurora"):
                pending = item["order"]
                if pending["status"] == "pending_approval":
                    decide_fn(
                        pending["order_id"],
                        decision="approve",
                        approver="anna@hotel-aurora.example",
                        quote_hash=pending["quote_hash"],
                        note="Approved in keyless BuyerAgent auto replay test",
                        approver_role="procurement_manager",
                    )
                    return
            stop.wait(0.03)

    worker = threading.Thread(target=approve_worker, daemon=True)
    worker.start()
    try:
        assert transport.kind == "inproc"
        agent = BuyerAgent(transport, "hotel-aurora", poll_s=0.04, approval_timeout_s=10.0)
        assert agent.mock is True
        assert agent.model == MOCK_MODEL_NAME
        res = agent.run("We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.")
        worker.join(timeout=2.0)

        assert res["status"] == "placed"
        assert res["mock"] is True
        assert res["summary"].startswith(MOCK_LABEL)
        assert res["order"]["approved_by"] == "anna@hotel-aurora.example"
        assert res["quote"]["provenance"]["served_from"] == "replay"
        assert signing.verify_quote(Quote.model_validate(res["quote"])) is True
        assert service.audit_verify()["ok"] is True
    finally:
        stop.set()
        transport.close()


def test_forced_replay_flag_selects_inproc(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_REPLAY", "1")
    assert replay.mode() == "forced"
    assert _has_forced_replay() is True

    t1 = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t1, InProcessTransport)
        assert t1.kind == "inproc"
        plan = t1.call(
            "plan_need",
            {
                "need": {
                    "customer_id": "hotel-aurora",
                    "product_query": "lactose-free milk",
                    "required_claims": ["FREE_FROM_LACTOSE"],
                    "quantity": 100.0,
                    "unit": "L",
                    "delivery_by": DELIVERY_MON,
                }
            },
        )
        assert len(plan["lines"]) >= 1
        assert plan["shortfall"] == 0
    finally:
        t1.close()

    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setattr(config, "OFFLINE", True)
    assert replay.mode() == "forced"
    t2 = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t2, InProcessTransport)
        assert t2.kind == "inproc"
    finally:
        t2.close()


def test_replay_disabled_keyless_falls_back_to_fake_even_after_replay_init(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_REPLAY", "0")
    assert config.AIMO_API_KEY == ""
    assert config.OFFLINE is False
    assert replay.mode() == "off"
    assert _has_forced_replay() is False

    t_initial = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_initial, FakeTransport)
        assert isinstance(t_initial.gw, FakeGateway)
        assert t_initial.kind == "fake"
    finally:
        t_initial.close()

    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    assert replay.mode() == "auto"
    _ensure_replay_ready()
    assert isinstance(bc._client, replay.ReplayClient)
    assert config.PRODUCT_JSON.exists()

    monkeypatch.setenv("AIMO_REPLAY", "0")
    assert replay.mode() == "off"
    t_after = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_after, FakeTransport)
        assert isinstance(t_after.gw, FakeGateway)
        assert t_after.kind == "fake"
    finally:
        t_after.close()


def test_offline_without_replay_and_missing_product_json_falls_back_to_fake(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.delenv("AIMO_REPLAY", raising=False)

    assert not config.PRODUCT_JSON.exists()
    assert replay.mode() == "off"
    assert _has_forced_replay() is False

    t_offline = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_offline, FakeTransport)
        assert isinstance(t_offline.gw, FakeGateway)
        assert t_offline.kind == "fake"
    finally:
        t_offline.close()

    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setattr(config, "OFFLINE", False)
    _ensure_replay_ready()
    assert isinstance(bc._client, replay.ReplayClient)
    assert "catalog-fixture" in config.PRODUCT_JSON.parts

    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setattr(config, "OFFLINE", True)
    assert replay.mode() == "off"
    t_offline_after = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_offline_after, FakeTransport)
        assert isinstance(t_offline_after.gw, FakeGateway)
        assert t_offline_after.kind == "fake"
    finally:
        t_offline_after.close()


def test_catalog_fixture_disabled_or_missing_meta_falls_back_to_fake(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not config.PRODUCT_JSON.exists()
    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    assert replay.mode() == "auto"
    assert _has_forced_replay() is False

    t_no_fixture = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_no_fixture, FakeTransport)
        assert isinstance(t_no_fixture.gw, FakeGateway)
        assert t_no_fixture.kind == "fake"
    finally:
        t_no_fixture.close()

    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    _ensure_replay_ready()
    assert "catalog-fixture" in config.PRODUCT_JSON.parts
    assert config.PRODUCT_JSON.exists()

    monkeypatch.setenv("AIMO_CATALOG_FIXTURE", "0")
    assert _has_forced_replay() is False
    t_no_fixture_after_extract = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_no_fixture_after_extract, FakeTransport)
        assert isinstance(t_no_fixture_after_extract.gw, FakeGateway)
        assert t_no_fixture_after_extract.kind == "fake"
    finally:
        t_no_fixture_after_extract.close()

    real_catalog = isolated_env / "real_product_data.json"
    real_catalog.write_bytes(config.PRODUCT_JSON.read_bytes())
    monkeypatch.setattr(config, "PRODUCT_JSON", real_catalog)
    assert "catalog-fixture" not in config.PRODUCT_JSON.parts
    assert _has_forced_replay() is True
    t_real_catalog_with_fixture_zero = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_real_catalog_with_fixture_zero, InProcessTransport)
        assert t_real_catalog_with_fixture_zero.kind == "inproc"
    finally:
        t_real_catalog_with_fixture_zero.close()

    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)
    empty_dir = isolated_env / "empty_replay"
    empty_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_REPLAY", str(empty_dir))
    assert replay.mode() == "forced"
    assert _has_forced_replay() is False

    t_no_meta = connect(
        "auto",
        agent_id="buyer-1",
        customer_id="hotel-aurora",
        mcp_url=DEAD_MCP,
        mandate=mandate_for("hotel-aurora"),
    )
    try:
        assert isinstance(t_no_meta, FakeTransport)
        assert isinstance(t_no_meta.gw, FakeGateway)
        assert t_no_meta.kind == "fake"
    finally:
        t_no_meta.close()


def test_inproc_fault_guard_ignores_faults_when_demo_faults_unset_or_zero(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = InProcessTransport(
        "buyer-1",
        "hotel-aurora",
        mandate=mandate_for("hotel-aurora"),
        faults="partial:*:400",
    )
    need_1000 = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [],
        "quantity": 1000.0,
        "unit": "L",
        "delivery_by": DELIVERY_MON,
    }
    try:
        for flag in (None, "0", " 0 ", "false"):
            if flag is None:
                monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
            else:
                monkeypatch.setenv("AIMO_DEMO_FAULTS", flag)
            assert clock.request_overrides_allowed(True) is False

            offers = transport.call(
                "get_offers",
                {"product_ids": ["464306"], "requested_date": DELIVERY_MON},
            )
            offer = next(o for o in offers["offers"] if o["product_id"] == "464306")
            assert "injected:partial" not in offer.get("issues", [])
            assert (offer.get("available_amount") or 0.0) > 400.0

            plan = transport.call("plan_need", {"need": need_1000})
            assert plan["shortfall"] == 0
            assert len(plan["lines"]) == 1
            assert plan["lines"][0]["qty_sales_units"] == 1000.0
            assert bc.current_faults().spec == ""
    finally:
        transport.close()


def test_inproc_fault_guard_applies_faults_when_demo_faults_enabled_and_resets_context(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    monkeypatch.delenv("AIMO_ENV", raising=False)
    assert clock.request_overrides_allowed(True) is True

    transport = InProcessTransport(
        "buyer-1",
        "hotel-aurora",
        mandate=mandate_for("hotel-aurora"),
        faults="partial:*:400",
    )
    need_1000 = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [],
        "quantity": 1000.0,
        "unit": "L",
        "delivery_by": DELIVERY_MON,
    }
    try:
        offers = transport.call(
            "get_offers",
            {"product_ids": ["464306"], "requested_date": DELIVERY_MON},
        )
        offer = next(o for o in offers["offers"] if o["product_id"] == "464306")
        assert offer["available_amount"] == 400.0
        assert "injected:partial" in offer["issues"]
        assert bc.current_faults().spec == ""

        plan = transport.call("plan_need", {"need": need_1000})
        assert plan["shortfall"] == 0
        assert len(plan["lines"]) >= 3
        assert all(ln["qty_sales_units"] <= 400.0 for ln in plan["lines"])
        assert bc.current_faults().spec == ""
    finally:
        transport.close()

    seen: dict[str, str] = {}

    class BoomSvc:
        @staticmethod
        def get_offers(product_ids: list[str], requested_date: str) -> dict[str, Any]:
            seen["inside"] = bc.current_faults().spec
            raise RuntimeError("boom inside service")

    spy = InProcessTransport.__new__(InProcessTransport)
    spy.service = BoomSvc
    spy.agent_id = "buyer-1"
    spy.customer_id = "hotel-aurora"
    spy.mandate = None
    spy.faults = "partial:*:400"

    with pytest.raises(RuntimeError, match="boom inside service"):
        spy.call("get_offers", {"product_ids": ["464306"], "requested_date": DELIVERY_MON})
    assert seen["inside"] == "partial:*:400"
    assert bc.current_faults().spec == ""

    with pytest.raises(ToolError):
        spy.call("nonexistent_tool", {})
    assert bc.current_faults().spec == ""


def test_inproc_fault_guard_ignores_faults_in_production_even_with_demo_faults_enabled(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    need_1000 = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "excluded_allergens": [],
        "quantity": 1000.0,
        "unit": "L",
        "delivery_by": DELIVERY_MON,
    }
    for env_val in ("production", "prod", "PRODUCTION"):
        monkeypatch.setenv("AIMO_ENV", env_val)
        assert clock.request_overrides_allowed(True) is False

        t_partial = InProcessTransport(
            "buyer-1",
            "hotel-aurora",
            mandate=mandate_for("hotel-aurora"),
            faults="partial:*:400",
        )
        try:
            offers = t_partial.call(
                "get_offers",
                {"product_ids": ["464306"], "requested_date": DELIVERY_MON},
            )
            offer = next(o for o in offers["offers"] if o["product_id"] == "464306")
            assert "injected:partial" not in offer.get("issues", [])
            assert (offer.get("available_amount") or 0.0) > 400.0

            plan = t_partial.call("plan_need", {"need": need_1000})
            assert plan["shortfall"] == 0
            assert len(plan["lines"]) == 1
            assert plan["lines"][0]["qty_sales_units"] == 1000.0
        finally:
            t_partial.close()

        t_price = InProcessTransport(
            "buyer-1",
            "hotel-aurora",
            mandate=mandate_for("hotel-aurora"),
            faults="price_shock:*:0.01",
        )
        try:
            quote = t_price.call(
                "create_quote",
                {
                    "delivery_date": DELIVERY_MON,
                    "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}],
                    "need": need_1000,
                },
            )
            assert quote["total_net"] >= 500.0
            assert bc.current_faults().spec == ""
        finally:
            t_price.close()
