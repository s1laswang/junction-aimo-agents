from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

from fastapi.testclient import TestClient
import pytest

from aimo import approvals, audit, catalog, keys, offers, orders, quotes, valio
from aimo.app import app
from aimo.models import PlaceOrderRequest, Product, QuoteLine, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")


SYNTHETIC_PRODUCTS = {
    "1001": Product(
        product_id="1001",
        name="Valio Luomu Kevytmaito laktoositon",
        brand="Valio",
        vendor="VALIO OY",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["organic", "free_from_lactose"],
        origin="FI",
        temperature=4,
    ),
    "1002": Product(
        product_id="1002",
        name="Valio Maito laktoositon UHT",
        brand="Valio",
        vendor="VALIO OY",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose"],
        origin="FI",
        temperature=20,
    ),
}


@pytest.fixture(autouse=True)
def isolated_offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nonexistent_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)

    keys._cache.clear()
    offers._overrides.clear()
    catalog.reset()

    monkeypatch.setattr(catalog, "get", lambda pid: SYNTHETIC_PRODUCTS.get(str(pid)))
    monkeypatch.setattr(
        catalog,
        "search",
        lambda q="", constraints=None, limit=50: list(SYNTHETIC_PRODUCTS.values())[:limit],
    )
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.25 if str(pid) in SYNTHETIC_PRODUCTS else None)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})

    def fake_price(customer, requested_date, ids):
        return {
            str(pid): {
                "productId": str(pid),
                "currency": "EUR",
                "catalogPrice": {"value": 1.50},
                "priceWithTax": {"value": 1.71},
            }
            for pid in ids
            if str(pid) in SYNTHETIC_PRODUCTS
        }

    def fake_availability(customer, requested_date, ids):
        return {
            str(pid): {
                "productId": str(pid),
                "salesBlock": False,
                "closingTime": "2026-11-15T12:00:00+02:00",
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 5000.0,
                        "availableDate": requested_date.isoformat(),
                        "salesUnit": "LTR",
                    }
                ],
            }
            for pid in ids
            if str(pid) in SYNTHETIC_PRODUCTS
        }

    monkeypatch.setattr(valio, "price", fake_price)
    monkeypatch.setattr(valio, "availability", fake_availability)

    yield tmp_path

    keys._cache.clear()
    offers._overrides.clear()
    catalog.reset()


def _issue_quote(
    customer="hotel-aurora",
    agent_id="buyer-aurora",
    product_id="1001",
    quantity=100.0,
    unit_price_net=1.50,
    hours_valid=2,
):
    now = datetime.now(timezone.utc)
    delivery_day = datetime.now(quotes.HELSINKI).date() + timedelta(days=2)
    net = round(quantity * unit_price_net, 2)
    gross = round(net * 1.14, 2)
    line = QuoteLine(
        product_id=product_id,
        name=SYNTHETIC_PRODUCTS[product_id].name,
        quantity=quantity,
        sales_unit="LTR",
        unit_price_net=unit_price_net,
        line_total_net=net,
    )
    return quotes.issue(
        customer=customer,
        agent_id=agent_id,
        lines=[line],
        delivery_date=delivery_day,
        valid_until=now + timedelta(hours=hours_valid),
        total_gross=gross,
    )


def test_single_use_quote_redemption_race():
    q = _issue_quote(quantity=50.0, unit_price_net=1.50)
    num_threads = 10
    barrier = threading.Barrier(num_threads)

    def worker():
        barrier.wait(timeout=10)
        try:
            out = quotes.redeem(q.quote_id, q.jws, "hotel-aurora", "buyer-aurora")
            return ("ok", out)
        except quotes.QuoteError as exc:
            return ("err", exc)

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(worker) for _ in range(num_threads)]
        results = [f.result(timeout=15) for f in futures]

    wins = [val for tag, val in results if tag == "ok"]
    errs = [val for tag, val in results if tag == "err"]

    assert len(wins) == 1
    assert wins[0].quote_id == q.quote_id
    assert wins[0].total_net == q.total_net
    assert len(errs) == num_threads - 1
    assert all(e.code == "QUOTE_USED" for e in errs)

    events = audit.events(order_id=q.quote_id)
    assert [e.action for e in events].count("quote.issued") == 1
    assert [e.action for e in events].count("quote.redeemed") == 1
    assert audit.verify() == (True, None)


def test_concurrent_orders_place_same_quote():
    q = _issue_quote(quantity=80.0, unit_price_net=1.50)
    num_threads = 8
    barrier = threading.Barrier(num_threads)

    def worker():
        req = PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws)
        barrier.wait(timeout=10)
        try:
            order = orders.place("hotel-aurora", "buyer-aurora", req)
            return ("ok", order)
        except quotes.QuoteError as exc:
            return ("err", exc)

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(worker) for _ in range(num_threads)]
        results = [f.result(timeout=15) for f in futures]

    wins = [val for tag, val in results if tag == "ok"]
    errs = [val for tag, val in results if tag == "err"]

    assert len(wins) == 1
    assert wins[0].quote_id == q.quote_id
    assert wins[0].status == "confirmed"
    assert len(errs) == num_threads - 1
    assert all(e.code == "QUOTE_USED" for e in errs)

    aurora_orders = orders.list(customer="hotel-aurora")
    assert len(aurora_orders) == 1
    assert aurora_orders[0].order_id == wins[0].order_id
    assert aurora_orders[0].quote_id == q.quote_id
    assert audit.verify() == (True, None)


def test_concurrent_http_post_orders_same_quote():
    q = _issue_quote(quantity=60.0, unit_price_net=1.50)
    num_threads = 8
    barrier = threading.Barrier(num_threads)

    with TestClient(app) as client:
        def worker():
            barrier.wait(timeout=10)
            return client.post(
                "/v1/orders",
                headers={"X-Agent-Key": "demo-hotel-aurora"},
                json={"quote_id": q.quote_id, "jws": q.jws},
            )

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(worker) for _ in range(num_threads)]
            responses = [f.result(timeout=15) for f in futures]

    ok_resps = [r for r in responses if r.status_code == 200]
    conflict_resps = [r for r in responses if r.status_code == 409]

    assert len(ok_resps) == 1
    assert len(conflict_resps) == num_threads - 1
    assert all(r.json()["code"] == "QUOTE_USED" for r in conflict_resps)

    aurora_orders = orders.list(customer="hotel-aurora")
    assert len(aurora_orders) == 1
    assert aurora_orders[0].order_id == ok_resps[0].json()["order_id"]
    assert audit.verify() == (True, None)


@pytest.mark.parametrize("decisions", [
    [True, False, True, False, True, False, True, False],
    [True, True, True, True, True, True, True, True],
    [False, False, False, False, False, False, False, False],
])
def test_concurrent_approvals_decide_race(decisions):
    q = _issue_quote(quantity=2000.0, unit_price_net=1.60)
    pending_order = orders.place(
        "hotel-aurora",
        "buyer-aurora",
        PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws),
    )
    assert pending_order.status == "pending_approval"

    approvers = ["Laura Virtanen", "Mikko Heikkinen"]
    num_threads = len(decisions)
    barrier = threading.Barrier(num_threads)

    def worker(idx, approve_flag):
        by = approvers[idx % len(approvers)]
        barrier.wait(timeout=10)
        try:
            decided = approvals.decide(
                pending_order.order_id,
                approve_flag,
                by,
                f"concurrent decision {idx}",
            )
            return ("ok", decided)
        except approvals.ApprovalError as exc:
            return ("err", exc)

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(worker, i, flag) for i, flag in enumerate(decisions)]
        results = [f.result(timeout=15) for f in futures]

    wins = [val for tag, val in results if tag == "ok"]
    errs = [val for tag, val in results if tag == "err"]

    assert len(wins) >= 1
    assert len(wins) + len(errs) == num_threads
    assert all(e.code == "NOT_PENDING" for e in errs)

    final_order = orders.get(pending_order.order_id)
    assert final_order is not None
    assert final_order.status in ("confirmed", "rejected")
    if all(decisions):
        assert final_order.status == "confirmed"
    if not any(decisions):
        assert final_order.status == "rejected"
    assert approvals.pending("hotel-aurora") == []

    with pytest.raises(approvals.ApprovalError) as after_exc:
        approvals.decide(pending_order.order_id, True, "Laura Virtanen", "late retry")
    assert after_exc.value.code == "NOT_PENDING"
    assert audit.verify() == (True, None)


def test_concurrent_distinct_orders_key_rotation_and_audit_chain():
    pre_rotation_quotes = [
        _issue_quote(product_id="1001", quantity=float(20 + i * 10), unit_price_net=1.50)
        for i in range(3)
    ]
    first_kid = keys.kid()
    rotated_kid = keys.rotate()
    assert rotated_kid != first_kid

    post_rotation_quotes = [
        _issue_quote(product_id="1002", quantity=float(50 + i * 10), unit_price_net=1.40)
        for i in range(3)
    ]
    distinct_quotes = pre_rotation_quotes + post_rotation_quotes
    assert len({q.quote_id for q in distinct_quotes}) == 6

    total_workers = 12
    barrier = threading.Barrier(total_workers)

    def place_worker(q):
        req = PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws)
        barrier.wait(timeout=10)
        return orders.place("hotel-aurora", "buyer-aurora", req)

    def jwks_verify_worker():
        barrier.wait(timeout=10)
        jwks_doc = keys.jwks()
        verified_ids = []
        for q in distinct_quotes:
            v1 = quotes.verify(q.jws, jwks_doc)
            v2 = quotes.verify(q.jws)
            assert v1.quote_id == q.quote_id
            assert v2.quote_id == q.quote_id
            verified_ids.append(v1.quote_id)
        return jwks_doc, verified_ids

    with ThreadPoolExecutor(max_workers=total_workers) as pool:
        place_futures = [pool.submit(place_worker, q) for q in distinct_quotes]
        verify_futures = [pool.submit(jwks_verify_worker) for _ in range(6)]
        placed_orders = [f.result(timeout=15) for f in place_futures]
        verify_results = [f.result(timeout=15) for f in verify_futures]

    assert len(placed_orders) == 6
    assert len({o.order_id for o in placed_orders}) == 6
    assert all(o.status == "confirmed" for o in placed_orders)

    persisted = orders.list(customer="hotel-aurora")
    assert len(persisted) == 6
    assert {o.quote_id for o in persisted} == {q.quote_id for q in distinct_quotes}

    expected_quote_ids = [q.quote_id for q in distinct_quotes]
    for jwks_doc, verified_ids in verify_results:
        assert verified_ids == expected_quote_ids
        assert {k["kid"] for k in jwks_doc["keys"]} == {rotated_kid, first_kid}
        assert all("d" not in k for k in jwks_doc["keys"])

    ok, bad_seq = audit.verify()
    assert ok is True and bad_seq is None

    trail = audit.events()
    assert [e.seq for e in trail] == list(range(1, len(trail) + 1))
    prev = audit.GENESIS
    for ev in trail:
        assert ev.prev_hash == prev
        prev = ev.hash

    cp = audit.checkpoint()
    head = audit.verify_checkpoint(cp["jws"], keys.jwks())
    assert head["seq"] == len(trail)
    assert head["hash"] == trail[-1].hash


def test_concurrent_http_post_approvals_decide_race():
    q = _issue_quote(quantity=2200.0, unit_price_net=1.50)
    pending_order = orders.place(
        "hotel-aurora",
        "buyer-aurora",
        PlaceOrderRequest(quote_id=q.quote_id, jws=q.jws),
    )
    assert pending_order.status == "pending_approval"

    num_threads = 8
    barrier = threading.Barrier(num_threads)
    approvers = ["Laura Virtanen", "Mikko Heikkinen"]

    with TestClient(app) as client:
        def worker(idx):
            approve_flag = idx % 2 == 0
            by = approvers[idx % 2]
            barrier.wait(timeout=10)
            return client.post(
                f"/v1/approvals/{pending_order.order_id}",
                headers={"X-Admin-Key": "demo-admin"},
                json={"approve": approve_flag, "by": by, "note": f"http race {idx}"},
            )

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(worker, i) for i in range(num_threads)]
            responses = [f.result(timeout=15) for f in futures]

    ok_resps = [r for r in responses if r.status_code == 200]
    conflict_resps = [r for r in responses if r.status_code == 409]

    assert len(ok_resps) >= 1
    assert len(ok_resps) + len(conflict_resps) == num_threads
    assert all(r.json()["code"] == "NOT_PENDING" for r in conflict_resps)

    final_order = orders.get(pending_order.order_id)
    assert final_order is not None
    assert final_order.status in ("confirmed", "rejected")
    assert audit.verify() == (True, None)


def test_concurrent_audit_record_and_checkpoint_integrity():
    keys.signing_key()
    writers = 8
    readers = 2
    per_writer = 3
    barrier = threading.Barrier(writers + readers)

    def writer_fn(worker_idx):
        barrier.wait(timeout=10)
        created = []
        for step in range(per_writer):
            ev = audit.record(
                "hotel-aurora",
                f"agent:w{worker_idx}",
                "order.placed",
                f"ord_{worker_idx}_{step}",
                f"Concurrent write {worker_idx}-{step}.",
                {"worker": worker_idx, "step": step, "total_net": 15.0 + step},
            )
            created.append(ev.seq)
        return created

    def reader_fn():
        barrier.wait(timeout=10)
        checks = []
        for _ in range(per_writer):
            ok, bad = audit.verify()
            cp = audit.checkpoint()
            head = audit.verify_checkpoint(cp["jws"])
            checks.append((ok, bad, head["seq"]))
        return checks

    with ThreadPoolExecutor(max_workers=writers + readers) as pool:
        w_futs = [pool.submit(writer_fn, i) for i in range(writers)]
        r_futs = [pool.submit(reader_fn) for _ in range(readers)]
        w_seqs = [seq for f in w_futs for seq in f.result(timeout=15)]
        r_checks = [chk for f in r_futs for chk in f.result(timeout=15)]

    expected_total = writers * per_writer
    assert sorted(w_seqs) == list(range(1, expected_total + 1))
    assert all(ok is True and bad is None and seq >= 0 for ok, bad, seq in r_checks)
    assert audit.verify() == (True, None)

    final_cp = audit.checkpoint()
    final_head = audit.verify_checkpoint(final_cp["jws"], keys.jwks())
    assert final_head["seq"] == expected_total

