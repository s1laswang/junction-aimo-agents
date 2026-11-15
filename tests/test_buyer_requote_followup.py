from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
from typing import Any, Iterator
import xml.etree.ElementTree as ET

from google.genai import types
import pytest

from aimo import config, gemini
import aimo.backend.catalog_store as cs
from aimo.backend import client as bc
from aimo.backend import clock
from aimo.backend import orders as store
from aimo.backend.peppol import NS_CBC, check_t116_rules, order_response_xml
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.mock_model import MOCK_MODEL_NAME, MockModel
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import mandate_for
from aimo.buyer.tool_schemas import TOOL_SCHEMAS
from aimo.buyer.tools import InProcessTransport, ToolSpec
import aimo.catalog as cat
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.models import AgentStep
from aimo.trust import audit, signing

ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = ROOT / "tests" / "fixtures" / "snapshot" / "replay"
REPLAY_META = REPLAY_DIR / "meta.json"
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
HERO = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
APPROVER = "anna@hotel-aurora.example"
PRICE_SHOCK = "price_shock:464306:1.25"


def _fc(name: str, args: dict[str, Any]) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                )
            )
        ]
    )


def _txt(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))]
    )


def _last_fn_response(contents: list[Any], name: str) -> dict[str, Any] | None:
    for item in reversed(contents):
        for part in getattr(item, "parts", None) or []:
            fr = getattr(part, "function_response", None)
            if fr and fr.name == name:
                return fr.response
    return None


def _order_dict(
    order_id: str,
    quote_id: str,
    quote_hash: str,
    status: str,
    *,
    approved_by: str | None = None,
    response_code: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "order_id": order_id,
        "quote_id": quote_id,
        "quote_hash": quote_hash,
        "customer_id": "hotel-aurora",
        "agent_id": "buyer-1",
        "status": status,
        "decision": {
            "outcome": "needs_approval",
            "approver_role": "procurement_manager",
            "hits": [
                {
                    "rule_id": "spend.approval",
                    "outcome": "needs_approval",
                    "message": "Total EUR 1010.00 exceeds EUR 500 auto-approve limit.",
                }
            ],
            "policy_id": "hotel-aurora",
            "policy_version": "1",
        },
        "approved_by": approved_by,
        "response_code": response_code,
        "response_lines": [],
        "created_at": "2026-11-14T13:00:00+00:00",
        "updated_at": "2026-11-14T13:05:00+00:00",
    }
    if provenance is not None:
        out["provenance"] = provenance
    return out


class StubRequoteTransport:
    kind = "stub"

    def __init__(self, status_sequences: dict[str, list[dict[str, Any]]], initial_order_id: str = "O-1") -> None:
        self.sequences = {oid: [dict(x) for x in seq] for oid, seq in status_sequences.items()}
        self.initial_order_id = initial_order_id
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(t["name"], t["description"], t["input_schema"]) for t in TOOL_SCHEMAS]

    def call(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        if name == "list_policies":
            return {"policies": [{"customer_id": "hotel-aurora", "policy_id": "hotel-aurora"}], "learned_swaps": []}
        if name == "search_products":
            return {
                "products": [
                    {
                        "id": "464306",
                        "name": "Valio Eila 1 l lactose-free semi-skimmed milk drink",
                        "brand": "Valio Eila",
                        "sales_unit": "ST",
                        "content_amount": 1.0,
                        "content_unit": "L",
                        "claims": ["FREE_FROM_LACTOSE"],
                        "delivery_reliability": 0.99,
                    }
                ]
            }
        if name == "get_offers":
            return {
                "offers": [
                    {
                        "product_id": "464306",
                        "requested_date": args["requested_date"],
                        "available": True,
                        "available_amount": 2500.0,
                        "cutoff_passed": False,
                        "logistic_model": "Stock",
                        "sales_unit": "ST",
                        "unit_price": 1.01,
                        "comparison_price": 1.01,
                        "comparison_unit": "L",
                        "fetched_at": "2026-11-14T13:00:00+00:00",
                        "issues": [],
                    }
                ]
            }
        if name == "plan_need":
            need = dict(args["need"])
            return {
                "need": need,
                "lines": [
                    {
                        "product_id": "464306",
                        "product_name": "Valio Eila 1 l lactose-free semi-skimmed milk drink",
                        "tier": "best",
                        "qty_sales_units": 1000.0,
                        "sales_unit": "ST",
                        "covered_amount": 1000.0,
                        "unit_price": 1.01,
                        "line_total": 1010.0,
                        "reasons": ["Usual 20 L dispenser cut-off passed; 1 L Stock carton meets claim."],
                        "flags": [],
                    }
                ],
                "candidates": [],
                "covered_amount": 1000.0,
                "shortfall": 0.0,
                "total_net": 1010.0,
                "explanation": "Covered 1,000 L with Valio Eila 1 l.",
            }
        if name == "create_quote":
            return {
                "quote_id": "Q-1",
                "quote_hash": "hash-q1",
                "customer_id": "hotel-aurora",
                "agent_id": "buyer-1",
                "delivery_date": args["delivery_date"],
                "lines": [
                    {
                        "product_id": "464306",
                        "product_name": "Valio Eila 1 l lactose-free semi-skimmed milk drink",
                        "qty_sales_units": 1000.0,
                        "sales_unit": "ST",
                        "unit_price": 1.01,
                        "line_total": 1010.0,
                        "tier": "best",
                        "flags": [],
                    }
                ],
                "total_net": 1010.0,
                "total_gross": 1151.4,
                "created_at": "2026-11-14T13:00:00+00:00",
                "expires_at": "2026-11-14T13:15:00+00:00",
            }
        if name == "place_order":
            return _order_dict(self.initial_order_id, args["quote_id"], args["quote_hash"], "pending_approval")
        if name == "get_order_status":
            oid = args["order_id"]
            seq = self.sequences[oid]
            return seq.pop(0) if len(seq) > 1 else dict(seq[0])
        raise AssertionError(f"Unexpected tool call: {name}")

    def close(self) -> None:
        pass


class StubGeminiGenerate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.summary_prompts: list[str] = []

    def __call__(self, feature: str, model: str, contents: Any, config: Any = None) -> types.GenerateContentResponse:
        self.calls.append((feature, model))
        if feature == "buyer.explain":
            return _txt("Usual 20 L dispenser cut-off passed; selected 1 L lactose-free Stock cartons.")
        if feature == "buyer.summary":
            self.summary_prompts.append(str(contents))
            return _txt("Order placed after re-quote approval.")
        done = [
            part.function_response.name
            for item in contents
            for part in (getattr(item, "parts", None) or [])
            if getattr(part, "function_response", None)
        ]
        if "search_products" not in done:
            return _fc("search_products", {"query": "lactose-free milk", "claims": ["FREE_FROM_LACTOSE"]})
        if "get_offers" not in done:
            return _fc("get_offers", {"product_ids": ["464306"], "requested_date": "2026-11-16"})
        if "plan_need" not in done:
            return _fc(
                "plan_need",
                {
                    "need": {
                        "customer_id": "hotel-aurora",
                        "product_query": "lactose-free milk",
                        "required_claims": ["FREE_FROM_LACTOSE"],
                        "quantity": 1000,
                        "unit": "L",
                        "delivery_by": "2026-11-16",
                    }
                },
            )
        if "create_quote" not in done:
            plan = _last_fn_response(contents, "plan_need")
            assert plan is not None
            lines = [
                {"product_id": ln["product_id"], "qty_sales_units": ln["qty_sales_units"], "tier": ln["tier"]}
                for ln in plan["lines"]
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if "place_order" not in done:
            quote = _last_fn_response(contents, "create_quote")
            assert quote is not None
            return _fc("place_order", {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]})
        return _txt("Completed order.")


@pytest.fixture
def keyless_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini must not be called in keyless mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)


@pytest.fixture
def isolated_replay_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    if not REPLAY_META.is_file():
        pytest.skip("S1 replay snapshot meta.json is not present")

    data_dir = tmp_path / "data"
    state_db = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    data_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.setenv("AIMO_REPLAY", str(REPLAY_DIR))
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.delenv("AIMO_NOW_FROZEN", raising=False)
    monkeypatch.delenv("AIMO_CATALOG_FIXTURE", raising=False)

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

    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(bc, "_client", None)
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    monkeypatch.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(cs, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    audit._ready.discard(str(state_db))

    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini must not be called in keyless replay mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)

    try:
        yield tmp_path
    finally:
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        bc.set_client(None)
        con = store._conns.pop(str(state_db), None)
        if con is not None:
            con.close()


def test_scripted_mock_model_follows_requoted_order_to_placed(keyless_env: None) -> None:
    transport = StubRequoteTransport(
        {
            "O-1": [
                _order_dict("O-1", "Q-1", "hash-q1", "pending_approval"),
                _order_dict(
                    "O-1",
                    "Q-1",
                    "hash-q1",
                    "expired",
                    provenance={
                        "requote_reason": "quote_expired",
                        "requoted_as": "O-2",
                        "requote_quote_id": "Q-2",
                    },
                ),
            ],
            "O-2": [
                _order_dict("O-2", "Q-2", "hash-q2", "pending_approval"),
                _order_dict("O-2", "Q-2", "hash-q2", "approved", approved_by=APPROVER),
                _order_dict("O-2", "Q-2", "hash-q2", "placed", approved_by=APPROVER, response_code="AP"),
            ],
        }
    )
    agent = BuyerAgent(
        transport,
        "hotel-aurora",
        poll_s=0.01,
        approval_timeout_s=5.0,
        now=SAT,
        audit_base_url="http://localhost:8000",
    )
    agent._approved_polls = 14
    assert agent.mock is True
    assert agent.model == MOCK_MODEL_NAME
    assert isinstance(agent._generate, MockModel)

    res = agent.run(HERO)

    assert res["mock"] is True
    assert res["status"] == "placed"
    assert res["order"]["order_id"] == "O-2"
    assert res["order"]["status"] == "placed"
    assert res["order"]["approved_by"] == APPROVER
    assert res["order"]["response_code"] == "AP"
    assert res["quote"]["quote_id"] == "Q-2"
    assert res["quote"]["quote_hash"] == "hash-q2"
    assert agent.blocked is False
    assert agent._approved_polls == 1
    assert agent._audit_link() == "http://localhost:8000/v1/audit/report/O-2"

    polled = [args["order_id"] for name, args in transport.calls if name == "get_order_status"]
    assert polled == ["O-1", "O-1", "O-2", "O-2", "O-2"]

    waits = [s for s in res["steps"] if s["kind"] == "approval_wait"]
    assert len(waits) == 2
    assert waits[0]["data"]["order_id"] == "O-1"
    assert "O-1 -> O-2" in waits[1]["title"]
    assert waits[1]["data"]["old_order_id"] == "O-1"
    assert waits[1]["data"]["order_id"] == "O-2"
    assert waits[1]["data"]["requote_reason"] == "quote_expired"
    assert waits[1]["data"]["quote_id"] == "Q-2"

    final_steps = [s for s in res["steps"] if s["kind"] == "final"]
    assert len(final_steps) == 1
    assert final_steps[0]["data"]["audit_report"] == "http://localhost:8000/v1/audit/report/O-2"


def test_scripted_mock_model_cycle_guard_prevents_infinite_requote_loop(keyless_env: None) -> None:
    two_cycle = StubRequoteTransport(
        {
            "O-1": [
                _order_dict(
                    "O-1",
                    "Q-1",
                    "hash-q1",
                    "expired",
                    provenance={"requote_reason": "quote_expired", "requoted_as": "O-2", "requote_quote_id": "Q-2"},
                )
            ],
            "O-2": [
                _order_dict(
                    "O-2",
                    "Q-2",
                    "hash-q2",
                    "expired",
                    provenance={"requote_reason": "quote_expired", "requoted_as": "O-1", "requote_quote_id": "Q-1"},
                )
            ],
        }
    )
    agent = BuyerAgent(two_cycle, "hotel-aurora", poll_s=0.01, approval_timeout_s=5.0, now=SAT)
    res = agent.run(HERO)

    polled = [args["order_id"] for name, args in two_cycle.calls if name == "get_order_status"]
    assert polled == ["O-1", "O-2"]
    assert res["status"] == "expired"
    assert res["order"]["order_id"] == "O-2"
    assert res["order"].get("timed_out") is not True
    assert agent.blocked is True

    self_cycle = StubRequoteTransport(
        {
            "O-1": [
                _order_dict(
                    "O-1",
                    "Q-1",
                    "hash-q1",
                    "expired",
                    provenance={"requote_reason": "quote_expired", "requoted_as": "O-1"},
                )
            ]
        }
    )
    agent_self = BuyerAgent(self_cycle, "hotel-aurora", poll_s=0.01, approval_timeout_s=5.0, now=SAT)
    res_self = agent_self.run(HERO)

    polled_self = [args["order_id"] for name, args in self_cycle.calls if name == "get_order_status"]
    assert polled_self == ["O-1"]
    assert res_self["status"] == "expired"
    assert res_self["order"]["order_id"] == "O-1"
    assert agent_self.blocked is True


@pytest.mark.parametrize(
    "provenance",
    [
        {"requote_reason": "line_unavailable", "requoted_as": None, "requote_quote_id": None},
        {},
        None,
    ],
)
def test_expired_order_without_requoted_as_ends_expired_and_blocked(
    keyless_env: None,
    provenance: dict[str, Any] | None,
) -> None:
    transport = StubRequoteTransport(
        {
            "O-1": [
                _order_dict("O-1", "Q-1", "hash-q1", "pending_approval"),
                _order_dict("O-1", "Q-1", "hash-q1", "expired", provenance=provenance),
            ]
        }
    )
    agent = BuyerAgent(transport, "hotel-aurora", poll_s=0.01, approval_timeout_s=5.0, now=SAT)
    res = agent.run(HERO)

    assert res["status"] == "expired"
    assert res["order"]["order_id"] == "O-1"
    assert res["order"]["status"] == "expired"
    assert agent.blocked is True
    assert "__blocked__" in agent._guard_args("place_order", {"quote_id": "Q-1", "quote_hash": "hash-q1"})
    waits = [s for s in res["steps"] if s["kind"] == "approval_wait"]
    assert len(waits) == 1


def test_real_replay_gateway_inproc_e2e_requote_followup(isolated_replay_env: Path) -> None:
    first_wait = threading.Event()
    handoff_wait = threading.Event()
    captured: dict[str, Any] = {}
    worker_errors: list[BaseException] = []
    decide_fn = getattr(service, "decide_approval", service.decide)

    def on_step(step: AgentStep) -> None:
        if step.kind == "approval_wait":
            if step.data.get("old_order_id"):
                captured["handoff_step"] = step.model_dump(mode="json")
                handoff_wait.set()
            else:
                captured["initial_order_id"] = step.data.get("order_id")
                first_wait.set()

    def human_approver() -> None:
        try:
            assert first_wait.wait(timeout=10.0), "BuyerAgent never entered initial approval_wait"
            old_oid = str(captured["initial_order_id"])
            old_order = service.get_order(old_oid)
            old_quote = service.get_quote(old_order.quote_id)
            expired_now = old_quote.created_at + timedelta(minutes=20)

            tok_now = clock.set_request_now(expired_now.isoformat())
            tok_faults = bc.set_request_faults(PRICE_SHOCK)
            try:
                with pytest.raises(service.ServiceError) as exc_info:
                    decide_fn(
                        old_oid,
                        decision="approve",
                        approver=APPROVER,
                        quote_hash=old_order.quote_hash,
                        note="Approve attempt after 20m price-lock expiry",
                        approver_role="procurement_manager",
                    )
                err = exc_info.value
                assert err.status == 409
                assert err.code == "requote_needed"
                detail = err.detail
                assert isinstance(detail, dict)
                captured["requote_detail"] = detail

                new_oid = str(detail["new_order"]["order_id"])
                new_qhash = str(detail["new_quote"]["quote_hash"])
                assert handoff_wait.wait(timeout=10.0), "BuyerAgent never followed requoted_as to new_order_id"

                placed = decide_fn(
                    new_oid,
                    decision="approve",
                    approver=APPROVER,
                    quote_hash=new_qhash,
                    note="Approved replacement quote in one step",
                    approver_role="procurement_manager",
                )
                captured["placed_order"] = placed.model_dump(mode="json")
            finally:
                bc.reset_request_faults(tok_faults)
                clock.reset_request_now(tok_now)
        except BaseException as exc:
            worker_errors.append(exc)
            first_wait.set()
            handoff_wait.set()

    transport = InProcessTransport("buyer-1", "hotel-aurora", mandate=mandate_for("hotel-aurora"))
    worker = threading.Thread(target=human_approver, daemon=True)
    worker.start()
    try:
        agent = BuyerAgent(
            transport,
            "hotel-aurora",
            poll_s=0.02,
            approval_timeout_s=15.0,
            now=SAT,
            emit=on_step,
            audit_base_url="http://127.0.0.1:8000",
        )
        res = agent.run(HERO)
        worker.join(timeout=10.0)
        assert not worker_errors, worker_errors
    finally:
        transport.close()

    detail = captured["requote_detail"]
    old_order_id = str(captured["initial_order_id"])
    new_order_id = str(detail["new_order"]["order_id"])
    new_quote_id = str(detail["new_quote"]["quote_id"])
    new_quote_hash = str(detail["new_quote"]["quote_hash"])

    assert old_order_id != new_order_id
    assert float(detail["changes"]["old_total_net"]) == pytest.approx(1010.00)
    assert float(detail["changes"]["new_total_net"]) == pytest.approx(1262.50)

    assert res["mock"] is True
    assert res["status"] == "placed"
    assert agent.blocked is False
    assert res["order"]["order_id"] == new_order_id
    assert res["order"]["status"] == "placed"
    assert res["order"]["approved_by"] == APPROVER
    assert res["order"]["response_code"] == "AP"
    assert res["order"]["quote_id"] == new_quote_id
    assert res["order"]["quote_hash"] == new_quote_hash
    assert res["quote"]["quote_id"] == new_quote_id
    assert res["quote"]["quote_hash"] == new_quote_hash
    assert agent._audit_link() == f"http://127.0.0.1:8000/v1/audit/report/{new_order_id}"

    handoff_step = captured["handoff_step"]
    assert f"{old_order_id} -> {new_order_id}" in handoff_step["title"]
    assert handoff_step["data"]["old_order_id"] == old_order_id
    assert handoff_step["data"]["order_id"] == new_order_id

    stored_old = service.get_order(old_order_id)
    assert stored_old.status == "expired"
    assert (stored_old.provenance or {}).get("requoted_as") == new_order_id

    stored_new = service.get_order(new_order_id)
    stored_new_quote = service.get_quote(new_quote_id)
    assert stored_new.status == "placed"
    assert stored_new.response_code == "AP"
    sig_check = signing.verify_quote_detailed(
        stored_new_quote,
        jwks_=service.jwks(),
        now=stored_new_quote.created_at,
    )
    assert sig_check["ok"] is True
    assert sig_check["signature_valid"] is True

    xml_text = order_response_xml(stored_new, stored_new_quote)
    assert check_t116_rules(xml_text) == []
    xml_root = ET.fromstring(xml_text.encode("utf-8"))
    peppol_code = xml_root.findtext("cbc:OrderResponseCode", None, {"cbc": NS_CBC})
    assert peppol_code is not None and peppol_code.strip() == "AP"

    old_events = service.audit_events(old_order_id)
    assert any(ev.get("action") == "order.requoted" for ev in old_events)
    audit_result = audit.verify()
    assert audit_result["ok"] is True
    assert audit_result.get("broken_at") is None
    assert service.audit_verify()["ok"] is True


def test_gemini_main_path_generate_fn_follows_requoted_order_identically() -> None:
    transport = StubRequoteTransport(
        {
            "O-1": [
                _order_dict("O-1", "Q-1", "hash-q1", "pending_approval"),
                _order_dict(
                    "O-1",
                    "Q-1",
                    "hash-q1",
                    "expired",
                    provenance={
                        "requote_reason": "quote_expired",
                        "requoted_as": "O-2",
                        "requote_quote_id": "Q-2",
                    },
                ),
            ],
            "O-2": [
                _order_dict("O-2", "Q-2", "hash-q2", "pending_approval"),
                _order_dict("O-2", "Q-2", "hash-q2", "placed", approved_by=APPROVER, response_code="AP"),
            ],
        }
    )
    gen = StubGeminiGenerate()
    agent = BuyerAgent(
        transport,
        "hotel-aurora",
        generate_fn=gen,
        use_gemini_parse=False,
        poll_s=0.01,
        approval_timeout_s=5.0,
        now=SAT,
        audit_base_url="http://localhost:8000",
    )
    assert agent.mock is False
    assert agent.escalate is True
    assert agent.model == config.GEMINI_AGENT_MODEL
    assert agent.reasoning_model == config.GEMINI_REASONING_MODEL

    res = agent.run(HERO)

    assert res["mock"] is False
    assert res["status"] == "placed"
    assert res["order"]["order_id"] == "O-2"
    assert res["order"]["status"] == "placed"
    assert res["order"]["approved_by"] == APPROVER
    assert res["order"]["response_code"] == "AP"
    assert res["quote"]["quote_id"] == "Q-2"
    assert res["quote"]["quote_hash"] == "hash-q2"
    assert agent.blocked is False
    assert agent.escalated is True
    assert agent._audit_link() == "http://localhost:8000/v1/audit/report/O-2"

    features = [feat for feat, _ in gen.calls]
    models_seen = {mdl for _, mdl in gen.calls}
    assert "buyer.agent_step" in features
    assert "buyer.explain" in features
    assert "buyer.summary" in features
    assert config.GEMINI_AGENT_MODEL in models_seen
    assert config.GEMINI_REASONING_MODEL in models_seen

    assert gen.summary_prompts
    assert "/v1/audit/report/O-2" in gen.summary_prompts[-1]
    assert json.dumps("Q-2") in gen.summary_prompts[-1]

    waits = [s for s in res["steps"] if s["kind"] == "approval_wait"]
    assert len(waits) == 2
    assert "O-1 -> O-2" in waits[1]["title"]
    assert waits[1]["data"]["old_order_id"] == "O-1"
    assert waits[1]["data"]["order_id"] == "O-2"


def test_multi_hop_requote_and_timeout_syncs_replacement_order_metadata(keyless_env: None) -> None:
    multi_hop = StubRequoteTransport(
        {
            "O-1": [
                _order_dict(
                    "O-1",
                    "Q-1",
                    "hash-q1",
                    "expired",
                    provenance={"requote_reason": "quote_expired", "requoted_as": "O-2", "requote_quote_id": "Q-2"},
                )
            ],
            "O-2": [
                _order_dict(
                    "O-2",
                    "Q-2",
                    "hash-q2",
                    "expired",
                    provenance={"requote_reason": "cutoff_passed", "requoted_as": "O-3", "requote_quote_id": "Q-3"},
                )
            ],
            "O-3": [
                _order_dict("O-3", "Q-3", "hash-q3", "pending_approval"),
                _order_dict("O-3", "Q-3", "hash-q3", "placed", approved_by=APPROVER, response_code="AP"),
            ],
        }
    )
    agent = BuyerAgent(multi_hop, "hotel-aurora", poll_s=0.01, approval_timeout_s=5.0, now=SAT)
    res = agent.run(HERO)
    assert res["status"] == "placed"
    assert res["order"]["order_id"] == "O-3"
    assert res["quote"]["quote_id"] == "Q-3"
    assert res["quote"]["quote_hash"] == "hash-q3"
    assert agent.blocked is False
    waits = [s for s in res["steps"] if s["kind"] == "approval_wait"]
    assert len(waits) == 3
    assert "O-1 -> O-2" in waits[1]["title"]
    assert "O-2 -> O-3" in waits[2]["title"]

    timeout_transport = StubRequoteTransport(
        {
            "O-1": [
                _order_dict(
                    "O-1",
                    "Q-1",
                    "hash-q1",
                    "expired",
                    provenance={"requote_reason": "quote_expired", "requoted_as": "O-2", "requote_quote_id": "Q-2"},
                )
            ],
            "O-2": [
                _order_dict("O-2", "Q-2", "hash-q2", "pending_approval"),
            ],
        }
    )
    agent_timeout = BuyerAgent(timeout_transport, "hotel-aurora", poll_s=0.02, approval_timeout_s=0.07, now=SAT)
    res_timeout = agent_timeout.run(HERO)
    assert res_timeout["status"] == "pending_approval"
    assert res_timeout["order"]["order_id"] == "O-2"
    assert res_timeout["order"]["timed_out"] is True
    assert res_timeout["quote"]["quote_id"] == "Q-2"
    assert res_timeout["quote"]["quote_hash"] == "hash-q2"


def test_file_hygiene_no_comments_or_dashes() -> None:
    text = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in text
    assert "\u2014" not in text
    assert "\u2013" not in text
