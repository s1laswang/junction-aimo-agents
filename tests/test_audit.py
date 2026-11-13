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
