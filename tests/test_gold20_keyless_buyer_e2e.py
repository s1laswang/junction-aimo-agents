from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest
from pydantic import ValidationError

import aimo.backend.catalog_store as cs
import aimo.catalog as cat
from aimo import config, gemini
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.eval_gold import APPROVERS, gold_answer, load_gold, score, summarize
from aimo.buyer.mock_model import MOCK_MODEL_NAME, MockModel, mock_wanted
from aimo.buyer.need import parse_quantity, parse_request
from aimo.buyer.profiles import get_profile, mandate_for
from aimo.buyer.tools import InProcessTransport, ToolSpec, gateway_agent_id
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.trust import signing

ROOT = Path(__file__).resolve().parents[1]
GOLD_20_PATH = ROOT / "tests" / "fixtures" / "gold" / "gold-buyer-20.jsonl"
REPLAY_REL = "tests/fixtures/snapshot/replay"


class RecordingInProcessTransport:
    kind = "inproc"

    def __init__(self, inner: InProcessTransport) -> None:
        self.inner = inner
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[ToolSpec]:
        return self.inner.list_tools()

    def call(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        return self.inner.call(name, args)

    def close(self) -> None:
        self.inner.close()


@pytest.fixture(scope="module")
def replay_fixture_catalog(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    mp = pytest.MonkeyPatch()
    base_dir = tmp_path_factory.mktemp("gold20-replay-cat")
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

    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini network calls are forbidden in keyless buyer tests")

    mp.setattr("aimo.gemini.generate", forbid_gemini)
    mp.setattr("aimo.gemini.embed", forbid_gemini)

    try:
        cat.load_catalog()
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


def _start_inproc_approver(stop: threading.Event) -> threading.Thread:
    seen: set[str] = set()

    def _loop() -> None:
        decide_fn = getattr(service, "decide_approval", None) or service.decide
        while not stop.is_set():
            for cust, approver_email in APPROVERS.items():
                try:
                    pending = service.list_approvals(status="pending", customer_id=cust)
                except Exception:
                    continue
                for item in pending if isinstance(pending, list) else []:
                    order = item.get("order", item) if isinstance(item, dict) else {}
                    quote = (item.get("quote") or {}) if isinstance(item, dict) else {}
                    oid = order.get("order_id")
                    if not oid or oid in seen:
                        continue
                    qhash = order.get("quote_hash") or quote.get("quote_hash") or ""
                    try:
                        decide_fn(
                            oid,
                            decision="approve",
                            approver=approver_email,
                            note="Approved by gold20 in-process approver",
                            quote_hash=qhash,
                        )
                        seen.add(oid)
                    except Exception:
                        continue
            stop.wait(0.02)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread


def _close_state_db(state_db: Path) -> None:
    con = store._conns.pop(str(state_db), None)
    if con is not None:
        con.close()


def test_file_hygiene_no_comments_or_dashes() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


def test_gold20_keyless_buyer_over_inproc_replay_snapshot(
    replay_fixture_catalog: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert mock_wanted() is True
    assert cat.catalog_source() == "fixture-subset"

    examples = load_gold([str(GOLD_20_PATH)])
    assert len(examples) == 20
    assert [ex["id"] for ex in examples] == [f"g{i:02d}" for i in range(1, 21)]
    assert {ex["lang"] for ex in examples} == {"en", "fi", "sv"}
    assert {ex["segment"] for ex in examples} == {"hotel", "school", "hospital", "event"}

    scorer_transport = InProcessTransport("scorer", "hotel-aurora", mandate=mandate_for("hotel-aurora"))
    allergen_cache: dict[str, dict[str, str]] = {}

    def allergen_lookup(pid: str) -> dict[str, str]:
        key = str(pid)
        if key not in allergen_cache:
            prod = scorer_transport.call("get_product", {"product_id": key})
            allergen_cache[key] = {
                str(k): str(v) for k, v in ((prod or {}).get("allergens") or {}).items()
            }
        return allergen_cache[key]

    rows: list[dict[str, Any]] = []
    results_by_id: dict[str, dict[str, Any]] = {}

    try:
        for ex in examples:
            gid = ex["id"]
            cust = ex["customer_id"]
            prof = get_profile(cust)
            state_db = tmp_path / f"state_{gid}.sqlite"
            key_dir = tmp_path / f"keys_{gid}"
            key_dir.mkdir(parents=True, exist_ok=True)

            monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
            monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
            monkeypatch.setattr(config, "STATE_DB", state_db)
            monkeypatch.setattr(config, "KEY_DIR", key_dir)
            monkeypatch.setattr(store, "DB_PATH", state_db)

            stop = threading.Event()
            approver_thread = _start_inproc_approver(stop)
            raw_transport = InProcessTransport(
                gateway_agent_id(prof["agent_id"], cust),
                cust,
                mandate=mandate_for(cust),
            )
            transport = RecordingInProcessTransport(raw_transport)

            try:
                agent = BuyerAgent(
                    transport,
                    cust,
                    use_gemini_parse=False,
                    poll_s=0.03,
                    approval_timeout_s=10.0,
                    answer_fn=gold_answer(ex),
                )
                assert agent.mock is True
                assert isinstance(agent._generate, MockModel)
                assert agent.use_gemini_parse is False
                assert agent.model == MOCK_MODEL_NAME

                res = agent.run(ex["request"])
                results_by_id[gid] = {
                    "result": res,
                    "calls": list(transport.calls),
                }

                scored = score(ex, res, allergen_lookup)
                scored.update(
                    {
                        "id": gid,
                        "segment": ex["segment"],
                        "lang": ex["lang"],
                        "customer_id": cust,
                    }
                )
                rows.append(scored)

                if res.get("quote") is not None or res.get("order") is not None:
                    audit_check = service.audit_verify()
                    assert audit_check["ok"] is True, (gid, audit_check)

                if res.get("status") == "placed":
                    placed_orders = store.list_orders(status="placed")
                    assert len(placed_orders) == 1, gid
                    saved_quote = store.get_quote(placed_orders[0].quote_id)
                    assert saved_quote is not None, gid
                    assert signing.verify_quote(saved_quote) is True, gid
            finally:
                stop.set()
                approver_thread.join(timeout=2.0)
                transport.close()
                _close_state_db(state_db)
    finally:
        scorer_transport.close()

    summ = summarize(rows)
    assert summ["n"] == 20
    assert summ["errors"] == 0
    assert all(r["status"] != "error" for r in rows)

    assert summ["overall"]["allergen_safe"] == 1.0
    assert all(r["allergen_safe"] is True and r["unsafe"] == [] for r in rows)

    assert summ["overall"]["zero_correction"] >= 0.90
    assert summ["overall"]["escalation_match"] >= 0.70
    assert summ["by_segment"]["hotel"]["zero_correction"] == 1.0
    assert summ["by_segment"]["hotel"]["escalation_match"] == 1.0
    assert summ["by_segment"]["school"]["zero_correction"] == 1.0
    assert summ["by_segment"]["school"]["allergen_safe"] == 1.0
    assert summ["by_segment"]["event"]["zero_correction"] == 1.0

    by_id = {r["id"]: r for r in rows}

    for school_id in ("g06", "g07", "g17"):
        srow = by_id[school_id]
        assert srow["status"] == "placed"
        assert [c["product_id"] for c in srow["cart"]] == ["470889"]
        assert srow["total_net"] < 300.0
        assert srow["escalated"] is False

    g15 = by_id["g15"]
    g15_run = results_by_id["g15"]["result"]
    g15_calls = [name for name, _ in results_by_id["g15"]["calls"]]
    assert g15["status"] == "no_order"
    assert g15["cart"] == []
    assert g15["zero_correction"] is True
    assert g15["allergen_safe"] is True
    assert g15_run["quote"] is None
    assert g15_run["order"] is None
    assert (g15_run["plan"] or {}).get("lines") == []
    assert (g15_run["plan"] or {}).get("shortfall") == 100.0
    assert "plan_need" in g15_calls
    assert "create_quote" not in g15_calls
    assert "place_order" not in g15_calls

    g09 = by_id["g09"]
    g09_run = results_by_id["g09"]["result"]
    g09_calls = [name for name, _ in results_by_id["g09"]["calls"]]
    assert g09["status"] == "needs_confirmation"
    assert g09["zero_correction"] is True
    assert g09["escalation_match"] is True
    assert g09_run["quote"] is not None
    assert g09_run["order"] is None
    assert "create_quote" in g09_calls
    assert "place_order" not in g09_calls
    blocked_steps = [
        s for s in g09_run["steps"]
        if s["kind"] == "tool_call" and s["data"].get("name") == "place_order" and s["data"].get("blocked")
    ]
    assert len(blocked_steps) == 1

    g16 = by_id["g16"]
    g16_run = results_by_id["g16"]["result"]
    assert g16["asked"] is True
    assert g16["allergen_safe"] is True
    assert g16["escalation_match"] is True
    assert len(g16_run["clarifications"]) >= 1
    assert any(s["kind"] == "needs_clarification" for s in g16_run["steps"])

    for multilingual_id in ("g07", "g10", "g11", "g14"):
        row = by_id[multilingual_id]
        assert row["status"] == "placed", (multilingual_id, row)
        assert row["zero_correction"] is True, (multilingual_id, row)
        if multilingual_id != "g07":
            assert row["escalation_match"] is True, (multilingual_id, row)
        assert row["allergen_safe"] is True, (multilingual_id, row)
        assert len(row["cart"]) >= 1, (multilingual_id, row)

    for multi_item_id in ("g05", "g19"):
        row = by_id[multi_item_id]
        run_data = results_by_id[multi_item_id]["result"]
        call_names = [name for name, _ in results_by_id[multi_item_id]["calls"]]
        assert len(run_data["needs"]) == 2, multi_item_id
        assert call_names.count("plan_need") == 2, multi_item_id
        assert row["status"] == "placed", multi_item_id
        assert row["zero_correction"] is True, (multi_item_id, row)
        assert row["allergen_safe"] is True, multi_item_id


NON_POSITIVE_REQUESTS = [
    ("0 litres", "hotel-aurora", 0.0, "L"),
    ("-5 litres", "hotel-aurora", -5.0, "L"),
    ("0 litraa maitoa", "hotel-aurora", 0.0, "L"),
    ("-10 kg smör", "hotel-aurora", -10.0, "KG"),
    ("0 kpl", "school-tampere", 0.0, "PCS"),
    ("We need 0 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.", "hotel-aurora", 0.0, "L"),
    ("We need -5 litres of lactose-free milk by Monday.", "hotel-aurora", -5.0, "L"),
    ("Tarvitsemme 0 litraa maitoa keskiviikoksi.", "school-tampere", 0.0, "L"),
    ("Vi behöver -10 kg smör till måndag.", "hotel-aurora", -10.0, "KG"),
    ("Tilaa 0 kpl gluteenitonta leipää maanantaiksi.", "hospital-meilahti", 0.0, "PCS"),
]


@pytest.mark.parametrize("text,customer_id,expected_qty,expected_unit", NON_POSITIVE_REQUESTS)
def test_zero_and_negative_quantities_fail_parse_request_validation(
    text: str,
    customer_id: str,
    expected_qty: float,
    expected_unit: str,
) -> None:
    parsed_qty = parse_quantity(text)
    assert parsed_qty == (expected_qty, expected_unit)

    with pytest.raises(ValidationError) as exc_info:
        parse_request(text, customer_id, use_gemini=False)

    errors = exc_info.value.errors()
    assert any("quantity" in err.get("loc", ()) for err in errors), errors


@pytest.mark.parametrize(
    "text,customer_id",
    [
        ("0 litres", "hotel-aurora"),
        ("-5 litres", "hotel-aurora"),
        ("0 litraa maitoa", "hotel-aurora"),
        ("-10 kg smör", "hotel-aurora"),
        ("0 kpl", "school-tampere"),
        ("We need 0 litres of lactose-free milk by Monday.", "hotel-aurora"),
        ("We need -5 litres of lactose-free milk by Monday.", "hotel-aurora"),
        ("Tarvitaan 0 litraa laktoositonta maitoa maanantaiksi.", "hotel-aurora"),
        ("Vi behöver -10 kg smör till måndag.", "hotel-aurora"),
        ("Tilaa 0 kpl ruisleipää keskiviikoksi.", "hospital-meilahti"),
        ("40 L lactose-free cream and 0 kg lactose-free butter by Monday.", "hotel-aurora"),
        ("30 litraa maitoa ja -2 kg voita maanantaiksi.", "hotel-aurora"),
        ("20 liter havredryck och 0 liter mjölk till onsdag.", "school-tampere"),
    ],
)
def test_zero_and_negative_quantities_stop_buyer_agent_before_plan_need(
    text: str,
    customer_id: str,
    replay_fixture_catalog: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "state_guard.sqlite"
    key_dir = tmp_path / "keys_guard"
    key_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("AIMO_STATE_DB", str(state_db))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", key_dir)
    monkeypatch.setattr(store, "DB_PATH", state_db)

    prof = get_profile(customer_id)
    inner = InProcessTransport(
        gateway_agent_id(prof["agent_id"], customer_id),
        customer_id,
        mandate=mandate_for(customer_id),
    )
    spy = RecordingInProcessTransport(inner)
    try:
        agent = BuyerAgent(
            spy,
            customer_id,
            use_gemini_parse=False,
            poll_s=0.02,
            approval_timeout_s=2.0,
        )
        assert agent.mock is True
        res = agent.run(text)

        assert res["status"] == "error"
        assert res["plan"] is None
        assert res["quote"] is None
        assert res["order"] is None

        plan_calls = [args for name, args in spy.calls if name == "plan_need"]
        quote_calls = [args for name, args in spy.calls if name == "create_quote"]
        order_calls = [args for name, args in spy.calls if name == "place_order"]
        assert plan_calls == []
        assert quote_calls == []
        assert order_calls == []
        assert spy.calls == []
        assert store.list_orders() == []

        error_steps = [s for s in res["steps"] if s["kind"] == "error"]
        assert len(error_steps) >= 1
        assert "ValidationError" in error_steps[0]["title"]
        assert res["steps"][-1]["kind"] == "final"
        assert res["steps"][-1]["data"]["status"] == "error"
    finally:
        spy.close()
        _close_state_db(state_db)
