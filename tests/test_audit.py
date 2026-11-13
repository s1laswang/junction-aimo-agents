import sqlite3

from aimo import audit


def test_chain(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    monkeypatch.setenv("AIMO_DB", str(db))
    audit.record("hotel-aurora", "agent:buyer", "quote.issued", "q1", "Cheapest lactose-free milk that is in stock.", {"quote_id": "q1", "total_gross": 1240})
    audit.record("hotel-aurora", "agent:buyer", "order.placed", "o1", "Under the 2,000 EUR per-order limit.", {"order_id": "o1", "quote_id": "q1", "total_net": 1240.0})
    audit.record("hospital-tays", "human:anna", "approval.approved", "o2", "Checked allergens.")
    assert audit.verify() == (True, None)
    assert len(audit.events("hotel-aurora")) == 2
    assert [e.seq for e in audit.events(order_id="o1")] == [1, 2]
    text = audit.narrate(audit.events())
    assert "1,240.00 EUR" in text and len(text.splitlines()) == 3
    assert "#2" in audit.narrate(audit.events(), view="valio")
    c = sqlite3.connect(db)
    c.execute("update audit set why='edited' where seq=2")
    c.commit()
    assert audit.verify() == (False, 2)


def test_v2(tmp_path, monkeypatch):
    from datetime import date

    from joserfc import jws

    from aimo import keys

    monkeypatch.setenv("AIMO_DB", str(tmp_path / "b.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    e = audit.record("hotel-aurora", "system", "order.pending_approval", "o9", "Held for Laura Virtanen.", {"order_id": "o9", "d": date(2026, 11, 14), "reasons": [{"code": "OVER_ORDER_LIMIT", "text": "The 3,400 EUR order is over the 3,000 EUR limit."}]})
    assert audit.verify() == (True, None)
    text = audit.narrate([e])
    assert "over the 3,000 EUR limit" in text
    h = e.ts.astimezone(audit.HELSINKI).strftime("%H:%M")
    assert text.startswith(h)
    cp = audit.checkpoint()
    got = jws.deserialize_compact(cp["jws"], keys.signing_key(), algorithms=["EdDSA"])
    assert got.protected["kid"] == keys.kid() and cp["hash"] == e.hash and cp["seq"] == 1


def test_float_matches_js(tmp_path, monkeypatch):
    import hashlib
    import json

    monkeypatch.setenv("AIMO_DB", str(tmp_path / "f.db"))
    e = audit.record("hotel-aurora", "system", "order.placed", "o1", "ok", {"total": 1590.0, "n": [2.0, 1.5]})
    js = json.dumps({"action": e.action, "actor": e.actor, "customer": e.customer, "detail": {"n": [2, 1.5], "total": 1590}, "seq": 1, "subject": "o1", "ts": e.ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "why": "ok"}, sort_keys=True, separators=(",", ":"))
    assert e.hash == hashlib.sha256(("0" * 64 + js).encode()).hexdigest()
    assert audit.verify() == (True, None)


def test_system_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "s.db"))
    audit.record("hotel-aurora", "system", "order.approved", "o1", "Order total 1,240.00 EUR is under the 3,000 EUR limit.", {"order_id": "o1"})
    audit.record("hotel-aurora", "system", "order.confirmed", "o1", "Mock confirmation, delivery 2026-11-16.", {"order_id": "o1"})
    audit.record("hotel-aurora", "agent:x", "order_rejected", "q1", "Quote belongs to another agent.", {"quote_id": "q1"})
    lines = audit.narrate(audit.events()).splitlines()
    assert lines[0][6:].startswith("Mandate check passed: Order total")
    assert "Order confirmed by Valio (mock). Delivery 2026-11-16." in lines[1]
    assert lines[2][6:].startswith("Refused: Quote belongs")
    assert "The system" not in "".join(lines)


def test_human_approval_not_doubled(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "h.db"))
    audit.record("hotel-aurora", "human:Laura Virtanen", "order.approved", "o1", "x", {"order_id": "o1"})
    audit.record("hotel-aurora", "human:Laura Virtanen", "approval.approved", "o1", "Laura Virtanen approved the order.", {"order_id": "o1"})
    audit.record("hotel-aurora", "human:Laura Virtanen", "order.placed", "o1", "Placed after human approval.", {"order_id": "o1"})
    assert len(audit.events(order_id="o1")) == 3
    lines = audit.narrate(audit.events(order_id="o1")).splitlines()
    assert len(lines) == 2 and "Placed after human approval." in lines[1]


def test_verify_checkpoint_cli(tmp_path, monkeypatch, capsys):
    import json

    from aimo import keys

    monkeypatch.setenv("AIMO_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    e = audit.record("hotel-aurora", "system", "order.placed", "o1", "ok")
    cp = audit.checkpoint()
    f = tmp_path / "jwks.json"
    f.write_text(json.dumps(keys.jwks()))
    assert audit.main(["x", "verify-checkpoint", cp["jws"], str(f)]) == 0
    assert f"OK seq=1 hash={e.hash}" in capsys.readouterr().out
    assert audit.main(["x", "verify-checkpoint", cp["jws"]]) == 0
    assert audit.main(["x", "verify-checkpoint", cp["jws"][:-4] + "AAAA"]) == 1
    assert "BAD_SIGNATURE" in capsys.readouterr().out
