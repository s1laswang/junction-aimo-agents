import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aimo import catalog, offers, valio
from aimo.app import app
from aimo.models import Product, UnitSize

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

DAY = date(2026, 11, 20)
CUSTOMER = "hotel-aurora"
AGENT_HEADERS = {"X-Agent-Key": "demo-hotel-aurora"}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture(autouse=True)
def sleeps(tmp_path, monkeypatch):
    recorded: list[float] = []
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "unit.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "missing_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    if valio._client is not None:
        valio._client.close()
    monkeypatch.setattr(valio, "_client", None)
    monkeypatch.setattr(valio.time, "sleep", lambda s: recorded.append(float(s)))
    catalog.reset()
    offers.clear_simulation()
    yield recorded
    if valio._client is not None:
        valio._client.close()
    valio._client = None
    offers.clear_simulation()
    catalog.reset()


def _install_transport(monkeypatch, handler) -> httpx.Client:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://mock.valio.test",
        headers={"X-API-Key": "test-key"},
        timeout=30,
    )
    monkeypatch.setattr(valio, "_client", client)
    return client


def _price_item(pid: str | int, value: float = 2.5) -> dict:
    return {
        "productId": str(pid),
        "currency": "EUR",
        "catalogPrice": {"value": value},
        "priceWithTax": {"value": round(value * 1.14, 2)},
    }


def _avail_item(pid: str | int, day: date = DAY, amount: float = 120.0) -> dict:
    return {
        "productId": str(pid),
        "closingTime": "2026-11-12T15:00:00+0200",
        "salesBlock": False,
        "scheduleLines": [
            {
                "salesBlock": False,
                "availableAmount": amount,
                "availableDate": day.isoformat(),
                "salesUnit": "LTR",
            }
        ],
    }


def _stub_catalog(monkeypatch) -> Product:
    milk = Product(
        product_id="464073",
        name="Valio Laktoositon Kevytmaito 1 L",
        brand="Valio",
        vendor="Valio Oy",
        category="10400",
        sales_unit="LTR",
        unit_size=UnitSize(value=1.0, unit="l"),
        allergens=["milk"],
        labels=["free_from_lactose"],
        origin="FI",
    )
    monkeypatch.setattr(catalog, "search", lambda q="", constraints=None, limit=50: [milk][:limit])
    monkeypatch.setattr(catalog, "get", lambda pid: milk if str(pid) == milk.product_id else None)
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.99)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.1)
    return milk


def _mcp_init(client: TestClient) -> str:
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "upstream-unit", "version": "1.0"},
            },
        },
        headers=MCP_HEADERS,
    )
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    n = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**MCP_HEADERS, "mcp-session-id": sid},
    )
    assert n.status_code in (200, 202, 204), n.text
    return sid


def _mcp_call(client: TestClient, sid: str, req_id: int, name: str, arguments: dict, headers: dict) -> dict:
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={**MCP_HEADERS, "mcp-session-id": sid, **headers},
    )
    assert r.status_code == 200, r.text
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
    return json.loads(body)["result"]


def test_http_client_singleton_and_missing_env(monkeypatch, sleeps):
    with pytest.raises(valio.UpstreamUnavailable, match="VALIO_API_BASE or VALIO_API_KEY not configured"):
        valio._http()
    assert valio._client is None

    monkeypatch.setenv("VALIO_API_BASE", "   ")
    monkeypatch.setenv("VALIO_API_KEY", "k")
    with pytest.raises(valio.UpstreamUnavailable):
        valio._http()

    monkeypatch.setenv("VALIO_API_BASE", "https://api.valio.example/sub/")
    monkeypatch.setenv("VALIO_API_KEY", "  secret-token  ")
    c1 = valio._http()
    c2 = valio._http()
    assert c1 is c2
    assert str(c1.base_url).rstrip("/") == "https://api.valio.example/sub"
    assert c1.headers["X-API-Key"] == "secret-token"
    assert sleeps == []


def test_429_numeric_retry_after_succeeds_on_second_attempt(monkeypatch, sleeps):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2.75"})
        return httpx.Response(200, json={"products": [_price_item("101", 3.4)]})

    _install_transport(monkeypatch, handler)
    out = valio.price(CUSTOMER, DAY, ["101"])
    assert len(calls) == 2
    assert sleeps == [2.75]
    assert out["101"]["catalogPrice"]["value"] == 3.4


def test_429_numeric_retry_after_succeeds_on_third_attempt(monkeypatch, sleeps):
    headers_seq = [{"Retry-After": "1.25"}, {"Retry-After": "4"}]
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(429, headers=headers_seq[calls - 1])
        return httpx.Response(200, json={"products": [_avail_item("202", DAY, 88.0)]})

    _install_transport(monkeypatch, handler)
    out = valio.availability(CUSTOMER, DAY, ["202"])
    assert calls == 3
    assert sleeps == [1.25, 4.0]
    assert out["202"]["scheduleLines"][0]["availableAmount"] == 88.0


def test_429_caps_retry_after_above_ten_seconds(monkeypatch, sleeps):
    waits = ["10.01", "60", "3600"]
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        hdr = {"Retry-After": waits[calls]}
        calls += 1
        return httpx.Response(429, headers=hdr)

    _install_transport(monkeypatch, handler)
    with pytest.raises(valio.UpstreamUnavailable, match="HTTP 429"):
        valio.price(CUSTOMER, DAY, ["303"])
    assert calls == 3
    assert sleeps == [10.0, 10.0]


def test_429_caps_large_retry_after_and_recovers(monkeypatch, sleeps):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "45"})
        return httpx.Response(200, json={"products": [_price_item("304", 1.9)]})

    _install_transport(monkeypatch, handler)
    out = valio.price(CUSTOMER, DAY, ["304"])
    assert calls == 2
    assert sleeps == [10.0]
    assert out["304"]["catalogPrice"]["value"] == 1.9


@pytest.mark.parametrize(
    "retry_after",
    [None, "", "not-a-number", "Wed, 21 Oct 2026 07:28:00 GMT"],
)
def test_429_missing_or_non_numeric_retry_after_uses_exponential_backoff(monkeypatch, sleeps, retry_after):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            hdrs = {} if retry_after is None else {"Retry-After": retry_after}
            return httpx.Response(429, headers=hdrs)
        return httpx.Response(200, json={"products": [_price_item("405", 4.2)]})

    _install_transport(monkeypatch, handler)
    out = valio.price(CUSTOMER, DAY, ["405"])
    assert calls == 3
    assert sleeps == [0.5, 1.0]
    assert out["405"]["catalogPrice"]["value"] == 4.2


@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [
        (500, "HTTP 500"),
        (502, "HTTP 502"),
        (503, "HTTP 503"),
        (httpx.ConnectTimeout, "ConnectTimeout"),
        (httpx.ReadTimeout, "ReadTimeout"),
        (httpx.ConnectError, "ConnectError"),
    ],
)
def test_5xx_and_transport_errors_retry_three_attempts_then_raise_on_cold_cache(
    monkeypatch, sleeps, failure, expected_detail
):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("simulated network failure", request=req)

    _install_transport(monkeypatch, handler)
    with pytest.raises(valio.UpstreamUnavailable) as exc_info:
        valio.price(CUSTOMER, DAY, ["500"])
    assert calls == 3
    assert sleeps == [0.5, 1.0]
    assert exc_info.value.code == "UPSTREAM_UNAVAILABLE"
    assert expected_detail in str(exc_info.value)


@pytest.mark.parametrize(
    "failure",
    [500, 502, 503, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError],
)
def test_5xx_and_transport_errors_fallback_to_stale_for_price_and_availability(
    tmp_path, monkeypatch, sleeps, failure
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    stale_day = date(2026, 11, 10)
    (cache_dir / f"price__{CUSTOMER}__{stale_day.isoformat()}.json").write_text(
        json.dumps({"501": _price_item("501", 6.75)})
    )
    (cache_dir / f"availability__{CUSTOMER}__{stale_day.isoformat()}.json").write_text(
        json.dumps({"501": _avail_item("501", stale_day, 42.0)})
    )

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("upstream unreachable", request=req)

    _install_transport(monkeypatch, handler)

    pr = valio.price(CUSTOMER, DAY, ["501"])
    assert pr["501"]["catalogPrice"]["value"] == 6.75
    assert calls == ["/price", "/price", "/price"]
    assert sleeps == [0.5, 1.0]

    av = valio.availability(CUSTOMER, DAY, ["501"])
    assert av["501"]["scheduleLines"][0]["availableAmount"] == 42.0
    assert calls == ["/price", "/price", "/price", "/availability", "/availability", "/availability"]
    assert sleeps == [0.5, 1.0, 0.5, 1.0]


def test_transient_transport_then_502_recovers_on_third_attempt(monkeypatch, sleeps):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("connect timed out", request=req)
        if calls == 2:
            return httpx.Response(502)
        return httpx.Response(200, json={"products": [_price_item("509", 7.1)]})

    _install_transport(monkeypatch, handler)
    out = valio.price(CUSTOMER, DAY, ["509"])
    assert calls == 3
    assert sleeps == [0.5, 1.0]
    assert out["509"]["catalogPrice"]["value"] == 7.1


def test_stale_picks_newest_date_file_and_isolates_customer_and_endpoint(tmp_path, monkeypatch, sleeps):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    (cache_dir / f"price__{CUSTOMER}__2026-11-01.json").write_text(
        json.dumps(
            {
                "p1": _price_item("p1", 1.0),
                "p2": _price_item("p2", 2.0),
                "p3": _price_item("p3", 3.0),
            }
        )
    )
    (cache_dir / f"price__{CUSTOMER}__2026-11-14.json").write_text(
        json.dumps({"p1": _price_item("p1", 1.9)})
    )
    (cache_dir / f"price__{CUSTOMER}__2026-11-09.json").write_text(
        json.dumps({"p1": _price_item("p1", 1.4), "p2": _price_item("p2", 2.5)})
    )
    (cache_dir / "price__other-customer__2026-11-19.json").write_text(
        json.dumps({"p1": _price_item("p1", 99.0), "p4": _price_item("p4", 99.0)})
    )
    (cache_dir / f"availability__{CUSTOMER}__2026-11-19.json").write_text(
        json.dumps({"p1": _avail_item("p1", date(2026, 11, 19), 500.0)})
    )
    (cache_dir / f"availability__{CUSTOMER}__2026-11-05.json").write_text(
        json.dumps(
            {
                "p1": _avail_item("p1", date(2026, 11, 5), 10.0),
                "p2": _avail_item("p2", date(2026, 11, 5), 75.0),
            }
        )
    )

    _install_transport(monkeypatch, lambda req: httpx.Response(503))

    prices = valio.price(CUSTOMER, DAY, ["p1", "p2", "p3", "p4"])
    assert set(prices) == {"p1", "p2", "p3"}
    assert prices["p1"]["catalogPrice"]["value"] == 1.9
    assert prices["p2"]["catalogPrice"]["value"] == 2.5
    assert prices["p3"]["catalogPrice"]["value"] == 3.0

    avail = valio.availability(CUSTOMER, DAY, ["p1", "p2"])
    assert avail["p1"]["scheduleLines"][0]["availableAmount"] == 500.0
    assert avail["p2"]["scheduleLines"][0]["availableAmount"] == 75.0
    assert sleeps == [0.5, 1.0, 0.5, 1.0]


def test_stale_ignores_corrupt_json_and_unreadable_entries(tmp_path, monkeypatch, sleeps):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    (cache_dir / f"price__{CUSTOMER}__2026-11-18.json").write_text("{corrupt json")
    (cache_dir / f"price__{CUSTOMER}__2026-11-17.json").write_text("")
    (cache_dir / f"price__{CUSTOMER}__2026-11-16.json").mkdir()
    (cache_dir / f"price__{CUSTOMER}__2026-11-10.json").write_text(
        json.dumps({"777": _price_item("777", 5.25)})
    )

    (cache_dir / f"availability__{CUSTOMER}__2026-11-18.json").write_text("not-json")
    (cache_dir / f"availability__{CUSTOMER}__2026-11-10.json").write_text(
        json.dumps({"777": _avail_item("777", date(2026, 11, 10), 64.0)})
    )

    _install_transport(monkeypatch, lambda req: httpx.Response(500))

    pr = valio.price(CUSTOMER, DAY, ["777"])
    assert pr["777"]["catalogPrice"]["value"] == 5.25

    av = valio.availability(CUSTOMER, DAY, ["777"])
    assert av["777"]["scheduleLines"][0]["availableAmount"] == 64.0
    assert sleeps == [0.5, 1.0, 0.5, 1.0]


def test_merges_partial_fresh_store_with_stale_hits(tmp_path, monkeypatch, sleeps):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    (cache_dir / f"price__{CUSTOMER}__{DAY.isoformat()}.json").write_text(
        json.dumps({"fresh1": _price_item("fresh1", 10.0)})
    )
    (cache_dir / f"price__{CUSTOMER}__2026-11-12.json").write_text(
        json.dumps(
            {
                "fresh1": _price_item("fresh1", 4.0),
                "stale2": _price_item("stale2", 20.0),
            }
        )
    )

    sent_bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent_bodies.append(json.loads(req.content))
        return httpx.Response(503)

    _install_transport(monkeypatch, handler)

    merged = valio.price(CUSTOMER, DAY, ["fresh1", "stale2", "unknown3"])
    assert len(sent_bodies) == 3
    assert all(b["products"] == ["stale2", "unknown3"] for b in sent_bodies)
    assert set(merged) == {"fresh1", "stale2"}
    assert merged["fresh1"]["catalogPrice"]["value"] == 10.0
    assert merged["stale2"]["catalogPrice"]["value"] == 20.0
    assert sleeps == [0.5, 1.0]

    other_day = date(2026, 11, 21)
    (cache_dir / f"price__{CUSTOMER}__{other_day.isoformat()}.json").write_text(
        json.dumps({"only_fresh": _price_item("only_fresh", 8.5)})
    )
    partial = valio.price(CUSTOMER, other_day, ["only_fresh", "completely_missing"])
    assert set(partial) == {"only_fresh"}
    assert partial["only_fresh"]["catalogPrice"]["value"] == 8.5


def test_merges_first_chunk_fresh_with_second_chunk_stale_on_mid_batch_failure(tmp_path, monkeypatch, sleeps):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    stale_store = {"0": _price_item("0", 0.5)}
    for i in range(1000, 1200):
        stale_store[str(i)] = _price_item(str(i), 9.0)
    (cache_dir / f"price__{CUSTOMER}__2026-11-11.json").write_text(json.dumps(stale_store))

    chunk_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        products = json.loads(req.content)["products"]
        chunk_calls.append(len(products))
        if len(products) == 1000:
            return httpx.Response(200, json={"products": [_price_item(pid, 3.0) for pid in products]})
        raise httpx.ReadTimeout("second chunk timed out", request=req)

    _install_transport(monkeypatch, handler)

    req_ids = [str(i) for i in range(1200)]
    out = valio.price(CUSTOMER, DAY, req_ids)
    assert chunk_calls == [1000, 200, 200, 200]
    assert sleeps == [0.5, 1.0]
    assert len(out) == 1200
    assert out["0"]["catalogPrice"]["value"] == 3.0
    assert out["999"]["catalogPrice"]["value"] == 3.0
    assert out["1000"]["catalogPrice"]["value"] == 9.0
    assert out["1199"]["catalogPrice"]["value"] == 9.0


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_non_retryable_4xx_raises_immediately_without_sleep(monkeypatch, sleeps, status_code):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json={"error": "client error"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(valio.UpstreamUnavailable) as exc_info:
        valio.price(CUSTOMER, DAY, ["464073"])
    assert calls == 1
    assert sleeps == []
    assert f"HTTP {status_code}" in str(exc_info.value)


def test_cold_cache_failure_propagates_503_to_rest_and_error_to_mcp(monkeypatch, sleeps):
    milk = _stub_catalog(monkeypatch)
    future_day = datetime.now(offers.HEL).date() + timedelta(days=5)

    attempts = []

    def failing_handler(req: httpx.Request) -> httpx.Response:
        attempts.append(req.url.path)
        return httpx.Response(503, json={"message": "service unavailable"})

    _install_transport(monkeypatch, failing_handler)

    with pytest.raises(valio.UpstreamUnavailable):
        valio.price(CUSTOMER, future_day, [milk.product_id])
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]

    need_payload = {
        "customer": CUSTOMER,
        "deliver_by": future_day.isoformat(),
        "items": [{"query": "milk", "quantity": 10, "unit": "l", "constraints": {"lactose_free": True}}],
    }
    quote_payload = {
        "offers": [{"product_id": milk.product_id, "quantity": 10}],
        "delivery_date": future_day.isoformat(),
    }

    with TestClient(app) as client:
        r_offers = client.post("/v1/offers", json=need_payload, headers=AGENT_HEADERS)
        assert r_offers.status_code == 503, r_offers.text
        body_offers = r_offers.json()
        assert body_offers["code"] == "UPSTREAM_UNAVAILABLE"
        assert body_offers.get("text")

        r_quotes = client.post("/v1/quotes", json=quote_payload, headers=AGENT_HEADERS)
        assert r_quotes.status_code == 503, r_quotes.text
        body_quotes = r_quotes.json()
        assert body_quotes["code"] == "UPSTREAM_UNAVAILABLE"
        assert body_quotes.get("text")

        sid = _mcp_init(client)

        mcp_offers = _mcp_call(client, sid, 2, "get_offers", need_payload, AGENT_HEADERS)
        assert mcp_offers.get("isError") is True, mcp_offers
        offers_text = " ".join(b["text"] for b in mcp_offers.get("content", []))
        assert "UPSTREAM_UNAVAILABLE" in offers_text
        offers_err = json.loads(offers_text.split("Error executing tool get_offers: ", 1)[-1])
        assert offers_err["code"] == "UPSTREAM_UNAVAILABLE"

        mcp_quote = _mcp_call(client, sid, 3, "request_quote", quote_payload, AGENT_HEADERS)
        assert mcp_quote.get("isError") is True, mcp_quote
        quote_text = " ".join(b["text"] for b in mcp_quote.get("content", []))
        assert "UPSTREAM_UNAVAILABLE" in quote_text
        quote_err = json.loads(quote_text.split("Error executing tool request_quote: ", 1)[-1])
        assert quote_err["code"] == "UPSTREAM_UNAVAILABLE"


def test_availability_cold_failure_propagates_503_and_mcp_error_when_price_is_cached(tmp_path, monkeypatch, sleeps):
    milk = _stub_catalog(monkeypatch)
    future_day = datetime.now(offers.HEL).date() + timedelta(days=5)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"price__{CUSTOMER}__{future_day.isoformat()}.json").write_text(
        json.dumps({milk.product_id: _price_item(milk.product_id, 1.5)})
    )

    def avail_timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("availability timeout", request=req)

    _install_transport(monkeypatch, avail_timeout)

    need_payload = {
        "customer": CUSTOMER,
        "deliver_by": future_day.isoformat(),
        "items": [{"query": "milk", "quantity": 5, "unit": "l", "constraints": {"lactose_free": True}}],
    }
    quote_payload = {
        "offers": [{"product_id": milk.product_id, "quantity": 5}],
        "delivery_date": future_day.isoformat(),
    }

    with TestClient(app) as client:
        r_offers = client.post("/v1/offers", json=need_payload, headers=AGENT_HEADERS)
        assert r_offers.status_code == 503, r_offers.text
        assert r_offers.json()["code"] == "UPSTREAM_UNAVAILABLE"

        r_quotes = client.post("/v1/quotes", json=quote_payload, headers=AGENT_HEADERS)
        assert r_quotes.status_code == 503, r_quotes.text
        assert r_quotes.json()["code"] == "UPSTREAM_UNAVAILABLE"

        sid = _mcp_init(client)
        mcp_offers = _mcp_call(client, sid, 2, "get_offers", need_payload, AGENT_HEADERS)
        assert mcp_offers.get("isError") is True
        assert "UPSTREAM_UNAVAILABLE" in json.dumps(mcp_offers.get("content", []))

        mcp_quote = _mcp_call(client, sid, 3, "request_quote", quote_payload, AGENT_HEADERS)
        assert mcp_quote.get("isError") is True
        assert "UPSTREAM_UNAVAILABLE" in json.dumps(mcp_quote.get("content", []))


def test_batching_deduplication_atomic_cache_write_and_warm_cache_zero_http(tmp_path, monkeypatch, sleeps):
    assert valio.CHUNK == 1000
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"price__{CUSTOMER}__{DAY.isoformat()}.json"

    precached = {str(i): _price_item(i, 1.25) for i in range(150)}
    cache_path.write_text(json.dumps(precached))

    atomic_moves: list[tuple[str, str, bool]] = []
    orig_replace = Path.replace

    def tracked_replace(self: Path, target: Path):
        if self.parent == cache_dir:
            parsed = json.loads(self.read_text())
            atomic_moves.append((self.name, Path(target).name, len(parsed) == 2500))
        return orig_replace(self, target)

    monkeypatch.setattr(Path, "replace", tracked_replace)

    sent_chunks: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["customer"] == CUSTOMER
        assert body["requestedDate"] == DAY.isoformat()
        products = body["products"]
        sent_chunks.append(products)
        return httpx.Response(200, json={"products": [_price_item(pid, 2.75) for pid in products]})

    _install_transport(monkeypatch, handler)

    mixed_request_ids: list[int | str] = []
    mixed_request_ids.extend(range(75))
    mixed_request_ids.extend(str(i) for i in range(75, 150))
    mixed_request_ids.extend([0, "0", 42, "100", 149, "149"])
    mixed_request_ids.extend(range(150, 1300))
    mixed_request_ids.extend([150, 250, 999, 1299])
    mixed_request_ids.extend(str(i) for i in range(1300, 2500))
    mixed_request_ids.extend(["1300", "1800", "2499"])

    result = valio.price(CUSTOMER, DAY, mixed_request_ids)

    assert [len(c) for c in sent_chunks] == [1000, 1000, 350]
    assert all(len(c) <= valio.CHUNK for c in sent_chunks)
    flattened = [pid for c in sent_chunks for pid in c]
    assert len(flattened) == 2350
    assert len(set(flattened)) == 2350
    assert flattened == [str(i) for i in range(150, 2500)]
    assert all(isinstance(pid, str) for pid in flattened)

    assert len(result) == 2500
    assert result["0"]["catalogPrice"]["value"] == 1.25
    assert result["149"]["catalogPrice"]["value"] == 1.25
    assert result["150"]["catalogPrice"]["value"] == 2.75
    assert result["2499"]["catalogPrice"]["value"] == 2.75

    assert atomic_moves == [(f"price__{CUSTOMER}__{DAY.isoformat()}.tmp", cache_path.name, True)]
    assert cache_path.exists()
    assert not cache_path.with_suffix(".tmp").exists()
    assert list(cache_dir.glob("*.tmp")) == []

    warm_all = valio.price(CUSTOMER, DAY, [i if i % 2 == 0 else str(i) for i in range(2500)])
    assert len(warm_all) == 2500
    assert len(sent_chunks) == 3

    warm_subset = valio.price(CUSTOMER, DAY, [10, "500", 1999, "2499"])
    assert set(warm_subset) == {"10", "500", "1999", "2499"}
    assert len(sent_chunks) == 3

    empty_res = valio.price(CUSTOMER, DAY, [])
    assert empty_res == {}
    assert len(sent_chunks) == 3
    assert sleeps == []


def test_availability_batching_and_warm_cache_zero_http(tmp_path, monkeypatch, sleeps):
    cache_dir = tmp_path / "cache"
    avail_chunks: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/availability"
        products = json.loads(req.content)["products"]
        avail_chunks.append(products)
        return httpx.Response(200, json={"products": [_avail_item(pid, DAY, 25.0) for pid in products]})

    _install_transport(monkeypatch, handler)

    out = valio.availability(CUSTOMER, DAY, [*range(1000), *(str(i) for i in range(1000, 2050)), 5, "1500"])
    assert [len(c) for c in avail_chunks] == [1000, 1000, 50]
    assert len(out) == 2050
    assert list(cache_dir.glob("*.tmp")) == []

    cached_again = valio.availability(CUSTOMER, DAY, [0, "1024", 2049])
    assert set(cached_again) == {"0", "1024", "2049"}
    assert len(avail_chunks) == 3
    assert sleeps == []

