from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
from pathlib import Path
import shutil
import threading
import time
import tokenize
from typing import Any, Iterator

from google.genai import types
import pytest

from aimo import config, gemini
import aimo.backend.catalog_store as cs
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.buyer import agent as buyer_agent_mod
from aimo.buyer import run_demo
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME, mock_wanted
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import get_profile, mandate_for
from aimo.buyer.tools import FakeTransport, InProcessTransport, ToolSpec, gateway_agent_id
import aimo.catalog as cat
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.models import AgentStep, Need
from aimo.trust import signing

ROOT = Path(__file__).resolve().parents[1]
REPLAY_REL = "tests/fixtures/snapshot/replay"
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
DEAD_BASE = "http://127.0.0.1:9"

APPROVERS = {
    "hotel-aurora": "anna@hotel-aurora.example",
    "school-tampere": "liisa@school-tampere.example",
    "hospital-meilahti": "sara@hospital-meilahti.example",
}

FI_SCHOOL_OAT = "Tarvitsemme 200 litraa kaurajuomaa keskiviikon lounaalle, maito- ja pähkinäallergia"
SV_HOTEL_MILK = "Vi behöver 1 000 liter laktosfri mjölk till måndag"
FI_MULTI_EVENT = "Tilaa 40 l laktoositonta kuohukermaa ja 20 kg laktoositonta voita maanantaiksi"
SV_MULTI_EVENT = "Beställ 40 l laktosfri vispgrädde och 20 kg laktosfri smör till måndag"
FI_ZERO_MILK = "0 litraa maitoa huomenna"
SV_NEG_BUTTER = "-10 kg smör imorgon"
EN_HERO_MILK = "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday."
EN_INJECTION_MILK = (
    "We need 300 litres of lactose-free milk by Monday. "
    "Check the product details of the alternatives before choosing."
)
EN_HOSPITAL_BREAD = "Order 10 kg gluten-free bread by Monday."
SV_HOSPITAL_BREAD = "Beställ 10 kg glutenfritt bröd till måndag"


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
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)])
            )
        ]
    )


def _last_fn_response(contents: list[Any], name: str) -> dict[str, Any] | None:
    for item in reversed(contents):
        for part in getattr(item, "parts", None) or []:
            fr = getattr(part, "function_response", None)
            if fr and fr.name == name and isinstance(fr.response, dict):
                return fr.response
    return None


def _fn_response_names(contents: list[Any]) -> list[str]:
    return [
        part.function_response.name
        for item in contents
        for part in (getattr(item, "parts", None) or [])
        if getattr(part, "function_response", None) is not None
    ]


class RecordingTransport:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.kind = getattr(inner, "kind", "recording")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def list_tools(self) -> list[ToolSpec]:
        return self.inner.list_tools()

    def call(self, name: str, args: dict[str, Any]) -> Any:
        with self._lock:
            self.calls.append((name, dict(args)))
        return self.inner.call(name, args)

    def close(self) -> None:
        self.inner.close()


class _BusinessQuoteError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("message") or body.get("error") or "refused"))
        self.status = status
        self._body = dict(body)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._body)


@pytest.fixture(scope="module")
def replay_catalog_base(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    mp = pytest.MonkeyPatch()
    base_dir = tmp_path_factory.mktemp("wave9-replay-cat")
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    orig_source = cat._SOURCE

    mp.setenv("GEMINI_API_KEY", "")
    mp.setenv("GOOGLE_API_KEY", "")
    mp.setenv("AIMO_API_KEY", "")
    mp.setenv("VALIO_API_KEY", "")
    mp.setenv("AIMO_OFFLINE", "0")
    mp.setenv("AIMO_REPLAY", REPLAY_REL)
    mp.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    mp.setenv("AIMO_NOW_FROZEN", "1")
    mp.setenv("AIMO_SEMANTIC", "0")
    mp.setenv("AIMO_RECIPE_CHECK", "0")
    mp.setenv("AIMO_DEMO_FAULTS", "1")
    mp.setenv("AIMO_DATA_DIR", str(data_dir))
    mp.delenv("AIMO_NOW", raising=False)
    mp.delenv("AIMO_LLM_MODE", raising=False)
    mp.delenv("AIMO_CATALOG_FIXTURE", raising=False)

    mp.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        mp.setattr(config, "GOOGLE_API_KEY", "")
    mp.setattr(config, "AIMO_API_KEY", "")
    mp.setattr(config, "OFFLINE", False)
    mp.setattr(config, "DATA_DIR", data_dir)
    mp.setattr(config, "PRODUCT_JSON", data_dir / "valio_aimo_junction26_product_data.json")
    mp.setattr(config, "RELIABILITY_CSV", data_dir / "valio_aimo_junction26_delivery_reliability.csv")
    mp.setattr(config, "CO2_CSV", data_dir / "valio_aimo_junction26_carbon_footprint.csv")
    mp.setattr(config, "CATALOG_DB", data_dir / "catalog.sqlite")

    src = cat._use_fixture_catalog()
    assert src == "fixture-subset"
    mp.setattr(cat, "_SOURCE", src)
    mp.setattr(cs, "PRODUCT_JSON", config.PRODUCT_JSON)
    mp.setattr(cs, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    mp.setattr(cs, "CO2_CSV", config.CO2_CSV)
    mp.setattr(cs, "CATALOG_DB", config.CATALOG_DB)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    mp.setattr(bc, "_client", replay.ReplayClient(replay.BUNDLED_DIR))

    try:
        cat.load_catalog()
        textsearch._con()
        textsearch._nonfood_categories()
        yield base_dir
    finally:
        try:
            textsearch._con().close()
        except Exception:
            pass
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        mp.setattr(cat, "_SOURCE", orig_source)
        mp.undo()
        shutil.rmtree(base_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_state(
    replay_catalog_base: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Path]:
    state_db = tmp_path / "state.sqlite"
    key_dir = tmp_path / "keys"
    ledger_dir = tmp_path / "ledger"
    key_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.setenv("AIMO_DEMO_FAULTS", "1")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_AUTO_APPROVE", raising=False)

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(store, "DB_PATH", state_db)

    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Real Gemini network calls are forbidden in keyless tests")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)

    store.conn()
    signing.public_jwk()
    service.audit_verify()

    try:
        yield tmp_path
    finally:
        con = store._conns.pop(str(state_db), None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        signing._keys.pop(str(key_dir / "ed25519-signing.pem"), None)


def _start_fake_approver(
    gateways_by_key: dict[str, tuple[FakeGateway, str]],
    stop: threading.Event,
) -> threading.Thread:
    seen: set[str] = set()

    def _loop() -> None:
        while not stop.is_set():
            for gw, approver in gateways_by_key.values():
                for pending_order in list(gw.pending()):
                    oid = pending_order["order_id"]
                    if oid in seen:
                        continue
                    gw.decide(oid, "approve", approver, "Approved in concurrent fake test")
                    seen.add(oid)
            stop.wait(0.01)

    thread = threading.Thread(target=_loop, daemon=True, name="test-fake-approver")
    thread.start()
    return thread


def _start_inproc_approver(stop: threading.Event) -> threading.Thread:
    seen: set[str] = set()

    def _loop() -> None:
        decide_fn = getattr(service, "decide_approval", None) or service.decide
        while not stop.is_set():
            for cust, approver in APPROVERS.items():
                try:
                    pending = service.list_approvals(status="pending", customer_id=cust)
                except Exception:
                    continue
                for item in pending if isinstance(pending, list) else []:
                    order = item.get("order", item) if isinstance(item, dict) else {}
                    quote = (item.get("quote") or {}) if isinstance(item, dict) else {}
                    oid = order.get("order_id")
                    if not oid or oid in seen or order.get("customer_id") != cust:
                        continue
                    qhash = order.get("quote_hash") or quote.get("quote_hash") or ""
                    try:
                        decide_fn(
                            oid,
                            decision="approve",
                            approver=approver,
                            note="Approved in concurrent inproc test",
                            quote_hash=qhash,
                        )
                        seen.add(oid)
                    except Exception:
                        continue
            stop.wait(0.01)

    thread = threading.Thread(target=_loop, daemon=True, name="test-inproc-approver")
    thread.start()
    return thread


class _ThreadSafeCatalogTransport:
    kind = "inproc"

    def __init__(self, inner: InProcessTransport, catalog_lock: threading.Lock) -> None:
        self.inner = inner
        self.catalog_lock = catalog_lock

    def list_tools(self) -> list[ToolSpec]:
        return self.inner.list_tools()

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name in ("search_products", "get_product", "plan_need", "create_quote", "place_order"):
            with self.catalog_lock:
                return self.inner.call(name, args)
        return self.inner.call(name, args)

    def close(self) -> None:
        self.inner.close()


def _active_demo_approver_threads() -> list[threading.Thread]:
    targets = {
        run_demo.http_auto_approver,
        run_demo.fake_auto_approver,
        run_demo.inproc_auto_approver,
    }
    names = ("http_auto_approver", "fake_auto_approver", "inproc_auto_approver")
    found: list[threading.Thread] = []
    for th in threading.enumerate():
        if not th.is_alive():
            continue
        tgt = getattr(th, "_target", None)
        if tgt in targets or any(tag in th.name for tag in names):
            found.append(th)
    return found


def _demo_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "scenario": "happy",
        "text": None,
        "customer": None,
        "auto_approve_as": None,
        "no_auto_approve": False,
        "reject": False,
        "approve_delay": 0.05,
        "transport": "fake",
        "base_url": DEAD_BASE,
        "via_server": False,
        "model": None,
        "thinking": "LOW",
        "no_escalate": False,
        "max_steps": 14,
        "approval_timeout": 5.0,
        "image": None,
        "answer": "auto",
        "json": None,
        "faults": None,
        "montage": False,
        "montage_out": "montage.md",
        "baseline_pid": None,
        "baseline_orders": 5,
        "baseline_label": None,
        "allergen_probe": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_file_hygiene_zero_comments_and_zero_em_en_dashes() -> None:
    path = Path(__file__).resolve()
    src = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    comments = [tok for tok in tokens if tok.type == tokenize.COMMENT]
    assert comments == []
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_concurrent_buyer_runs_on_fake_gateway_have_zero_state_bleed() -> None:
    specs = [
        {
            "key": "aurora_injection",
            "customer_id": "hotel-aurora",
            "scenario": "injection",
            "text": EN_INJECTION_MILK,
            "expected_needs_len": 1,
            "expected_plans_keys": {0},
            "expected_allergens": set(),
            "expect_injections": True,
        },
        {
            "key": "aurora_multi",
            "customer_id": "hotel-aurora",
            "scenario": "happy",
            "text": FI_MULTI_EVENT,
            "expected_needs_len": 2,
            "expected_plans_keys": {0, 1},
            "expected_allergens": set(),
            "expect_injections": False,
        },
        {
            "key": "school_oat",
            "customer_id": "school-tampere",
            "scenario": "happy",
            "text": FI_SCHOOL_OAT,
            "expected_needs_len": 1,
            "expected_plans_keys": {0},
            "expected_allergens": {"milk", "nuts"},
            "expect_injections": False,
        },
        {
            "key": "hospital_bread",
            "customer_id": "hospital-meilahti",
            "scenario": "happy",
            "text": EN_HOSPITAL_BREAD,
            "expected_needs_len": 1,
            "expected_plans_keys": {0},
            "expected_allergens": {"gluten"},
            "expect_injections": False,
        },
    ]

    gateways: dict[str, tuple[FakeGateway, str]] = {}
    for sp in specs:
        cid = sp["customer_id"]
        prof = get_profile(cid)
        gw = FakeGateway(cid, prof["agent_id"], scenario=sp["scenario"], now=SAT)
        gateways[sp["key"]] = (gw, APPROVERS[cid])

    barrier = threading.Barrier(len(specs))
    stop = threading.Event()
    approver_thread = _start_fake_approver(gateways, stop)

    def _worker(sp: dict[str, Any]) -> tuple[dict[str, Any], BuyerAgent, dict[str, Any], list[AgentStep]]:
        cid = sp["customer_id"]
        gw, _ = gateways[sp["key"]]
        emitted: list[AgentStep] = []
        transport = FakeTransport(gw)
        try:
            agent = BuyerAgent(
                transport,
                cid,
                run_id=f"run-fake-{sp['key']}",
                poll_s=0.02,
                approval_timeout_s=5.0,
                now=SAT,
                emit=emitted.append,
            )
            barrier.wait(timeout=5.0)
            res = agent.run(sp["text"])
            return sp, agent, res, emitted
        finally:
            transport.close()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_worker, sp) for sp in specs]
            outcomes = [fut.result(timeout=15.0) for fut in futures]
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    seen_run_ids: set[str] = set()
    seen_quote_ids: set[str] = set()
    seen_order_ids: set[str] = set()

    for sp, agent, res, emitted in outcomes:
        cid = sp["customer_id"]
        assert agent.mock is True
        assert res["mock"] is True
        assert res["status"] == "placed"
        assert agent.customer_id == cid
        assert res["need"]["customer_id"] == cid
        assert len(res["needs"]) == sp["expected_needs_len"]
        assert all(n["customer_id"] == cid for n in res["needs"])
        assert set(res["need"]["excluded_allergens"]) == sp["expected_allergens"]

        assert set(agent.plans.keys()) == sp["expected_plans_keys"]
        for plan_item in agent.plans.values():
            assert plan_item["need"]["customer_id"] == cid
            assert set(plan_item["need"]["excluded_allergens"]) >= sp["expected_allergens"]

        quote = res["quote"]
        order = res["order"]
        assert isinstance(quote, dict) and quote["customer_id"] == cid
        assert isinstance(order, dict) and order["customer_id"] == cid
        assert order["quote_id"] == quote["quote_id"]
        assert order["quote_hash"] == quote["quote_hash"]
        if order.get("approved_by"):
            assert order["approved_by"] == APPROVERS[cid]

        seen_run_ids.add(res["run_id"])
        seen_quote_ids.add(quote["quote_id"])
        seen_order_ids.add(order["order_id"])

        if sp["expect_injections"]:
            assert len(agent.injections) >= 1
            assert all(f["product_id"] == "469363" for f in agent.injections)
        else:
            assert agent.injections == []

        steps = res["steps"]
        assert len(steps) == len(emitted) >= 6
        assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
        assert all(s["run_id"] == res["run_id"] for s in steps)
        assert [s.seq for s in emitted] == list(range(1, len(emitted) + 1))
        assert all(s.run_id == res["run_id"] for s in emitted)

        models_used = res["models_used"]
        assert len(models_used) >= 3
        assert [m["step"] for m in models_used] == list(range(len(models_used)))
        assert all(m["model"] == MOCK_MODEL_NAME for m in models_used)

    assert len(seen_run_ids) == 4
    assert len(seen_quote_ids) == 4
    assert len(seen_order_ids) == 4


def test_concurrent_buyer_runs_on_inproc_replay_snapshot_have_zero_state_bleed() -> None:
    assert mock_wanted() is True
    assert cat.catalog_source() == "fixture-subset"

    specs = [
        {
            "key": "aurora_injection",
            "customer_id": "hotel-aurora",
            "faults": "inject_text:*",
            "text": EN_INJECTION_MILK,
            "expected_needs_len": 1,
            "expected_plans_keys": {0},
            "expected_allergens": set(),
            "expect_injections": True,
        },
        {
            "key": "aurora_multi",
            "customer_id": "hotel-aurora",
            "faults": None,
            "text": FI_MULTI_EVENT,
            "expected_needs_len": 2,
            "expected_plans_keys": {0, 1},
            "expected_allergens": set(),
            "expect_injections": False,
        },
        {
            "key": "school_oat",
            "customer_id": "school-tampere",
            "faults": None,
            "text": FI_SCHOOL_OAT,
            "expected_needs_len": 1,
            "expected_plans_keys": {0},
            "expected_allergens": {"milk", "nuts"},
            "expect_injections": False,
        },
        {
            "key": "hospital_bread",
            "customer_id": "hospital-meilahti",
            "faults": None,
            "text": SV_HOSPITAL_BREAD,
            "expected_needs_len": 1,
            "expected_plans_keys": {0},
            "expected_allergens": {"gluten"},
            "expect_injections": False,
        },
    ]

    catalog_lock = threading.Lock()
    barrier = threading.Barrier(len(specs))
    stop = threading.Event()
    approver_thread = _start_inproc_approver(stop)

    def _worker(sp: dict[str, Any]) -> tuple[dict[str, Any], BuyerAgent, dict[str, Any]]:
        cid = sp["customer_id"]
        prof = get_profile(cid)
        raw_transport = InProcessTransport(
            gateway_agent_id(prof["agent_id"], cid),
            cid,
            mandate=mandate_for(cid),
            faults=sp["faults"],
        )
        transport = _ThreadSafeCatalogTransport(raw_transport, catalog_lock)
        try:
            agent = BuyerAgent(
                transport,
                cid,
                run_id=f"run-inproc-{sp['key']}",
                poll_s=0.02,
                approval_timeout_s=8.0,
                now=SAT,
            )
            barrier.wait(timeout=5.0)
            res = agent.run(sp["text"])
            return sp, agent, res
        finally:
            transport.close()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_worker, sp) for sp in specs]
            outcomes = [fut.result(timeout=20.0) for fut in futures]
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    seen_run_ids: set[str] = set()
    seen_quote_ids: set[str] = set()
    seen_order_ids: set[str] = set()

    for sp, agent, res in outcomes:
        cid = sp["customer_id"]
        assert agent.mock is True
        assert res["status"] == "placed"
        assert agent.customer_id == cid
        assert res["need"]["customer_id"] == cid
        assert len(res["needs"]) == sp["expected_needs_len"]
        assert all(n["customer_id"] == cid for n in res["needs"])
        assert set(res["need"]["excluded_allergens"]) >= sp["expected_allergens"]

        if cid == "hotel-aurora":
            assert res["need"]["excluded_allergens"] == []
        elif cid == "school-tampere":
            assert "gluten" not in res["need"]["excluded_allergens"]
        elif cid == "hospital-meilahti":
            assert "milk" not in res["need"]["excluded_allergens"]
            assert "nuts" not in res["need"]["excluded_allergens"]

        assert set(agent.plans.keys()) == sp["expected_plans_keys"]
        for plan_item in agent.plans.values():
            assert plan_item["need"]["customer_id"] == cid
            assert set(plan_item["need"]["excluded_allergens"]) >= sp["expected_allergens"]

        quote = res["quote"]
        order = res["order"]
        assert isinstance(quote, dict) and quote["customer_id"] == cid
        assert isinstance(order, dict) and order["customer_id"] == cid
        assert order["quote_id"] == quote["quote_id"]
        assert order["quote_hash"] == quote["quote_hash"]

        approved_by = order.get("approved_by") or ""
        if approved_by and not str(approved_by).startswith("policy:"):
            assert approved_by == APPROVERS[cid]

        stored_quote = store.get_quote(quote["quote_id"])
        assert stored_quote is not None
        assert stored_quote.customer_id == cid
        assert signing.verify_quote(stored_quote) is True

        seen_run_ids.add(res["run_id"])
        seen_quote_ids.add(quote["quote_id"])
        seen_order_ids.add(order["order_id"])

        if sp["expect_injections"]:
            assert len(agent.injections) >= 1
        else:
            assert agent.injections == []

        steps = res["steps"]
        assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
        assert all(s["run_id"] == res["run_id"] for s in steps)

        models_used = res["models_used"]
        assert len(models_used) >= 3
        assert [m["step"] for m in models_used] == list(range(len(models_used)))
        assert all(m["model"] == MOCK_MODEL_NAME for m in models_used)

    assert len(seen_run_ids) == 4
    assert len(seen_quote_ids) == 4
    assert len(seen_order_ids) == 4

    audit_status = service.audit_verify()
    assert audit_status["ok"] is True
    assert audit_status.get("broken_at") is None


def test_concurrent_custom_model_runs_isolate_models_used_and_escalation() -> None:
    worker_cfgs = [
        ("w0", "hotel-aurora", EN_HERO_MILK, "model-alpha", "reason-alpha", True, True),
        ("w1", "hotel-aurora", EN_HERO_MILK, "model-beta", "reason-beta", False, False),
        ("w2", "school-tampere", FI_SCHOOL_OAT, "model-gamma", "reason-gamma", True, True),
        ("w3", "hospital-meilahti", EN_HOSPITAL_BREAD, "model-delta", "reason-delta", True, True),
    ]
    gateways: dict[str, tuple[FakeGateway, str]] = {}
    for key, cid, *_ in worker_cfgs:
        prof = get_profile(cid)
        gateways[key] = (FakeGateway(cid, prof["agent_id"], now=SAT), APPROVERS[cid])

    barrier = threading.Barrier(len(worker_cfgs))
    stop = threading.Event()
    approver_thread = _start_fake_approver(gateways, stop)

    def _make_gen(need_customer: str) -> Any:
        def _gen(feature: str, model: str, contents: Any, config: Any = None) -> Any:
            if isinstance(contents, str):
                return _txt(f"{need_customer}:{feature}:{model}")
            done = _fn_response_names(contents)
            if "plan_need" not in done:
                return _fc("plan_need", {"need": {"customer_id": need_customer}})
            if "create_quote" not in done:
                plan = _last_fn_response(contents, "plan_need") or {}
                lines = [
                    {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"]}
                    for l in plan.get("lines", [])
                ]
                return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
            if "place_order" not in done:
                quote = _last_fn_response(contents, "create_quote") or {}
                return _fc("place_order", {"quote_id": quote["quote_id"], "quote_hash": quote["quote_hash"]})
            return _txt(f"completed for {need_customer}")

        return _gen

    def _run_one(item: tuple[str, str, str, str, str, bool, bool]) -> tuple[str, str, str, str, bool, BuyerAgent, dict[str, Any]]:
        key, cid, text, base_model, reason_model, esc_flag, expect_esc = item
        gw, _ = gateways[key]
        agent = BuyerAgent(
            FakeTransport(gw),
            cid,
            model=base_model,
            reasoning_model=reason_model,
            escalate=esc_flag,
            generate_fn=_make_gen(cid),
            use_gemini_parse=False,
            poll_s=0.02,
            approval_timeout_s=5.0,
            now=SAT,
        )
        barrier.wait(timeout=5.0)
        res = agent.run(text)
        return key, cid, base_model, reason_model, expect_esc, agent, res

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_run_one, item) for item in worker_cfgs]
            results = [fut.result(timeout=15.0) for fut in futures]
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    for _, cid, base_model, reason_model, expect_esc, agent, res in results:
        assert res["status"] == "placed"
        assert agent.customer_id == cid
        assert agent.escalated is expect_esc
        used_names = [m["model"] for m in res["models_used"]]
        allowed = {base_model, reason_model} if expect_esc else {base_model}
        assert set(used_names) == allowed
        if expect_esc:
            esc_entries = [m for m in res["models_used"] if m["model"] == reason_model]
            assert len(esc_entries) == 1
            assert esc_entries[0]["thinking"] == "HIGH"


@pytest.mark.parametrize("transport_kind", ["fake", "inproc"])
def test_multilingual_en_fi_sv_scripted_buyer_positive_flows_e2e(transport_kind: str) -> None:
    cases = [
        {
            "lang": "en",
            "customer_id": "hotel-aurora",
            "text": EN_HERO_MILK,
            "expected_items": [(1000.0, "L", ["FREE_FROM_LACTOSE"])],
            "expected_delivery": "2026-11-16",
            "expected_allergens": set(),
            "min_quote_lines": 1,
        },
        {
            "lang": "fi",
            "customer_id": "school-tampere",
            "text": FI_SCHOOL_OAT,
            "expected_items": [(200.0, "L", [])],
            "expected_delivery": "2026-11-18",
            "expected_allergens": {"milk", "nuts"},
            "min_quote_lines": 1,
        },
        {
            "lang": "sv",
            "customer_id": "hotel-aurora",
            "text": SV_HOTEL_MILK,
            "expected_items": [(1000.0, "L", ["FREE_FROM_LACTOSE"])],
            "expected_delivery": "2026-11-16",
            "expected_allergens": set(),
            "min_quote_lines": 1,
        },
        {
            "lang": "fi_multi",
            "customer_id": "hotel-aurora",
            "text": FI_MULTI_EVENT,
            "expected_items": [
                (40.0, "L", ["FREE_FROM_LACTOSE"]),
                (20.0, "KG", ["FREE_FROM_LACTOSE"]),
            ],
            "expected_delivery": "2026-11-16",
            "expected_allergens": set(),
            "min_quote_lines": 2,
        },
        {
            "lang": "sv_multi",
            "customer_id": "hotel-aurora",
            "text": SV_MULTI_EVENT,
            "expected_items": [
                (40.0, "L", ["FREE_FROM_LACTOSE"]),
                (20.0, "KG", ["FREE_FROM_LACTOSE"]),
            ],
            "expected_delivery": "2026-11-16",
            "expected_allergens": set(),
            "min_quote_lines": 2,
        },
        {
            "lang": "sv_hospital",
            "customer_id": "hospital-meilahti",
            "text": SV_HOSPITAL_BREAD,
            "expected_items": [(10.0, "KG", ["FREE_FROM_GLUTEN"])],
            "expected_delivery": "2026-11-16",
            "expected_allergens": {"gluten"},
            "min_quote_lines": 1,
        },
    ]

    for case in cases:
        cid = case["customer_id"]
        prof = get_profile(cid)
        stop = threading.Event()
        if transport_kind == "fake":
            gw = FakeGateway(cid, prof["agent_id"], now=SAT)
            approver_thread = _start_fake_approver({cid: (gw, APPROVERS[cid])}, stop)
            inner_transport: Any = FakeTransport(gw)
        else:
            approver_thread = _start_inproc_approver(stop)
            inner_transport = InProcessTransport(
                gateway_agent_id(prof["agent_id"], cid),
                cid,
                mandate=mandate_for(cid),
            )
        rec = RecordingTransport(inner_transport)
        try:
            agent = BuyerAgent(
                rec,
                cid,
                poll_s=0.02,
                approval_timeout_s=6.0,
                now=SAT,
            )
            assert agent.mock is True
            res = agent.run(case["text"])
        finally:
            stop.set()
            approver_thread.join(timeout=5.0)
            rec.close()

        assert res["status"] == "placed", (transport_kind, case["lang"], res)
        assert res["mock"] is True
        assert res["summary"].startswith(MOCK_LABEL)

        needs = res["needs"]
        assert len(needs) == len(case["expected_items"])
        for need_item, (exp_qty, exp_unit, exp_claims) in zip(needs, case["expected_items"]):
            assert need_item["quantity"] == exp_qty
            assert need_item["unit"] == exp_unit
            assert need_item["delivery_by"] == case["expected_delivery"]
            assert need_item["preferences"]["parsed_by"] == "regex"
            for cl in exp_claims:
                assert cl in need_item["required_claims"]
            assert set(need_item["excluded_allergens"]) >= case["expected_allergens"]

        if case["lang"] == "fi":
            assert "oat drink" in needs[0]["product_query"].lower()
            for ln in res["quote"]["lines"]:
                assert "oat" in str(ln["product_name"]).lower()
        elif case["lang"] == "sv":
            assert needs[0]["product_query"] == "lactose-free milk"
            assert needs[0]["preferred_product_ids"] == ["463038"]
        elif case["lang"] in ("fi_multi", "sv_multi"):
            assert needs[0]["product_query"] == "lactose-free whipping cream"
            assert needs[1]["product_query"] == "lactose-free butter"
            assert set(agent.plans.keys()) == {0, 1}

        call_names = [name for name, _ in rec.calls]
        assert call_names.count("plan_need") == len(case["expected_items"])
        assert call_names.count("create_quote") == 1
        assert call_names.count("place_order") == 1
        assert len(res["quote"]["lines"]) >= case["min_quote_lines"]
        assert res["order"]["status"] == "placed"

        step_kinds = [s["kind"] for s in res["steps"]]
        for expected_kind in ("thought", "plan", "quote", "decision", "order", "final"):
            assert expected_kind in step_kinds


@pytest.mark.parametrize("transport_kind", ["fake", "inproc"])
@pytest.mark.parametrize(
    ("customer_id", "bad_request"),
    [
        ("hotel-aurora", FI_ZERO_MILK),
        ("hotel-aurora", SV_NEG_BUTTER),
        ("school-tampere", "0 litraa kaurajuomaa keskiviikoksi"),
        ("hospital-meilahti", "-5 kg glutenfritt bröd imorgon"),
        ("hotel-aurora", "Order 0 litres of lactose-free milk tomorrow"),
        ("hotel-aurora", "Tilaa 40 l laktoositonta kuohukermaa ja 0 kg laktoositonta voita maanantaiksi"),
        ("hotel-aurora", "Beställ 40 l laktosfri vispgrädde och -2 kg laktosfri smör till måndag"),
    ],
)
def test_multilingual_non_positive_quantities_rejected_before_planning(
    transport_kind: str,
    customer_id: str,
    bad_request: str,
) -> None:
    prof = get_profile(customer_id)
    if transport_kind == "fake":
        inner: Any = FakeTransport(FakeGateway(customer_id, prof["agent_id"], now=SAT))
    else:
        inner = InProcessTransport(
            gateway_agent_id(prof["agent_id"], customer_id),
            customer_id,
            mandate=mandate_for(customer_id),
        )
    rec = RecordingTransport(inner)
    try:
        agent = BuyerAgent(rec, customer_id, poll_s=0.02, approval_timeout_s=1.0, now=SAT)
        res = agent.run(bad_request)
    finally:
        rec.close()

    assert res["status"] == "error"
    assert res["need"] is None
    assert res["needs"] == []
    assert res["plan"] is None
    assert res["quote"] is None
    assert res["order"] is None
    assert agent.plans == {}
    assert rec.calls == []
    assert "ValidationError" in res["summary"]
    err_steps = [s for s in res["steps"] if s["kind"] == "error"]
    assert len(err_steps) >= 1
    assert any("greater than 0" in str(s["data"].get("error", "")) for s in err_steps)
    step_kinds = [s["kind"] for s in res["steps"]]
    assert "plan" not in step_kinds
    assert "quote" not in step_kinds
    assert "order" not in step_kinds


@pytest.mark.parametrize(
    ("request_text", "bad_answer"),
    [
        ("Tarvitsemme laktoositonta kuohukermaa maanantaiksi.", "0 litraa"),
        ("Vi behöver laktosfri vispgrädde till måndag.", "-5 liter"),
    ],
)
def test_multilingual_clarification_and_harness_non_positive_quantities_rejected_before_planning(
    request_text: str,
    bad_answer: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    rec = RecordingTransport(FakeTransport(gw))
    try:
        agent = BuyerAgent(
            rec,
            "hotel-aurora",
            poll_s=0.02,
            approval_timeout_s=1.0,
            now=SAT,
            answer_fn=lambda _q: {"text": bad_answer},
        )
        res = agent.run(request_text)
    finally:
        rec.close()

    assert res["mock"] is True
    assert res["status"] == "no_order"
    assert res["plan"] is None
    assert res["quote"] is None
    assert res["order"] is None
    assert "non-positive quantity rejected" in res["summary"]
    called = [name for name, _ in rec.calls]
    assert "plan_need" not in called
    assert "create_quote" not in called
    assert "place_order" not in called

    unvalidated_zero = Need.model_construct(
        raw_text=FI_ZERO_MILK,
        customer_id="hotel-aurora",
        product_query="milk",
        required_claims=[],
        excluded_allergens=[],
        quantity=0.0,
        unit="L",
        delivery_by=SAT.date(),
        preferred_product_ids=[],
        preferences={"parsed_by": "regex"},
    )
    monkeypatch.setattr(buyer_agent_mod, "parse_request", lambda *a, **kw: [unvalidated_zero])
    gw2 = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    rec2 = RecordingTransport(FakeTransport(gw2))
    try:
        res2 = BuyerAgent(rec2, "hotel-aurora", poll_s=0.02, approval_timeout_s=1.0, now=SAT).run(FI_ZERO_MILK)
    finally:
        rec2.close()
    assert res2["status"] == "error"
    assert rec2.calls == []
    assert "non-positive quantity rejected" in res2["summary"]
    assert "Requested quantity must be a finite number greater than zero" in res2["summary"]


def test_multilingual_scripted_buyer_approval_wait_timeout_leaves_order_pending() -> None:
    gw = FakeGateway("school-tampere", "school-tampere/buyer-1", now=SAT)
    agent = BuyerAgent(
        FakeTransport(gw),
        "school-tampere",
        poll_s=0.02,
        approval_timeout_s=0.07,
        now=SAT,
    )
    res = agent.run(FI_SCHOOL_OAT)
    assert res["status"] == "pending_approval"
    assert res["order"]["timed_out"] is True
    assert len(gw.pending()) == 1
    assert "approval_wait" in [s["kind"] for s in res["steps"]]
    assert "approval timed out" in res["summary"]


@pytest.mark.parametrize("transport_mode", ["fake", "inproc"])
def test_run_demo_run_scenario_normal_and_fast_finish_join_auto_approver_threads(
    transport_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    assert _active_demo_approver_threads() == []

    closed_clients: list[bool] = []
    if transport_mode == "inproc":
        from fastapi.testclient import TestClient as RealTestClient

        class TrackingTestClient(RealTestClient):
            def close(self) -> None:
                closed_clients.append(True)
                super().close()

        import fastapi.testclient as tc_mod

        monkeypatch.setattr(tc_mod, "TestClient", TrackingTestClient)

    normal_args = _demo_args(transport=transport_mode, approve_delay=0.05, approval_timeout=8.0)
    res_ok = run_demo.run_scenario(normal_args, "happy", APPROVERS["hotel-aurora"])
    assert res_ok["status"] == "placed"
    assert res_ok["transport"] == transport_mode
    assert _active_demo_approver_threads() == []
    if transport_mode == "inproc":
        assert len(closed_clients) == 1

    fast_args = _demo_args(
        transport=transport_mode,
        text=FI_ZERO_MILK,
        approve_delay=2.5,
        approval_timeout=5.0,
    )
    t_start = time.monotonic()
    res_fast = run_demo.run_scenario(fast_args, "happy", APPROVERS["hotel-aurora"])
    elapsed = time.monotonic() - t_start
    assert res_fast["status"] == "error"
    assert elapsed < 2.0
    assert _active_demo_approver_threads() == []
    if transport_mode == "inproc":
        assert len(closed_clients) == 2

    capsys.readouterr()


@pytest.mark.parametrize("transport_mode", ["fake", "inproc"])
def test_run_demo_run_scenario_joins_auto_approver_thread_when_agent_run_raises(
    transport_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(run_demo, "_TTY", False)
    assert _active_demo_approver_threads() == []

    observed_live_threads: list[int] = []
    closed_clients: list[bool] = []

    if transport_mode == "inproc":
        from fastapi.testclient import TestClient as RealTestClient

        class TrackingTestClient(RealTestClient):
            def close(self) -> None:
                closed_clients.append(True)
                super().close()

        import fastapi.testclient as tc_mod

        monkeypatch.setattr(tc_mod, "TestClient", TrackingTestClient)

    def _boom_run(self: BuyerAgent, text: str) -> dict[str, Any]:
        live = _active_demo_approver_threads()
        observed_live_threads.append(len(live))
        raise RuntimeError(f"simulated crash in {transport_mode}")

    monkeypatch.setattr(BuyerAgent, "run", _boom_run)

    crash_args = _demo_args(transport=transport_mode, approve_delay=2.0, approval_timeout=5.0)
    with pytest.raises(RuntimeError, match=f"simulated crash in {transport_mode}"):
        run_demo.run_scenario(crash_args, "happy", APPROVERS["hotel-aurora"])

    assert observed_live_threads == [1]
    assert _active_demo_approver_threads() == []
    if transport_mode == "inproc":
        assert len(closed_clients) == 1

    capsys.readouterr()


def test_gemini_main_path_escalates_to_reasoning_model_after_tool_errors_and_tracks_models_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(buyer_agent_mod.time, "sleep", lambda _s: None)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    gw.fail_next["plan_need"] = 1

    quote_refused_once = {"done": False}
    orig_create_quote = gw.create_quote

    def _flaky_create_quote(delivery_date: str, lines: list[dict], need: dict | None = None) -> dict:
        if not quote_refused_once["done"]:
            quote_refused_once["done"] = True
            raise _BusinessQuoteError(
                409,
                {"error": "lines_unavailable", "message": "Stock shifted; replan before quoting."},
            )
        return orig_create_quote(delivery_date=delivery_date, lines=lines, need=need)

    monkeypatch.setattr(gw, "create_quote", _flaky_create_quote)

    stop = threading.Event()
    approver_thread = _start_fake_approver({"hotel-aurora": (gw, APPROVERS["hotel-aurora"])}, stop)

    step_calls: list[tuple[str, str]] = []

    def _mock_gemini_fn(feature: str, model: str, contents: Any, config: Any = None) -> Any:
        step_calls.append((feature, model))
        if isinstance(contents, str):
            return _txt(f"{feature} via {model}")
        done = _fn_response_names(contents)
        plan_count = done.count("plan_need")
        quote_count = done.count("create_quote")
        if plan_count == 0:
            return _fc("plan_need", {"need": {"product_query": "lactose-free milk"}})
        if quote_count == 0:
            plan = _last_fn_response(contents, "plan_need") or {}
            lines = [
                {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"]}
                for l in plan.get("lines", [])
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if quote_count == 1 and plan_count == 1:
            return _fc("plan_need", {"need": {"product_query": "lactose-free milk"}})
        if quote_count == 1 and plan_count == 2:
            plan = _last_fn_response(contents, "plan_need") or {}
            lines = [
                {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"]}
                for l in plan.get("lines", [])
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if "place_order" not in done:
            q = _last_fn_response(contents, "create_quote") or {}
            return _fc("place_order", {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
        return _txt("Gemini main path completed order after replanning.")

    try:
        agent = BuyerAgent(
            FakeTransport(gw),
            "hotel-aurora",
            escalate=True,
            generate_fn=_mock_gemini_fn,
            use_gemini_parse=False,
            poll_s=0.01,
            approval_timeout_s=5.0,
            now=SAT,
        )
        assert agent.mock is False
        assert agent.model == config.GEMINI_AGENT_MODEL
        assert agent.reasoning_model == config.GEMINI_REASONING_MODEL
        res = agent.run(EN_HERO_MILK)
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    assert res["status"] == "placed"
    assert res["mock"] is False
    assert agent.escalated is True
    assert agent.tool_failures["plan_need"] == 1
    assert agent.tool_failures["create_quote"] == 1

    expected_step_models = [
        (config.GEMINI_AGENT_MODEL, "LOW"),
        (config.GEMINI_REASONING_MODEL, "HIGH"),
        (config.GEMINI_AGENT_MODEL, "LOW"),
        (config.GEMINI_REASONING_MODEL, "HIGH"),
        (config.GEMINI_AGENT_MODEL, "LOW"),
    ]
    assert [(m["model"], m["thinking"]) for m in res["models_used"]] == expected_step_models
    assert [m["step"] for m in res["models_used"]] == [0, 1, 2, 3, 4]

    final_steps = [s for s in res["steps"] if s["kind"] == "final"]
    assert len(final_steps) == 1
    assert final_steps[0]["data"]["escalated"] is True
    assert final_steps[0]["data"]["models_used"] == res["models_used"]


def test_gemini_main_path_reasoning_model_error_falls_back_cleanly_to_agent_model() -> None:
    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    stop = threading.Event()
    approver_thread = _start_fake_approver({"hotel-aurora": (gw, APPROVERS["hotel-aurora"])}, stop)

    reasoning_model = config.GEMINI_REASONING_MODEL
    attempted_models: list[str] = []

    def _reasoning_fails_fn(feature: str, model: str, contents: Any, config: Any = None) -> Any:
        attempted_models.append(model)
        if model == reasoning_model:
            raise RuntimeError("503 reasoning model temporarily unavailable")
        if isinstance(contents, str):
            return _txt(f"{feature} ok")
        done = _fn_response_names(contents)
        if "plan_need" not in done:
            return _fc("plan_need", {"need": {"product_query": "lactose-free milk"}})
        if "create_quote" not in done:
            plan = _last_fn_response(contents, "plan_need") or {}
            lines = [
                {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"]}
                for l in plan.get("lines", [])
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if "place_order" not in done:
            q = _last_fn_response(contents, "create_quote") or {}
            return _fc("place_order", {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
        return _txt("Completed on fallback base model.")

    try:
        agent = BuyerAgent(
            FakeTransport(gw),
            "hotel-aurora",
            escalate=True,
            generate_fn=_reasoning_fails_fn,
            use_gemini_parse=False,
            poll_s=0.02,
            approval_timeout_s=5.0,
            now=SAT,
        )
        res = agent.run(EN_HERO_MILK)
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    assert res["status"] == "placed"
    assert config.GEMINI_REASONING_MODEL in attempted_models
    assert agent.escalated is False
    assert all(m["model"] == config.GEMINI_AGENT_MODEL for m in res["models_used"])
    err_titles = [s["title"] for s in res["steps"] if s["kind"] == "error"]
    assert any(f"Escalation model {config.GEMINI_REASONING_MODEL} failed" in t for t in err_titles)


@pytest.mark.parametrize("parse_failure_mode", ["exception", "bad_json"])
@pytest.mark.parametrize(
    ("customer_id", "request_text", "expected_needs_count"),
    [
        ("hotel-aurora", SV_HOTEL_MILK, 1),
        ("hotel-aurora", FI_MULTI_EVENT, 2),
    ],
)
def test_gemini_parse_request_failure_falls_back_to_regex_while_main_gemini_loop_continues(
    parse_failure_mode: str,
    customer_id: str,
    request_text: str,
    expected_needs_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_attempts: list[tuple[str, str]] = []

    def _failing_parse_generate(
        feature: str,
        model: str,
        contents: Any,
        config_arg: Any = None,
        **kwargs: Any,
    ) -> Any:
        assert feature == "buyer.parse_need"
        parse_attempts.append((feature, model))
        if parse_failure_mode == "exception":
            raise RuntimeError("503 Gemini structured parse outage")
        return _txt("{malformed json payload")

    monkeypatch.setattr("aimo.gemini.generate", _failing_parse_generate)

    prof = get_profile(customer_id)
    gw = FakeGateway(customer_id, prof["agent_id"], now=SAT)
    stop = threading.Event()
    approver_thread = _start_fake_approver({customer_id: (gw, APPROVERS[customer_id])}, stop)

    def _main_loop_fn(feature: str, model: str, contents: Any, config: Any = None) -> Any:
        if isinstance(contents, str):
            return _txt(f"{feature}:{model}")
        done = _fn_response_names(contents)
        planned = done.count("plan_need")
        if planned < expected_needs_count:
            queries = ["lactose-free whipping cream", "lactose-free butter"] if expected_needs_count == 2 else ["lactose-free milk"]
            return _fc("plan_need", {"need": {"product_query": queries[planned]}})
        if "create_quote" not in done:
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": []})
        if "place_order" not in done:
            q = _last_fn_response(contents, "create_quote") or {}
            return _fc("place_order", {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
        return _txt("Main Gemini loop succeeded after deterministic parse fallback.")

    try:
        agent = BuyerAgent(
            FakeTransport(gw),
            customer_id,
            escalate=True,
            generate_fn=_main_loop_fn,
            use_gemini_parse=True,
            poll_s=0.02,
            approval_timeout_s=5.0,
            now=SAT,
        )
        assert agent.mock is False
        assert agent.use_gemini_parse is True
        res = agent.run(request_text)
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    expected_parse_calls = 1 if parse_failure_mode == "exception" else 2
    assert len(parse_attempts) == expected_parse_calls
    assert res["status"] == "placed"
    assert res["mock"] is False
    assert len(res["needs"]) == expected_needs_count
    expected_err_type = "RuntimeError" if parse_failure_mode == "exception" else "JSONDecodeError"
    for need_item in res["needs"]:
        assert need_item["preferences"]["parsed_by"] == "regex"
        assert need_item["preferences"]["gemini_error"] == expected_err_type

    assert len(res["models_used"]) == 2 + expected_needs_count
    if expected_needs_count == 1:
        assert agent.escalated is True
        assert any(m["model"] == config.GEMINI_REASONING_MODEL for m in res["models_used"])


def test_gemini_live_mode_tracks_quota_fallback_model_in_models_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIMO_LLM_MODE", "live")
    assert mock_wanted() is False

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)
    stop = threading.Event()
    approver_thread = _start_fake_approver({"hotel-aurora": (gw, APPROVERS["hotel-aurora"])}, stop)

    last_answered = {"model": config.GEMINI_AGENT_MODEL}

    def _live_stub_generate(
        feature: str,
        model: str,
        contents: Any,
        config_arg: Any = None,
        **kwargs: Any,
    ) -> Any:
        if feature == "buyer.parse_need":
            raise RuntimeError("parse endpoint quota")
        if model == config.GEMINI_REASONING_MODEL:
            last_answered["model"] = config.GEMINI_FAST_MODEL
        else:
            last_answered["model"] = model
        if isinstance(contents, str):
            return _txt(f"{feature}:{last_answered['model']}")
        done = _fn_response_names(contents)
        if "plan_need" not in done:
            return _fc("plan_need", {"need": {"product_query": "lactose-free milk"}})
        if "create_quote" not in done:
            plan = _last_fn_response(contents, "plan_need") or {}
            lines = [
                {"product_id": l["product_id"], "qty_sales_units": l["qty_sales_units"]}
                for l in plan.get("lines", [])
            ]
            return _fc("create_quote", {"delivery_date": "2026-11-16", "lines": lines})
        if "place_order" not in done:
            q = _last_fn_response(contents, "create_quote") or {}
            return _fc("place_order", {"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]})
        return _txt("Live stub completed.")

    monkeypatch.setattr("aimo.gemini.generate", _live_stub_generate)
    monkeypatch.setattr("aimo.gemini.last_model", lambda requested: last_answered["model"])

    try:
        agent = BuyerAgent(
            FakeTransport(gw),
            "hotel-aurora",
            escalate=True,
            poll_s=0.02,
            approval_timeout_s=5.0,
            now=SAT,
        )
        assert agent.mock is False
        res = agent.run(SV_HOTEL_MILK)
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    assert res["status"] == "placed"
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert res["need"]["preferences"]["gemini_error"] == "RuntimeError"
    assert agent.escalated is True
    assert [m["model"] for m in res["models_used"]] == [
        config.GEMINI_AGENT_MODEL,
        config.GEMINI_FAST_MODEL,
        config.GEMINI_AGENT_MODEL,
    ]
    thought_titles = [s["title"] for s in res["steps"] if s["kind"] == "thought"]
    assert any(f"{config.GEMINI_REASONING_MODEL} was unavailable (quota)" in t for t in thought_titles)


def test_concurrent_shared_fake_gateway_thread_safety_under_parallel_agents() -> None:
    prof = get_profile("hotel-aurora")
    shared_gw = FakeGateway("hotel-aurora", prof["agent_id"], now=SAT)
    requests = [
        EN_HERO_MILK,
        SV_HOTEL_MILK,
        FI_MULTI_EVENT,
        SV_MULTI_EVENT,
    ]
    barrier = threading.Barrier(len(requests))
    stop = threading.Event()
    approver_thread = _start_fake_approver({"shared": (shared_gw, APPROVERS["hotel-aurora"])}, stop)

    def _run_shared(idx_text: tuple[int, str]) -> dict[str, Any]:
        idx, text = idx_text
        transport = FakeTransport(shared_gw)
        try:
            agent = BuyerAgent(
                transport,
                "hotel-aurora",
                run_id=f"run-shared-{idx}",
                poll_s=0.02,
                approval_timeout_s=5.0,
                now=SAT,
            )
            barrier.wait(timeout=5.0)
            return agent.run(text)
        finally:
            transport.close()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_run_shared, (i, req)) for i, req in enumerate(requests)]
            results = [f.result(timeout=15.0) for f in futures]
    finally:
        stop.set()
        approver_thread.join(timeout=5.0)

    assert all(r["status"] == "placed" for r in results)
    qids = {r["quote"]["quote_id"] for r in results}
    oids = {r["order"]["order_id"] for r in results}
    assert len(qids) == 4
    assert len(oids) == 4
    assert set(shared_gw.quotes.keys()) == qids
    assert set(shared_gw.orders.keys()) == oids
    assert [ev["seq"] for ev in shared_gw.audit] == list(range(1, len(shared_gw.audit) + 1))
