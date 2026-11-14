import base64
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from joserfc import jws
from joserfc.jwk import OKPKey
import pytest

from aimo import audit, keys
from aimo.app import app
from aimo.models import AuditEvent

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

HELSINKI = ZoneInfo("Europe/Helsinki")
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AURORA_KEY = {"X-Agent-Key": "demo-hotel-aurora"}
TAYS_KEY = {"X-Agent-Key": "demo-hospital-tays"}
ADMIN_KEY = {"X-Admin-Key": "demo-admin"}


class McpSession:
    def __init__(self, client: TestClient):
        self.client = client
        self.sid = None
        self.seq = 0
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "audit-unit", "version": "1.0"},
            },
        )
        resp = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**MCP_HEADERS, "mcp-session-id": self.sid},
        )
        assert resp.status_code in (200, 202, 204), resp.text

    def rpc(self, method: str, params: dict | None = None, headers: dict | None = None) -> dict:
        self.seq += 1
        req_headers = dict(MCP_HEADERS)
        if self.sid and method != "initialize":
            req_headers["mcp-session-id"] = self.sid
        if headers:
            req_headers.update(headers)
        resp = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}},
            headers=req_headers,
        )
        assert resp.status_code == 200, resp.text
        self.sid = resp.headers.get("mcp-session-id", self.sid)
        body = resp.text
        if resp.headers["content-type"].startswith("text/event-stream"):
            body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
        return json.loads(body)

    def call_ok(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        res = out["result"]
        assert not res.get("isError"), res
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        blocks = [json.loads(b["text"]) for b in res.get("content", [])]
        assert len(blocks) == 1, res
        return blocks[0]

    def call_err(self, name: str, arguments: dict | None = None, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
        res = out["result"]
        assert res.get("isError") is True, res
        text = " ".join(b["text"] for b in res.get("content", []))
        prefix = f"Error executing tool {name}: "
        return json.loads(text.split(prefix, 1)[-1])


@pytest.fixture(autouse=True)
def isolate_audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "audit_unit.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nodata"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)
    monkeypatch.delenv("AIMO_STDIO_ADMIN_KEY", raising=False)
    keys._cache.clear()
    yield
    keys._cache.clear()


@pytest.fixture
def gateway():
    with TestClient(app) as client:
        yield client, McpSession(client)


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _seed_six_events() -> list[AuditEvent]:
    return [
        audit.record(
            "hotel-aurora",
            "agent:buyer-aurora",
            "parse.request",
            "hotel-aurora",
            "Parsed 120 l lactose-free milk.",
            {"text": "120 l lactose-free milk", "qty": 120.0},
        ),
        audit.record(
            "hotel-aurora",
            "agent:buyer-aurora",
            "quote.issued",
            "q_100",
            "Issued signed quote q_100.",
            {"quote_id": "q_100", "total_net": 180.0, "total_gross": 205.2, "lines": [10.0, 2.5]},
        ),
        audit.record(
            "hotel-aurora",
            "agent:buyer-aurora",
            "quote.redeemed",
            "q_100",
            "Redeemed signed quote q_100.",
            {"quote_id": "q_100", "total_net": 180.0, "total_gross": 205.2},
        ),
        audit.record(
            "hotel-aurora",
            "agent:buyer-aurora",
            "order.placed",
            "ord_100",
            "Order total is within per-order mandate.",
            {"order_id": "ord_100", "quote_id": "q_100", "total_net": 180.0},
        ),
        audit.record(
            "hotel-aurora",
            "system",
            "order.approved",
            "ord_100",
            "Order total 180.00 EUR is under the 2,500 EUR limit.",
            {"order_id": "ord_100"},
        ),
        audit.record(
            "hospital-tays",
            "system",
            "order.confirmed",
            "ord_200",
            "Mock confirmation, delivery 2026-11-17.",
            {"order_id": "ord_200", "confirmation_id": "MOCK-200", "mock": True},
        ),
    ]


def test_empty_audit_db_verify_checkpoint_rest_and_mcp(gateway):
    client, mcp = gateway

    assert audit.events() == []
    assert audit.verify() == (True, None)
    assert audit.narrate([]) == ""
    assert audit.narrate([], view="valio") == ""

    cp = audit.checkpoint()
    assert cp["seq"] == 0
    assert cp["hash"] == "0" * 64
    assert cp["ts"] is None
    assert isinstance(cp["jws"], str)
    assert len(cp["jws"].split(".")) == 3

    verified_cp = audit.verify_checkpoint(cp["jws"])
    assert verified_cp == {"seq": 0, "hash": "0" * 64, "ts": None}

    parsed_jws = jws.deserialize_compact(cp["jws"], keys.signing_key(), algorithms=["EdDSA"])
    assert parsed_jws.headers() == {"alg": "EdDSA", "kid": keys.kid()}
    assert json.loads(parsed_jws.payload) == {"seq": 0, "hash": "0" * 64, "ts": None}

    rest_admin = client.get("/v1/audit", headers=ADMIN_KEY)
    assert rest_admin.status_code == 200
    body = rest_admin.json()
    assert body["events"] == []
    assert body["text"] == ""
    assert body["verified"] == {"ok": True, "bad_seq": None}
    assert body["checkpoint"]["seq"] == 0
    assert body["checkpoint"]["hash"] == "0" * 64
    assert body["checkpoint"]["ts"] is None
    assert audit.verify_checkpoint(body["checkpoint"]["jws"]) == {"seq": 0, "hash": "0" * 64, "ts": None}

    mcp_body = mcp.call_ok("get_audit_trail", {}, headers=AURORA_KEY)
    assert mcp_body["events"] == []
    assert mcp_body["text"] == ""
    assert mcp_body["verified"] == {"ok": True, "bad_seq": None}
    assert mcp_body["checkpoint"]["seq"] == 0
    assert mcp_body["checkpoint"]["hash"] == "0" * 64
    assert mcp_body["checkpoint"]["ts"] is None
    assert audit.verify_checkpoint(mcp_body["checkpoint"]["jws"]) == {"seq": 0, "hash": "0" * 64, "ts": None}

    unauth_rest = client.get("/v1/audit")
    assert unauth_rest.status_code == 401
    assert unauth_rest.json()["code"] == "unauthorized"

    forbid_rest = client.get("/v1/audit?customer=hospital-tays", headers=AURORA_KEY)
    assert forbid_rest.status_code == 403
    assert forbid_rest.json()["code"] == "forbidden"

    unauth_mcp = mcp.call_err("get_audit_trail", {})
    assert unauth_mcp["code"] == "unauthorized"

    forbid_mcp = mcp.call_err("get_audit_trail", {"customer": "hospital-tays"}, headers=AURORA_KEY)
    assert forbid_mcp["code"] == "forbidden"


def test_multi_event_chain_links_hashes_and_norm_float_canonicalization():
    assert audit._norm(10.0) == 10 and type(audit._norm(10.0)) is int
    assert audit._norm(0.0) == 0 and type(audit._norm(0.0)) is int
    assert audit._norm(-7.0) == -7 and type(audit._norm(-7.0)) is int
    assert audit._norm(10.25) == 10.25 and type(audit._norm(10.25)) is float
    assert audit._norm(True) is True
    assert audit._norm(False) is False
    assert audit._norm({"a": 10.0, "b": [1.0, 1.5, {"c": 99.0}]}) == {"a": 10, "b": [1, 1.5, {"c": 99}]}

    evs = _seed_six_events()
    assert [e.seq for e in evs] == [1, 2, 3, 4, 5, 6]
    assert evs[0].prev_hash == "0" * 64
    for idx in range(1, len(evs)):
        assert evs[idx].prev_hash == evs[idx - 1].hash

    loaded = audit.events()
    assert [e.seq for e in loaded] == [1, 2, 3, 4, 5, 6]
    assert loaded[0].detail == {"text": "120 l lactose-free milk", "qty": 120}
    assert type(loaded[0].detail["qty"]) is int
    assert loaded[1].detail["lines"] == [10, 2.5]
    assert type(loaded[1].detail["lines"][0]) is int
    assert type(loaded[1].detail["lines"][1]) is float

    prev = "0" * 64
    for e in loaded:
        canonical = json.dumps(
            {
                "action": e.action,
                "actor": e.actor,
                "customer": e.customer,
                "detail": e.detail,
                "seq": e.seq,
                "subject": e.subject,
                "ts": e.ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "why": e.why,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_hash = hashlib.sha256((prev + canonical).encode()).hexdigest()
        assert e.prev_hash == prev
        assert e.hash == expected_hash
        prev = e.hash

    assert audit.verify() == (True, None)

    ev_date = audit.record(
        "hotel-aurora",
        "system",
        "order.placed",
        "ord_date",
        "Check date serialization and float normalization.",
        {"delivery": date(2026, 11, 18), "units": 10.0, "nested": {"count": 3.0, "frac": 3.75}},
    )
    assert ev_date.seq == 7
    assert ev_date.detail == {"delivery": "2026-11-18", "units": 10, "nested": {"count": 3, "frac": 3.75}}
    assert audit.verify() == (True, None)

    con = sqlite3.connect(os.environ["AIMO_DB"])
    try:
        con.execute(
            "update audit set detail=? where seq=7",
            ('{"delivery": "2026-11-18", "nested": {"count": 3.0, "frac": 3.75}, "units": 10.0}',),
        )
        con.commit()
    finally:
        con.close()
    assert audit.verify() == (True, None)

    with pytest.raises(sqlite3.IntegrityError):
        audit.record(None, "system", "order.placed", "ord_bad", "Null customer must rollback.")
    ev_after_rollback = audit.record("hotel-aurora", "system", "order.placed", "ord_after", "Chain continues cleanly.")
    assert ev_after_rollback.seq == 8
    assert audit.verify() == (True, None)


@pytest.mark.parametrize(
    ("column", "tampered_value"),
    [
        ("why", "Tampered explanation"),
        ("detail", '{"quote_id": "q_100", "total_gross": 205.2, "total_net": 999}'),
        ("actor", "agent:rogue-actor"),
        ("action", "order.confirmed"),
        ("customer", "school-hervanta"),
        ("subject", "q_tampered"),
        ("ts", "2099-12-31T23:59:59Z"),
        ("prev_hash", "f" * 64),
        ("hash", "a" * 64),
    ],
)
def test_tamper_each_column_on_seq_3_pinpointed_in_verify_rest_and_mcp(gateway, column, tampered_value):
    client, mcp = gateway
    _seed_six_events()
    assert audit.verify() == (True, None)

    con = sqlite3.connect(os.environ["AIMO_DB"])
    try:
        con.execute(f"update audit set {column}=? where seq=3", (tampered_value,))
        con.commit()
    finally:
        con.close()

    assert audit.verify() == (False, 3)

    rest_admin = client.get("/v1/audit", headers=ADMIN_KEY)
    assert rest_admin.status_code == 200
    assert rest_admin.json()["verified"] == {"ok": False, "bad_seq": 3}

    rest_tays = client.get("/v1/audit", headers=TAYS_KEY)
    assert rest_tays.status_code == 200
    assert rest_tays.json()["verified"] == {"ok": False, "bad_seq": 3}

    mcp_trail = mcp.call_ok("get_audit_trail", {}, headers=AURORA_KEY)
    assert mcp_trail["verified"] == {"ok": False, "bad_seq": 3}


def test_recomputed_row_hash_caught_at_next_link_and_deleted_row_gap_caught(gateway):
    client, mcp = gateway
    evs = _seed_six_events()
    assert len(evs) == 6
    assert audit.verify() == (True, None)

    con = sqlite3.connect(os.environ["AIMO_DB"])
    try:
        r3 = con.execute("select * from audit where seq=3").fetchone()
        forged_why = "Rehashed tampered row"
        row_dict = {
            "seq": r3[0],
            "ts": r3[1],
            "customer": r3[2],
            "actor": r3[3],
            "action": r3[4],
            "subject": r3[5],
            "detail": audit._norm(json.loads(r3[6])),
            "why": forged_why,
        }
        forged_hash = audit._digest(r3[8], row_dict)
        con.execute("update audit set why=?, hash=? where seq=3", (forged_why, forged_hash))
        con.commit()
    finally:
        con.close()

    assert audit.verify() == (False, 4)
    assert client.get("/v1/audit", headers=ADMIN_KEY).json()["verified"] == {"ok": False, "bad_seq": 4}
    assert mcp.call_ok("get_audit_trail", {}, headers=ADMIN_KEY)["verified"] == {"ok": False, "bad_seq": 4}


def test_deleting_seq_2_gap_returns_false_3_and_deleting_seq_1_returns_false_2(gateway, tmp_path, monkeypatch):
    client, mcp = gateway
    for idx in range(1, 5):
        audit.record("hotel-aurora", "system", "order.placed", f"ord_{idx}", f"Step {idx}.", {"step": idx})
    assert [e.seq for e in audit.events()] == [1, 2, 3, 4]
    assert audit.verify() == (True, None)

    con = sqlite3.connect(os.environ["AIMO_DB"])
    try:
        con.execute("delete from audit where seq=2")
        con.commit()
    finally:
        con.close()

    assert [e.seq for e in audit.events()] == [1, 3, 4]
    assert audit.verify() == (False, 3)
    assert client.get("/v1/audit", headers=ADMIN_KEY).json()["verified"] == {"ok": False, "bad_seq": 3}
    assert mcp.call_ok("get_audit_trail", {}, headers=AURORA_KEY)["verified"] == {"ok": False, "bad_seq": 3}

    db_first_gap = tmp_path / "gap_seq_1.db"
    monkeypatch.setenv("AIMO_DB", str(db_first_gap))
    for idx in range(1, 4):
        audit.record("hotel-aurora", "system", "order.placed", f"ord_{idx}", f"Step {idx}.")
    con2 = sqlite3.connect(db_first_gap)
    try:
        con2.execute("delete from audit where seq=1")
        con2.commit()
    finally:
        con2.close()
    assert [e.seq for e in audit.events()] == [2, 3]
    assert audit.verify() == (False, 2)


def test_events_order_id_links_quote_events_and_respects_customer_filter(gateway):
    client, mcp = gateway

    e_parse = audit.record("hotel-aurora", "agent:buyer-aurora", "parse.request", "hotel-aurora", "Asked for milk.")
    e_q1_issued = audit.record(
        "hotel-aurora",
        "agent:buyer-aurora",
        "quote.issued",
        "q_alpha",
        "Quote q_alpha issued.",
        {"quote_id": "q_alpha", "total_net": 2600.0, "total_gross": 2964.0},
    )
    e_q2_issued = audit.record(
        "hospital-tays",
        "agent:buyer-tays",
        "quote.issued",
        "q_beta",
        "Quote q_beta issued.",
        {"quote_id": "q_beta", "total_net": 400.0, "total_gross": 456.0},
    )
    e_q1_redeemed = audit.record(
        "hotel-aurora",
        "agent:buyer-aurora",
        "quote.redeemed",
        "q_alpha",
        "Quote q_alpha redeemed.",
        {"quote_id": "q_alpha", "total_net": 2600.0, "total_gross": 2964.0},
    )
    e_o1_placed = audit.record(
        "hotel-aurora",
        "agent:buyer-aurora",
        "order.placed",
        "ord_alpha",
        "Order placed from q_alpha.",
        {"order_id": "ord_alpha", "quote_id": "q_alpha", "total_net": 2600.0},
    )
    e_o1_pending = audit.record(
        "hotel-aurora",
        "system",
        "order.pending_approval",
        "ord_alpha",
        "Held for Laura Virtanen.",
        {"order_id": "ord_alpha", "reasons": [{"code": "OVER_ORDER_LIMIT", "text": "Over 2,500 EUR limit."}]},
    )
    e_q2_redeemed = audit.record(
        "hospital-tays",
        "agent:buyer-tays",
        "quote.redeemed",
        "q_beta",
        "Quote q_beta redeemed.",
        {"quote_id": "q_beta", "total_net": 400.0, "total_gross": 456.0},
    )
    e_o2_placed = audit.record(
        "hospital-tays",
        "agent:buyer-tays",
        "order.placed",
        "ord_beta",
        "Order ord_beta placed.",
        {"order_id": "ord_beta", "quote_id": "q_beta", "total_net": 400.0},
    )
    e_o1_approved = audit.record(
        "hotel-aurora",
        "human:Laura Virtanen",
        "approval.approved",
        "ord_alpha",
        "Laura Virtanen approved ord_alpha.",
        {"order_id": "ord_alpha", "note": "approved"},
    )
    e_o1_confirmed = audit.record(
        "hotel-aurora",
        "system",
        "order.confirmed",
        "ord_alpha",
        "Mock confirmation, delivery 2026-11-16.",
        {"order_id": "ord_alpha", "confirmation_id": "MOCK-ALPHA", "mock": True},
    )
    e_o1_stockout = audit.record(
        "hotel-aurora",
        "system",
        "order.stockout",
        "hotel-aurora",
        "Stock dropped after confirmation.",
        {"order_id": "ord_alpha", "total_net": 2550.0},
    )
    e_mandate = audit.record(
        "hotel-aurora",
        "system",
        "mandate_updated",
        "hotel-aurora",
        "Updated mandate.",
        {"per_order_eur": 3000},
    )

    assert e_parse.seq == 1 and e_mandate.seq == 12
    expected_alpha_seqs = [
        e_q1_issued.seq,
        e_q1_redeemed.seq,
        e_o1_placed.seq,
        e_o1_pending.seq,
        e_o1_approved.seq,
        e_o1_confirmed.seq,
        e_o1_stockout.seq,
    ]
    expected_beta_seqs = [e_q2_issued.seq, e_q2_redeemed.seq, e_o2_placed.seq]

    alpha_events = audit.events(order_id="ord_alpha")
    assert [e.seq for e in alpha_events] == expected_alpha_seqs
    assert [e.action for e in alpha_events] == [
        "quote.issued",
        "quote.redeemed",
        "order.placed",
        "order.pending_approval",
        "approval.approved",
        "order.confirmed",
        "order.stockout",
    ]

    assert [e.seq for e in audit.events(customer="hotel-aurora", order_id="ord_alpha")] == expected_alpha_seqs
    assert audit.events(customer="hospital-tays", order_id="ord_alpha") == []
    assert [e.seq for e in audit.events(order_id="ord_beta")] == expected_beta_seqs
    assert audit.events(order_id="ord_missing") == []

    rest_alpha = client.get("/v1/audit?order_id=ord_alpha", headers=AURORA_KEY)
    assert rest_alpha.status_code == 200
    assert [e["seq"] for e in rest_alpha.json()["events"]] == expected_alpha_seqs

    rest_cross_empty = client.get("/v1/audit?order_id=ord_alpha", headers=TAYS_KEY)
    assert rest_cross_empty.status_code == 200
    assert rest_cross_empty.json()["events"] == []

    mcp_alpha = mcp.call_ok("get_audit_trail", {"order_id": "ord_alpha"}, headers=AURORA_KEY)
    assert [e["seq"] for e in mcp_alpha["events"]] == expected_alpha_seqs


def test_narrate_all_verbs_and_system_actions_across_buyer_and_valio_views():
    fixed_ts = datetime(2026, 11, 14, 10, 5, tzinfo=timezone.utc)
    expected_clock = fixed_ts.astimezone(HELSINKI).strftime("%H:%M")
    assert expected_clock == "12:05"

    def make_ev(seq: int, customer: str, actor: str, action: str, subject: str, why: str, detail: dict | None = None) -> AuditEvent:
        return AuditEvent(
            seq=seq,
            ts=fixed_ts,
            customer=customer,
            actor=actor,
            action=action,
            subject=subject,
            detail=detail or {},
            why=why,
            prev_hash="0" * 64,
            hash="1" * 64,
        )

    verb_cases = [
        ("quote.issued", "agent:buyer-aurora", "q_1", "Valid until cutoff.", {"total_gross": 1240.0}, "was issued a signed quote for 1,240.00 EUR. Valid until cutoff."),
        ("quote.redeemed", "agent:buyer-aurora", "q_1", "Quote q_1 redeemed for 1,240.00 EUR.", {"total_gross": 1240.0}, "redeemed a signed quote. Quote q_1 redeemed for 1,240.00 EUR."),
        ("order.draft", "agent:buyer-aurora", "ord_1", "Draft created.", {"amount": 95.5, "currency": "SEK"}, "started an order for 95.50 SEK. Draft created."),
        ("order.placed", "agent:buyer-aurora", "ord_1", "Within mandate.", {"total_net": 500.0}, "placed an order for 500.00 EUR. Within mandate."),
        (
            "order.pending_approval",
            "agent:buyer-aurora",
            "ord_1",
            "Requested review.",
            {"decision": {"reasons": [{"code": "AGENT_REQUESTED", "text": "VIP event check."}]}},
            "had an order held for human approval. Requested review. Reasons: VIP event check.",
        ),
        ("order.approved", "agent:buyer-aurora", "ord_1", "Auto check.", {}, "had an order approved. Auto check."),
        ("order.confirmed", "agent:buyer-aurora", "ord_1", "Supplier ack.", {}, "had an order confirmed by the supplier. Supplier ack."),
        ("order.rejected", "agent:buyer-aurora", "ord_1", "Supplier declined.", {}, "had an order rejected. Supplier declined."),
        (
            "order.blocked",
            "agent:buyer-aurora",
            "ord_1",
            "Blocked by policy.",
            {"reasons": ["Contains excluded allergen peanut", "Blocked by policy"]},
            "had an order blocked. Blocked by policy. Reasons: Contains excluded allergen peanut.",
        ),
        ("order.stockout", "agent:buyer-aurora", "ord_1", "Reopened after stock drop.", {}, "had an order reopened after a supplier stock-out. Reopened after stock drop."),
        ("parse.request", "agent:buyer-aurora", "hotel-aurora", "100 l milk.", {}, "asked for products. 100 l milk."),
        ("offers.compared", "agent:buyer-aurora", "hotel-aurora", "Compared 3 offers.", {}, "compared offers. Compared 3 offers."),
        ("order.expired", "agent:buyer-aurora", "ord_1", "Cutoff elapsed.", {}, "had an order expire. Cutoff elapsed."),
        ("approval.approved", "human:laura virtanen", "ord_1", "Approved for weekend.", {}, "approved an order. Approved for weekend."),
        ("approval.rejected", "human:mikko heikkinen", "ord_1", "Over budget.", {}, "rejected an order. Over budget."),
        ("mandate_updated", "system", "hotel-aurora", "Order limit set to 3,000 EUR.", {}, "had its buying mandate updated. Order limit set to 3,000 EUR."),
    ]

    covered_verbs = {c[0] for c in verb_cases} | {"order_rejected"}
    assert covered_verbs == set(audit.VERBS.keys())

    for idx, (action, actor, subject, why, detail, expected_tail) in enumerate(verb_cases, start=1):
        ev = make_ev(idx, "hotel-aurora", actor, action, subject, why, detail)
        buyer_line = audit.narrate([ev], view="buyer")
        valio_line = audit.narrate([ev], view="valio")

        if actor == "agent:buyer-aurora":
            assert buyer_line == f"12:05 Hotel Aurora's buyer agent {expected_tail}"
            assert valio_line == f"12:05 Hotel Aurora's buyer agent (buyer-aurora) {expected_tail} [{subject}, #{idx}]"
        elif actor == "human:laura virtanen":
            assert buyer_line == f"12:05 Laura Virtanen {expected_tail}"
            assert valio_line == f"12:05 Laura Virtanen at Hotel Aurora {expected_tail} [{subject}, #{idx}]"
        elif actor == "human:mikko heikkinen":
            assert buyer_line == f"12:05 Mikko Heikkinen {expected_tail}"
            assert valio_line == f"12:05 Mikko Heikkinen at Hotel Aurora {expected_tail} [{subject}, #{idx}]"
        else:
            assert buyer_line == f"12:05 The system {expected_tail}"
            assert valio_line == f"12:05 The system {expected_tail} [{subject}, #{idx}]"

    system_cases = [
        ("order.approved", "system", "ord_2", "Under 2,500 EUR limit.", {}, "12:05 Mandate check passed: Under 2,500 EUR limit."),
        (
            "order.pending_approval",
            "system",
            "ord_2",
            "Held for Helena Korhonen.",
            {"reasons": [{"code": "OVER_ORDER_LIMIT", "text": "Order exceeds 1,500 EUR."}]},
            "12:05 Order held for human approval: Held for Helena Korhonen. Reasons: Order exceeds 1,500 EUR.",
        ),
        (
            "order.blocked",
            "system",
            "ord_2",
            "Mandate forbids peanut.",
            {"reasons": [{"code": "ALLERGEN_BLOCKED", "text": "Mandate forbids peanut."}]},
            "12:05 Order blocked: Mandate forbids peanut.",
        ),
        ("order.confirmed", "system", "ord_2", "Mock confirmation, delivery 2026-11-16.", {}, "12:05 Order confirmed by Valio (mock). Delivery 2026-11-16."),
        ("order.confirmed", "system", "ord_2", "", {}, "12:05 Order confirmed by Valio (mock)."),
        ("order.expired", "system", "ord_2", "Quote validity ended.", {}, "12:05 Order expired: Quote validity ended."),
        ("order.rejected", "system", "ord_2", "Rejected automatically.", {}, "12:05 Order rejected: Rejected automatically."),
        ("order.stockout", "system", "ord_2", "Milk 464201 out of stock.", {}, "12:05 Supplier stock-out: Milk 464201 out of stock."),
        ("order_rejected", "agent:buyer-tays", "q_9", "Quote was already used.", {}, "12:05 Refused: Quote was already used."),
        ("order_rejected", "system", "q_9", "", {}, "12:05 Refused:"),
    ]
    assert {c[0] for c in system_cases} == set(audit.SYSTEM.keys())

    for idx, (action, actor, subject, why, detail, expected_buyer) in enumerate(system_cases, start=20):
        ev = make_ev(idx, "hospital-tays", actor, action, subject, why, detail)
        assert audit.narrate([ev], view="buyer") == expected_buyer
        assert audit.narrate([ev], view="valio") == f"{expected_buyer} [{subject}, #{idx}]"

    anon_agent = make_ev(40, "school-hervanta", "agent:", "parse.request", "school-hervanta", "")
    assert audit.narrate([anon_agent], view="buyer") == "12:05 School Hervanta's buyer agent asked for products."
    assert audit.narrate([anon_agent], view="valio") == "12:05 School Hervanta's buyer agent asked for products. [school-hervanta, #40]"

    anon_human = make_ev(41, "school-hervanta", "human:", "approval.approved", "ord_41", "")
    assert audit.narrate([anon_human], view="buyer") == "12:05 A person approved an order."
    assert audit.narrate([anon_human], view="valio") == "12:05 A person at School Hervanta approved an order. [ord_41, #41]"

    custom_ev = AuditEvent.model_construct(
        seq=42,
        ts="2026-11-14T10:05:00Z",
        customer="hotel-aurora",
        actor="agent:bot-1",
        action="custom.audit_step",
        subject="sub_42",
        detail={},
        why="Custom step completed",
        prev_hash="0" * 64,
        hash="1" * 64,
    )
    assert audit.narrate([custom_ev], view="buyer") == "12:05 Hotel Aurora's buyer agent custom audit step. Custom step completed."
    assert audit.narrate([custom_ev], view="valio") == "12:05 Hotel Aurora's buyer agent (bot-1) custom audit step. Custom step completed. [sub_42, #42]"


def test_narrate_deduplicates_human_order_approved_and_order_rejected_in_both_views():
    e1 = audit.record("hotel-aurora", "human:Laura Virtanen", "approval.approved", "ord_a", "Laura Virtanen approved the order.", {"order_id": "ord_a"})
    e2 = audit.record("hotel-aurora", "human:Laura Virtanen", "order.approved", "ord_a", "Approved by Laura Virtanen.", {"order_id": "ord_a"})
    e3 = audit.record("hotel-aurora", "human:Laura Virtanen", "order.placed", "ord_a", "Placed after human approval.", {"order_id": "ord_a"})
    e4 = audit.record("hospital-tays", "human:Helena Korhonen", "approval.rejected", "ord_r", "Helena Korhonen rejected the order.", {"order_id": "ord_r"})
    e5 = audit.record("hospital-tays", "human:Helena Korhonen", "order.rejected", "ord_r", "Rejected by Helena Korhonen.", {"order_id": "ord_r"})

    all_evs = [e1, e2, e3, e4, e5]

    buyer_lines = audit.narrate(all_evs, view="buyer").splitlines()
    assert len(buyer_lines) == 3
    assert "Laura Virtanen approved an order. Laura Virtanen approved the order." in buyer_lines[0]
    assert "Laura Virtanen placed an order. Placed after human approval." in buyer_lines[1]
    assert "Helena Korhonen rejected an order. Helena Korhonen rejected the order." in buyer_lines[2]
    assert "Approved by Laura Virtanen" not in "\n".join(buyer_lines)
    assert "Rejected by Helena Korhonen" not in "\n".join(buyer_lines)

    valio_lines = audit.narrate(all_evs, view="valio").splitlines()
    assert len(valio_lines) == 3
    assert valio_lines[0].endswith(f"Laura Virtanen at Hotel Aurora approved an order. Laura Virtanen approved the order. [ord_a, #{e1.seq}]")
    assert valio_lines[1].endswith(f"Laura Virtanen at Hotel Aurora placed an order. Placed after human approval. [ord_a, #{e3.seq}]")
    assert valio_lines[2].endswith(f"Helena Korhonen at Hospital Tays rejected an order. Helena Korhonen rejected the order. [ord_r, #{e4.seq}]")


def test_checkpoint_rotation_cli_and_forged_token_rejections(gateway, tmp_path, capsys):
    client, _ = gateway

    cp_empty = audit.checkpoint()
    assert cp_empty["seq"] == 0 and cp_empty["hash"] == "0" * 64 and cp_empty["ts"] is None
    kid_0 = keys.kid()

    assert audit.main(["aimo.audit", "verify-checkpoint", cp_empty["jws"]]) == 0
    assert capsys.readouterr().out.strip() == f"OK seq=0 hash={'0' * 64}"

    single_key_no_kid = jws.serialize_compact(
        {"alg": "EdDSA"},
        json.dumps({"seq": 0, "hash": "0" * 64, "ts": None}).encode(),
        keys.signing_key(),
        algorithms=["EdDSA"],
    )
    with pytest.raises(ValueError, match="BAD_SIGNATURE"):
        audit.verify_checkpoint(single_key_no_kid)

    e1 = audit.record("hotel-aurora", "agent:buyer-aurora", "quote.issued", "q_1", "Quote 1.")
    cp_1 = audit.checkpoint()
    assert cp_1["seq"] == 1 and cp_1["hash"] == e1.hash

    kid_1 = keys.rotate()
    assert kid_1 != kid_0

    e2 = audit.record("hotel-aurora", "system", "order.confirmed", "ord_1", "Confirmed 1.")
    cp_2 = audit.checkpoint()
    assert cp_2["seq"] == 2 and cp_2["hash"] == e2.hash

    kid_2 = keys.rotate()
    assert len({kid_0, kid_1, kid_2}) == 3
    keys._cache.clear()

    e3 = audit.record("hospital-tays", "system", "order.placed", "ord_2", "Placed 2.")
    cp_3 = audit.checkpoint()
    assert cp_3["seq"] == 3 and cp_3["hash"] == e3.hash

    jwks_doc = client.get("/.well-known/jwks.json").json()
    assert {k["kid"] for k in jwks_doc["keys"]} == {kid_0, kid_1, kid_2}
    jwks_file = tmp_path / "jwks_all.json"
    jwks_file.write_text(json.dumps(jwks_doc), encoding="utf-8")

    for expected_seq, expected_hash, cp in [
        (0, "0" * 64, cp_empty),
        (1, e1.hash, cp_1),
        (2, e2.hash, cp_2),
        (3, e3.hash, cp_3),
    ]:
        verified_mem = audit.verify_checkpoint(cp["jws"])
        verified_explicit = audit.verify_checkpoint(cp["jws"], jwks_doc)
        assert verified_mem == {"seq": expected_seq, "hash": expected_hash, "ts": cp["ts"]}
        assert verified_explicit == verified_mem

        rc_default = audit.main(["aimo.audit", "verify-checkpoint", cp["jws"]])
        out_default = capsys.readouterr().out.strip()
        assert rc_default == 0
        assert out_default == f"OK seq={expected_seq} hash={expected_hash}"

        rc_file = audit.main(["aimo.audit", "verify-checkpoint", cp["jws"], str(jwks_file)])
        out_file = capsys.readouterr().out.strip()
        assert rc_file == 0
        assert out_file == f"OK seq={expected_seq} hash={expected_hash}"

    proc_ok = subprocess.run(
        [sys.executable, "-m", "aimo.audit", "verify-checkpoint", cp_1["jws"], str(jwks_file)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert proc_ok.returncode == 0
    assert proc_ok.stdout.strip() == f"OK seq=1 hash={e1.hash}"

    active_only_file = tmp_path / "jwks_active_only.json"
    active_only_file.write_text(json.dumps({"keys": [keys.public_jwk()]}), encoding="utf-8")
    assert audit.main(["aimo.audit", "verify-checkpoint", cp_1["jws"], str(active_only_file)]) == 1
    assert capsys.readouterr().out.strip() == "BAD_SIGNATURE"

    seg_h, seg_p, seg_s = cp_3["jws"].split(".")

    tampered_body = {"seq": 999, "hash": cp_3["hash"], "ts": cp_3["ts"]}
    tampered_payload_jws = f"{seg_h}.{_b64url_encode(json.dumps(tampered_body, separators=(',', ':')).encode())}.{seg_s}"

    tampered_hash_body = {"seq": cp_3["seq"], "hash": ("f" if cp_3["hash"][0] != "f" else "0") + cp_3["hash"][1:], "ts": cp_3["ts"]}
    tampered_hash_jws = f"{seg_h}.{_b64url_encode(json.dumps(tampered_hash_body, separators=(',', ':')).encode())}.{seg_s}"

    alg_none_hdr = _b64url_encode(json.dumps({"alg": "none", "kid": kid_2}, separators=(",", ":")).encode())
    alg_none_empty_sig = f"{alg_none_hdr}.{seg_p}."
    alg_none_with_sig = f"{alg_none_hdr}.{seg_p}.{seg_s}"

    hs_hdr = _b64url_encode(json.dumps({"alg": "HS256", "kid": kid_2}, separators=(",", ":")).encode())
    pub_x = _b64url_decode(keys.public_jwk()["x"])
    hs_sig = _b64url_encode(hmac.new(pub_x, f"{hs_hdr}.{seg_p}".encode(), hashlib.sha256).digest())
    alg_hs256_jws = f"{hs_hdr}.{seg_p}.{hs_sig}"

    rogue_key = OKPKey.generate_key("Ed25519")
    rogue_own_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": rogue_key.thumbprint()},
        _b64url_decode(seg_p),
        rogue_key,
        algorithms=["EdDSA"],
    )
    rogue_spoofed_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": kid_2},
        _b64url_decode(seg_p),
        rogue_key,
        algorithms=["EdDSA"],
    )
    unknown_kid_valid_sig = jws.serialize_compact(
        {"alg": "EdDSA", "kid": "unknown-kid-999"},
        _b64url_decode(seg_p),
        keys.signing_key(),
        algorithms=["EdDSA"],
    )
    missing_kid_valid_sig = jws.serialize_compact(
        {"alg": "EdDSA"},
        _b64url_decode(seg_p),
        keys.signing_key(),
        algorithms=["EdDSA"],
    )
    missing_hash_field = jws.serialize_compact(
        {"alg": "EdDSA", "kid": kid_2},
        json.dumps({"seq": 3}).encode(),
        keys.signing_key(),
        algorithms=["EdDSA"],
    )
    non_dict_payload = jws.serialize_compact(
        {"alg": "EdDSA", "kid": kid_2},
        json.dumps([1, 2, 3]).encode(),
        keys.signing_key(),
        algorithms=["EdDSA"],
    )

    bad_tokens = [
        tampered_payload_jws,
        tampered_hash_jws,
        cp_3["jws"][:-4] + ("AAAA" if cp_3["jws"][-4:] != "AAAA" else "BBBB"),
        alg_none_empty_sig,
        alg_none_with_sig,
        alg_hs256_jws,
        rogue_own_kid,
        rogue_spoofed_kid,
        unknown_kid_valid_sig,
        missing_kid_valid_sig,
        missing_hash_field,
        non_dict_payload,
        "not-a-compact-jws",
        "",
    ]

    for bad in bad_tokens:
        with pytest.raises(ValueError, match="BAD_SIGNATURE"):
            audit.verify_checkpoint(bad)
        rc = audit.main(["aimo.audit", "verify-checkpoint", bad])
        out = capsys.readouterr().out.strip()
        assert rc == 1
        assert out == "BAD_SIGNATURE"

    for bad_file_case in [tampered_payload_jws, alg_none_empty_sig, rogue_own_kid, rogue_spoofed_kid]:
        rc_f = audit.main(["aimo.audit", "verify-checkpoint", bad_file_case, str(jwks_file)])
        out_f = capsys.readouterr().out.strip()
        assert rc_f == 1
        assert out_f == "BAD_SIGNATURE"

    for invalid_argv in [
        ["aimo.audit"],
        ["aimo.audit", "verify-checkpoint"],
        ["aimo.audit", "unknown-cmd", cp_3["jws"]],
    ]:
        rc_usage = audit.main(invalid_argv)
        out_usage = capsys.readouterr().out.strip()
        assert rc_usage == 2
        assert out_usage.startswith("usage: python -m aimo.audit verify-checkpoint")

    proc_usage = subprocess.run(
        [sys.executable, "-m", "aimo.audit"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert proc_usage.returncode == 2
    assert proc_usage.stdout.strip().startswith("usage: python -m aimo.audit verify-checkpoint")
