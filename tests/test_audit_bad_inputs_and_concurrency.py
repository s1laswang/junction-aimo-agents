from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading
from types import MappingProxyType
from typing import Any

from fastapi.testclient import TestClient
import pytest

from aimo import config
from aimo.backend import orders as store
from aimo.models import Need, Product, Quote, QuoteLine
from aimo.trust import signing
from aimo.trust.audit import MAX_ID_LEN, AuditLog, actor_text
from aimo.trust.policy import evaluate, get_mandate
from conformance import verify as conformance_verify


class _ExplodingStr:
    def __str__(self) -> str:
        raise RuntimeError("unprintable value")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store.close_all()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    for k in ("AIMO_NOW", "AIMO_NOW_FROZEN", "AIMO_REPLAY", "AIMO_REPLAY_CLOCK", "AIMO_ENV", "AIMO_FAULTS"):
        monkeypatch.delenv(k, raising=False)
    yield
    store.close_all()


@pytest.fixture
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.sqlite")


@pytest.mark.parametrize(
    "bad_val",
    [
        None,
        True,
        False,
        0,
        123,
        3.14,
        [],
        {},
        b"bytes",
        "",
        "   ",
        "\t\n",
        "bad\x00id",
        "\x00",
        "x" * (MAX_ID_LEN + 1),
    ],
)
def test_append_rejects_invalid_actor_action_and_subject_id(log: AuditLog, bad_val: Any) -> None:
    with pytest.raises(ValueError):
        log.append(bad_val, "test.action", "S-1", "Valid summary")
    with pytest.raises(ValueError):
        log.append("system:test", bad_val, "S-1", "Valid summary")
    with pytest.raises(ValueError):
        log.append("system:test", "test.action", bad_val, "Valid summary")
    assert log.events() == []
    assert log.verify()["ok"] is True


@pytest.mark.parametrize(
    "bad_summary",
    [
        None,
        True,
        False,
        0,
        42,
        [],
        {},
        b"bytes",
        "",
        "   ",
        "\x00",
        " \x00 \t ",
    ],
)
def test_append_rejects_invalid_or_blank_summary_and_strips_nul_bytes(log: AuditLog, bad_summary: Any) -> None:
    with pytest.raises(ValueError):
        log.append("system:test", "test.action", "S-1", bad_summary)
    assert log.events() == []

    ev = log.append("  system:test  ", "  test.action  ", "  S-1  ", "Recorded\x00 audit\x00 event.")
    assert ev.actor == "system:test"
    assert ev.action == "test.action"
    assert ev.subject_id == "S-1"
    assert ev.summary == "Recorded audit event."
    assert "\x00" not in ev.summary
    assert log.verify()["ok"] is True


def test_append_sanitizes_non_dict_data_nan_inf_circular_and_unserializable_values(
    log: AuditLog, tmp_path: Path
) -> None:
    non_dict_inputs: list[tuple[Any, dict[str, Any]]] = [
        (None, {}),
        ([1, 2, 3], {"value": [1, 2, 3]}),
        (("a", 1, 2), {"value": ["a", 1, 2]}),
        ("raw-string", {"value": "raw-string"}),
        (42, {"value": 42}),
        (False, {"value": False}),
        (float("nan"), {"value": None}),
        (float("inf"), {"value": None}),
    ]
    for idx, (raw_data, expected) in enumerate(non_dict_inputs):
        ev = log.append("system:test", "data.nondict", f"S-nd-{idx}", "Non-dict payload", data=raw_data)
        assert ev.data == expected

    need = Need(
        customer_id="hotel-aurora",
        product_query="lactose-free milk",
        quantity=10,
        unit="L",
        delivery_by=date(2026, 11, 16),
    )
    ev_model = log.append("system:test", "data.model", "S-model", "Pydantic payload", data=need)
    assert ev_model.data["customer_id"] == "hotel-aurora"

    circular: dict[str, Any] = {"label": "loop"}
    circular["self"] = circular
    circular_list: list[Any] = []
    circular_list.append(circular_list)

    hostile_data: Any = MappingProxyType(
        {
            "nan_val": float("nan"),
            "pos_inf": float("inf"),
            "neg_inf": float("-inf"),
            "dec_nan": Decimal("NaN"),
            "dec_inf": Decimal("Infinity"),
            "dec_ok": Decimal("19.75"),
            "dt": datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc),
            "day": date(2026, 11, 16),
            "raw_bytes": b"hello\x00bytes",
            "mem": memoryview(b"mv"),
            "set_vals": {"b", "a"},
            "nul\x00key": "val\x00clean",
            "surrogate": "bad\ud800char",
            "path": tmp_path / "sample.txt",
            "custom_obj": object(),
            "exploding": _ExplodingStr(),
            "circular_dict": circular,
            "circular_list": circular_list,
            "total_net": float("nan"),
            "customer_id": "hotel-aurora",
        }
    )
    ev_hostile = log.append("system:test", "order.placed", "ORD-hostile", "Hostile order data", data=hostile_data)
    assert ev_hostile.data["nan_val"] is None
    assert ev_hostile.data["pos_inf"] is None
    assert ev_hostile.data["neg_inf"] is None
    assert ev_hostile.data["dec_nan"] is None
    assert ev_hostile.data["dec_inf"] is None
    assert ev_hostile.data["dec_ok"] == 19.75
    assert ev_hostile.data["raw_bytes"] == "hellobytes"
    assert ev_hostile.data["mem"] == "mv"
    assert ev_hostile.data["set_vals"] == ["a", "b"]
    assert ev_hostile.data["nulkey"] == "valclean"
    assert ev_hostile.data["circular_dict"]["self"] == "[circular]"
    assert ev_hostile.data["circular_list"] == ["[circular]"]
    assert ev_hostile.data["exploding"] == "<_ExplodingStr>"

    v = log.verify()
    assert v["ok"] is True and v["checked"] == len(non_dict_inputs) + 2

    dumped = [e.model_dump(mode="json") for e in log.events()]
    ext_v = conformance_verify.verify_events(dumped, signing.jwks(), contiguous=True)
    assert ext_v["ok"] is True, ext_v

    now = datetime(2026, 11, 14, 12, 5, tzinfo=timezone.utc)
    q = signing.sign_quote(
        Quote(
            quote_id="Q-after-nan",
            customer_id="hotel-aurora",
            agent_id="hotel-aurora/buyer-1",
            delivery_date=date(2026, 11, 16),
            lines=[
                QuoteLine(
                    product_id="464306",
                    product_name="Milk 1L",
                    qty_sales_units=10,
                    sales_unit="ST",
                    unit_price=1.0,
                    line_total=10.0,
                    tier="preferred",
                )
            ],
            total_net=10.0,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )
    )
    prod = Product(id="464306", name="Milk 1L", sales_unit="ST", category="10400", content_amount=1.0, content_unit="L")
    dec = evaluate(q, get_mandate("hotel-aurora", "buyer-1"), products={"464306": prod}, now=now, audit_log=log)
    assert dec.outcome in ("auto_approve", "needs_approval")


def test_append_related_ids_bare_string_and_invalid_item_filtering(log: AuditLog) -> None:
    log.append("system:test", "quote.created", "Q-123", "Quote 123")
    log.append("system:test", "quote.created", "Q-456", "Quote 456")

    ev_bare = log.append("system:test", "order.created", "ORD-123", "Order linked by bare str", related_ids="Q-123")
    assert ev_bare.data["related_ids"] == ["Q-123"]

    ev_data_bare = log.append(
        "system:test",
        "order.updated",
        "ORD-123",
        "Order linked by data bare str",
        data={
            "related_ids": "Q-456",
        },
        related_ids=[
            None,
            True,
            False,
            123,
            {},
            [],
            b"Q-bytes",
            "",
            "   ",
            "bad\x00link",
            "x" * (MAX_ID_LEN + 1),
            "ORD-123",
            "  Q-123  ",
        ],
    )
    assert ev_data_bare.data["related_ids"] == ["Q-456", "Q-123"]

    for non_iter_rel in (None, True, False, 123, {"a": 1}, b"raw"):
        ev_clean = log.append(
            "system:test",
            "audit.note",
            "S-clean",
            "Ignored non-iterable related_ids",
            data={"related_ids": non_iter_rel},
            related_ids=non_iter_rel,
        )
        assert "related_ids" not in ev_clean.data

    con = sqlite3.connect(log.db_path)
    try:
        links = [r[0] for r in con.execute("SELECT subject_id FROM audit_links ORDER BY rowid").fetchall()]
    finally:
        con.close()
    assert links == ["Q-123", "Q-456", "Q-123"]
    assert "Q" not in links and "-" not in links
    assert log.related_subjects("ORD-123") == {"ORD-123", "Q-123", "Q-456"}
    assert log.verify()["ok"] is True


@pytest.mark.parametrize(
    ("tamper_col", "tamper_val"),
    [
        ("data", "{bad json"),
        ("data", "[1, 2, 3]"),
        ("data", "null"),
        ("data", "123"),
        ("data", '"bare-string"'),
        ("data", "true"),
        ("data", '{"amount": NaN}'),
        ("data", '{"amount": Infinity}'),
        ("data", 99999),
        ("data", b"\xff\xfe"),
        ("ts", "not-a-timestamp"),
        ("actor", 12345),
    ],
)
def test_corrupt_sqlite_rows_caught_cleanly_by_verify_and_readers_never_crash(
    tmp_path: Path, tamper_col: str, tamper_val: Any
) -> None:
    db = tmp_path / f"corrupt_{tamper_col}_{ abs(hash(repr(tamper_val))) }.sqlite"
    log = AuditLog(db)
    log.append("system:test", "quote.created", "S-corrupt", "First valid event", data={"value": [1, 2, 3]})
    log.append("system:test", "order.placed", "S-corrupt", "Second event to corrupt", data={"value": [1, 2, 3]})

    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("DROP TRIGGER IF EXISTS audit_no_update")
        con.execute(f"UPDATE audit_events SET {tamper_col} = ? WHERE seq = 2", (tamper_val,))
    finally:
        con.close()

    v = log.verify()
    assert v["ok"] is False
    assert v["checked"] == 1
    assert v["broken_at"] == 2
    assert isinstance(v["reason"], str) and v["reason"]

    all_evs = log.events()
    assert len(all_evs) == 2
    assert isinstance(all_evs[1].data, dict)

    sub_evs = log.events("S-corrupt")
    assert len(sub_evs) == 2

    act_evs = log.events_by_action("order.placed")
    assert len(act_evs) == 1

    md = log.render("S-corrupt")
    assert "CHAIN BROKEN at event \x232" in md

    pack = log.evidence_pack("S-corrupt")
    assert pack["chain"]["ok"] is False
    assert pack["chain"]["broken_at"] == 2
    assert len(pack["events"]) == 2


def test_http_audit_routes_never_500_when_sqlite_row_has_corrupt_or_non_dict_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aimo.gateway.app import create_app

    monkeypatch.setenv("AIMO_APPROVER_PIN", "test-pin")
    default_log = AuditLog()
    default_log.append("system:gateway", "quote.created", "Q-http-1", "Issued quote")
    default_log.append("system:gateway", "order.placed", "Q-http-1", "Placed order")

    con = sqlite3.connect(config.STATE_DB, isolation_level=None)
    try:
        con.execute("DROP TRIGGER IF EXISTS audit_no_update")
        con.execute("UPDATE audit_events SET data = ? WHERE seq = 1", ("{broken",))
        con.execute("UPDATE audit_events SET data = ? WHERE seq = 2", ("[1,2]",))
    finally:
        con.close()

    api = TestClient(create_app(with_mcp=False), raise_server_exceptions=False)
    r_verify = api.get("/v1/audit/verify")
    assert r_verify.status_code == 200
    assert r_verify.json()["ok"] is False and r_verify.json()["broken_at"] == 1

    r_list = api.get("/v1/audit")
    assert r_list.status_code == 200 and len(r_list.json()) == 2

    r_sub = api.get("/v1/audit", params={"subject_id": "Q-http-1"})
    assert r_sub.status_code == 200 and len(r_sub.json()) == 2

    r_rep = api.get("/v1/audit/report/Q-http-1")
    assert r_rep.status_code == 200
    assert "CHAIN BROKEN at event \x231" in r_rep.text


@pytest.mark.parametrize(
    "bad_query",
    [
        True,
        False,
        0,
        123,
        -99,
        3.14,
        [],
        {},
        b"Q-1",
        "",
        "   ",
        "Q-\x00-1",
        "\x00",
        "x" * (MAX_ID_LEN + 1),
        "x" * 10000,
    ],
)
def test_query_methods_guard_bad_subject_id_and_action_without_interface_error(
    log: AuditLog, bad_query: Any
) -> None:
    log.append("system:test", "quote.created", "Q-valid", "Valid event")
    max_len_id = "S" * MAX_ID_LEN
    log.append("system:test", "quote.created", max_len_id, "Boundary 256-char subject")
    assert len(log.events(max_len_id)) == 1

    assert log.related_subjects(bad_query) == set()
    assert log.events(bad_query) == []
    assert log.events_by_action(bad_query) == []

    md = log.render(bad_query)
    assert "_No events recorded for this subject._" in md
    assert "Valid event" not in md

    pack = log.evidence_pack(bad_query)
    assert pack["events"] == []
    assert pack["quote"] is None
    assert pack["decision"] is None

    assert log.events_by_action(None) == []
    assert "_No events recorded for this subject._" in log.render(None)
    assert log.evidence_pack(None)["events"] == []
    assert len(log.events(None)) == 2


def test_render_guards_none_outcome_non_str_approver_role_and_malformed_hits(log: AuditLog) -> None:
    log.append(
        "system:policy-engine",
        "policy.evaluated",
        "ORD-render-1",
        "Evaluated policy with null fields",
        data={
            "decision": {
                "outcome": None,
                "approver_role": None,
                "policy_id": None,
                "policy_version": None,
                "hits": None,
            }
        },
    )
    md_null = log.render("ORD-render-1")
    assert "\x23\x23 Policy decision: ?" in md_null
    assert "(approver:" not in md_null

    log.append(
        "system:policy-engine",
        "policy.evaluated",
        "ORD-render-2",
        "Evaluated policy with hostile hits",
        data={
            "decision": {
                "outcome": 123,
                "approver_role": True,
                "policy_id": "hotel-aurora",
                "policy_version": 2,
                "hits": [
                    None,
                    "not-a-dict",
                    42,
                    {},
                    {"outcome": ["unhashable"], "rule_id": None, "message": None},
                    {"outcome": "reject", "rule_id": "spend", "message": 9001},
                    {"outcome": "allow", "rule_id": "allergens:464306", "message": "milk | free"},
                ],
            },
            "quote": {"quote_id": "Q-bad-hash", "quote_hash": 12345},
        },
    )
    log.append(
        "human:anna",
        "approval.verified",
        "ORD-render-2",
        "Weird approval metadata",
        data={"approver": True, "approver_role": 999, "note": ["bad"]},
    )
    log.append(
        "human:anna",
        "approval.verified",
        "ORD-render-2",
        "Valid approver with non-str role",
        data={"approver": "anna@hotel-aurora.example", "approver_role": 999, "decision": "approve", "note": 123, "quote_hash": None},
    )
    md_hostile = log.render("ORD-render-2")
    assert "\x23\x23 Policy decision: ?" in md_hostile
    assert "| `?` | ? |  |" in md_hostile
    assert "| `spend` | **REJECT** | 9001 |" in md_hostile
    assert "| `allergens:464306` | pass | milk / free |" in md_hostile
    assert "Approved by **anna@hotel-aurora.example** at" in md_hostile
    assert "**True**" not in md_hostile
    assert "Chain verified:" in md_hostile


def test_actor_text_and_recreated_db_path_reinitializes_cleanly(tmp_path: Path) -> None:
    for bad_actor in (None, True, False, 123, [], {}):
        assert actor_text(bad_actor) == ""
    assert actor_text("agent:buyer-1") == "Buyer agent buyer-1"
    assert actor_text("human:anna") == "anna"
    assert actor_text("system:gateway") == "System (gateway)"

    db_file = tmp_path / "recreate.sqlite"
    log = AuditLog(db_file)
    log.append("system:test", "evt", "S-1", "Before unlink")
    assert len(log.events()) == 1

    for p in tmp_path.glob("recreate.sqlite*"):
        p.unlink()

    log.append("system:test", "evt", "S-2", "After unlink")
    evs = log.events()
    assert len(evs) == 1 and evs[0].subject_id == "S-2"
    assert log.verify()["ok"] is True


def test_sixteen_thread_concurrent_append_events_verify_and_render_stress(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "stress16.sqlite")
    num_threads = 16
    rounds_per_thread = 8
    barrier = threading.Barrier(num_threads)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker(tid: int) -> None:
        try:
            barrier.wait(timeout=15)
            quote_id = f"Q-T{tid:02d}"
            order_id = f"ORD-T{tid:02d}"
            for r in range(rounds_per_thread):
                subj = quote_id if r % 2 == 0 else order_id
                rel = quote_id if subj == order_id else ()
                payload: Any = (
                    {
                        "round": r,
                        "metric": float("nan") if r == 3 else float(r) * 1.5,
                        "decision": {
                            "outcome": "needs_approval" if r % 3 == 0 else "auto_approve",
                            "approver_role": "procurement_manager" if r % 3 == 0 else None,
                            "policy_id": "hotel-aurora",
                            "policy_version": "1.0.0",
                            "hits": [{"rule_id": "spend", "outcome": "allow", "message": "within cap"}],
                        },
                    }
                    if r % 2 == 1
                    else [tid, r]
                )
                log.append(
                    f"agent:hotel-aurora/buyer-{tid}",
                    "policy.evaluated" if r % 2 == 1 else "quote.created",
                    subj,
                    f"Thread {tid} round {r}\x00 recorded.",
                    data=payload,
                    related_ids=rel,
                )
                evs = log.events(order_id)
                assert isinstance(evs, list)
                by_act = log.events_by_action("quote.created")
                assert len(by_act) >= 1
                v = log.verify()
                assert v["ok"] is True, v
                md = log.render(order_id)
                assert "Audit trail for" in md and "CHAIN BROKEN" not in md
                if r == rounds_per_thread - 1:
                    pack = log.evidence_pack(order_id)
                    assert pack["chain"]["ok"] is True
        except BaseException as exc:
            with err_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert all(not t.is_alive() for t in threads)
    assert errors == [], errors

    final_v = log.verify()
    assert final_v["ok"] is True
    assert final_v["checked"] == num_threads * rounds_per_thread

    all_dumped = [e.model_dump(mode="json") for e in log.events()]
    assert len(all_dumped) == num_threads * rounds_per_thread
    ext_check = conformance_verify.verify_events(all_dumped, signing.jwks(), contiguous=True)
    assert ext_check["ok"] is True, ext_check
