from datetime import date, datetime, time, timedelta, timezone
import json
import warnings
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from aimo import allocate, catalog, keys, offers, quotes, valio
from aimo.app import app
from aimo.models import (
    Constraint,
    Mandate,
    Need,
    NeedItem,
    Product,
    QuoteRequestLine,
    SimulateStock,
    UnitSize,
)
from aimo.quotes import QuoteError

HEL = ZoneInfo("Europe/Helsinki")
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
AGENT_HEADERS = {"X-Agent-Key": "demo-hotel-aurora"}


def _product(
    pid: str,
    name: str = "Valio Lactose Free Milk 1L",
    brand: str = "Valio",
    allergens: list[str] | None = None,
    labels: list[str] | None = None,
    size_l: float = 1.0,
) -> Product:
    return Product(
        product_id=pid,
        name=name,
        brand=brand,
        vendor="Valio Oy",
        category="10400",
        sales_unit="ST",
        unit_size=UnitSize(value=size_l, unit="l"),
        allergens=allergens if allergens is not None else ["milk"],
        labels=labels if labels is not None else ["free_from_lactose"],
        origin="FI",
    )


def _price_entry(pid: str, net: float = 1.40, gross: float | None = None) -> dict:
    g = gross if gross is not None else round(net * 1.14, 2)
    return {
        "productId": pid,
        "currency": "EUR",
        "catalogPrice": {"value": net},
        "priceWithTax": {"value": g},
    }


def _avail_entry(
    pid: str,
    available_date: date | str | None,
    amount: float = 500.0,
    closing: str | None = "2026-11-15T15:00:00+02:00",
    sales_block: bool = False,
    extra_lines: list[dict] | None = None,
) -> dict:
    ad_str = available_date.isoformat() if isinstance(available_date, date) else available_date
    line: dict = {
        "salesBlock": sales_block,
        "availableAmount": amount,
        "salesUnit": "ST",
    }
    if ad_str is not None:
        line["availableDate"] = ad_str
    lines = [line]
    if extra_lines:
        lines.extend(extra_lines)
    out: dict = {
        "productId": pid,
        "salesBlock": sales_block,
        "scheduleLines": lines,
    }
    if closing is not None:
        out["closingTime"] = closing
    return out


def _wire(
    monkeypatch,
    products: list[Product],
    prices: dict[str, dict],
    avail: dict[str, dict],
    rel: dict[str, float] | None = None,
    co2: dict[str, float] | None = None,
) -> None:
    by_id = {p.product_id: p for p in products}
    rel_map = rel or {}
    co2_map = co2 or {}

    def fake_search(q: str = "", constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
        if "oat" in q.lower() and not any("oat" in p.name.lower() for p in products):
            return []
        return list(products[:limit])

    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(str(pid)))
    monkeypatch.setattr(catalog, "reliability", lambda pid: rel_map.get(str(pid)))
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: co2_map.get(str(pid)))
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {i: prices[i] for i in ids if i in prices})
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {i: avail[i] for i in ids if i in avail})


def _freeze_clock(monkeypatch, instant_utc: datetime) -> None:
    real_dt = datetime

    class FrozenDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant_utc.astimezone().replace(tzinfo=None)
            return instant_utc.astimezone(tz)

    monkeypatch.setattr(offers, "datetime", FrozenDateTime)
    monkeypatch.setattr(quotes, "datetime", FrozenDateTime)


def _last_sunday(year: int, month: int) -> date:
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() + 1) % 7
    return last - timedelta(days=offset)


class McpHelper:
    def __init__(self, client: TestClient):
        self.client = client
        self.sid: str | None = None
        self.seq = 0
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cutoff-test", "version": "1.0"},
            },
        )
        r = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**MCP_HEADERS, "mcp-session-id": self.sid or ""},
        )
        assert r.status_code in (200, 202, 204), r.text

    def rpc(self, method: str, params: dict | None = None, headers: dict | None = None) -> dict:
        self.seq += 1
        req_headers = dict(MCP_HEADERS)
        if self.sid and method != "initialize":
            req_headers["mcp-session-id"] = self.sid
        if headers:
            req_headers.update(headers)
        r = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params or {}},
            headers=req_headers,
        )
        assert r.status_code == 200, r.text
        self.sid = r.headers.get("mcp-session-id", self.sid)
        body = r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
        return json.loads(body)

    def call(self, name: str, arguments: dict, headers: dict | None = None) -> dict:
        out = self.rpc("tools/call", {"name": name, "arguments": arguments}, headers=headers)
        assert "result" in out, out
        return out["result"]

    def call_ok(self, name: str, arguments: dict, headers: dict | None = None) -> dict:
        res = self.call(name, arguments, headers=headers)
        assert not res.get("isError"), res
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        blocks = [json.loads(b["text"]) for b in res.get("content", [])]
        assert len(blocks) == 1
        return blocks[0]

    def call_err(self, name: str, arguments: dict, headers: dict | None = None) -> dict:
        res = self.call(name, arguments, headers=headers)
        assert res.get("isError") is True, res
        text = " ".join(b["text"] for b in res.get("content", []))
        prefix = f"Error executing tool {name}: "
        payload = text.split(prefix, 1)[-1]
        return json.loads(payload)


@pytest.fixture(autouse=True)
def isolated_offline_env(tmp_path, monkeypatch):
    warnings.filterwarnings("ignore", message="EdDSA is deprecated")
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "cutoff.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nonexistent_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("AIMO_ADMIN_KEY", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    valio._client = None
    keys._cache.clear()
    catalog.reset()
    allocate._data.cache_clear()
    offers.clear_simulation()
    monkeypatch.setattr(catalog, "search", lambda q="", constraints=None, limit=50: [])
    monkeypatch.setattr(catalog, "get", lambda pid: None)
    monkeypatch.setattr(catalog, "reliability", lambda pid: None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: None)
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {})
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: {})
    yield
    offers.clear_simulation()
    allocate._data.cache_clear()
    catalog.reset()
    keys._cache.clear()
    valio._client = None


@pytest.fixture
def future_day() -> date:
    return datetime.now(HEL).date() + timedelta(days=10)


@pytest.mark.parametrize(
    "closing_time,avail_date,day,expected_date,expected_clock,expected_offset_h,expected_tzname",
    [
        ("2026-11-15T15:00:00+02:00", date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 19), time(15, 0, 0), 2, "EET"),
        ("2026-11-15T17:00:00+0200", date(2026, 12, 10), date(2026, 12, 10), date(2026, 12, 9), time(17, 0, 0), 2, "EET"),
        ("2026-11-15T15:00:00+02:00", date(2026, 7, 20), date(2026, 7, 20), date(2026, 7, 19), time(15, 0, 0), 3, "EEST"),
        ("2026-07-15T15:00:00+03:00", date(2026, 7, 20), date(2026, 7, 20), date(2026, 7, 19), time(15, 0, 0), 3, "EEST"),
        ("2026-07-15T14:30:00+0300", date(2026, 8, 5), date(2026, 8, 5), date(2026, 8, 4), time(14, 30, 0), 3, "EEST"),
        ("2026-07-15T15:00:00+03:00", date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 19), time(15, 0, 0), 2, "EET"),
        ("2026-11-15T13:00:00Z", date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 19), time(15, 0, 0), 2, "EET"),
        ("2026-07-15T13:00:00Z", date(2026, 7, 20), date(2026, 7, 20), date(2026, 7, 19), time(16, 0, 0), 3, "EEST"),
        ("2026-11-15T10:15:45+00:00", date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 19), time(12, 15, 45), 2, "EET"),
        ("2026-11-15T14:00:00+01:00", date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 19), time(15, 0, 0), 2, "EET"),
        ("2026-11-15T08:00:00-05:00", date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 19), time(15, 0, 0), 2, "EET"),
    ],
)
def test_cutoff_converts_eet_eest_utc_and_foreign_offsets_to_helsinki(
    closing_time,
    avail_date,
    day,
    expected_date,
    expected_clock,
    expected_offset_h,
    expected_tzname,
):
    got = offers.cutoff(closing_time, avail_date, day)
    assert got.tzinfo == HEL
    assert got.date() == expected_date
    assert got.timetz().replace(tzinfo=None) == expected_clock
    assert got.utcoffset() == timedelta(hours=expected_offset_h)
    assert got.tzname() == expected_tzname


@pytest.mark.parametrize(
    "closing_time,avail_date,expected_dt",
    [
        (
            "2026-11-15T00:00:00+02:00",
            date(2026, 11, 20),
            datetime(2026, 11, 19, 0, 0, 0, tzinfo=HEL),
        ),
        (
            "2026-11-15T22:00:00Z",
            date(2026, 11, 20),
            datetime(2026, 11, 19, 0, 0, 0, tzinfo=HEL),
        ),
        (
            "2026-07-15T21:00:00Z",
            date(2026, 7, 20),
            datetime(2026, 7, 19, 0, 0, 0, tzinfo=HEL),
        ),
        (
            "2026-11-15T23:59:59+02:00",
            date(2026, 11, 20),
            datetime(2026, 11, 19, 23, 59, 59, tzinfo=HEL),
        ),
        (
            "2026-07-15T23:59:59+03:00",
            date(2026, 7, 20),
            datetime(2026, 7, 19, 23, 59, 59, tzinfo=HEL),
        ),
        (
            "2026-12-15T00:00:00+02:00",
            date(2027, 1, 1),
            datetime(2026, 12, 31, 0, 0, 0, tzinfo=HEL),
        ),
        (
            "2027-02-15T23:59:59+02:00",
            date(2027, 3, 1),
            datetime(2027, 2, 28, 23, 59, 59, tzinfo=HEL),
        ),
        (
            "2028-02-15T23:59:59+02:00",
            date(2028, 3, 1),
            datetime(2028, 2, 29, 23, 59, 59, tzinfo=HEL),
        ),
    ],
)
def test_cutoff_midnight_end_of_day_and_calendar_rollovers(closing_time, avail_date, expected_dt):
    got = offers.cutoff(closing_time, avail_date, date(2026, 11, 25))
    assert got == expected_dt
    assert got.tzinfo == HEL
    assert (got.hour, got.minute, got.second) == (expected_dt.hour, expected_dt.minute, expected_dt.second)


def test_cutoff_string_vs_date_available_date_and_none_fallback_to_day():
    closing = "2026-11-15T16:45:00+02:00"
    fallback_day = date(2026, 11, 25)
    target_day = date(2026, 11, 22)

    from_date = offers.cutoff(closing, target_day, fallback_day)
    from_str = offers.cutoff(closing, "2026-11-22", fallback_day)
    assert from_date == from_str
    assert from_date == datetime(2026, 11, 21, 16, 45, 0, tzinfo=HEL)
    assert from_str.tzinfo == HEL

    later_from_str = offers.cutoff(closing, "2026-11-30", fallback_day)
    assert later_from_str == datetime(2026, 11, 29, 16, 45, 0, tzinfo=HEL)

    from_none = offers.cutoff(closing, None, fallback_day)
    assert from_none == datetime(2026, 11, 24, 16, 45, 0, tzinfo=HEL)
    assert from_none.tzinfo == HEL


@pytest.mark.parametrize("year", [2026, 2027])
def test_cutoff_finnish_spring_dst_transition_last_sunday_of_march(year):
    spring_sunday = _last_sunday(year, 3)
    assert spring_sunday.weekday() == 6
    saturday = spring_sunday - timedelta(days=1)
    monday = spring_sunday + timedelta(days=1)

    co_for_sunday = offers.cutoff(f"{year}-03-20T15:00:00+02:00", spring_sunday, spring_sunday)
    assert co_for_sunday.date() == saturday
    assert co_for_sunday.timetz().replace(tzinfo=None) == time(15, 0)
    assert co_for_sunday.utcoffset() == timedelta(hours=2)
    assert co_for_sunday.tzname() == "EET"

    co_sunday_pre_jump = offers.cutoff(f"{year}-03-20T02:30:00+02:00", monday, monday)
    assert co_sunday_pre_jump.date() == spring_sunday
    assert co_sunday_pre_jump.timetz().replace(tzinfo=None) == time(2, 30)
    assert co_sunday_pre_jump.utcoffset() == timedelta(hours=2)
    assert co_sunday_pre_jump.tzname() == "EET"

    co_sunday_post_jump = offers.cutoff(f"{year}-03-20T04:00:00+02:00", monday, monday)
    assert co_sunday_post_jump.date() == spring_sunday
    assert co_sunday_post_jump.timetz().replace(tzinfo=None) == time(4, 0)
    assert co_sunday_post_jump.utcoffset() == timedelta(hours=3)
    assert co_sunday_post_jump.tzname() == "EEST"

    co_for_monday = offers.cutoff(f"{year}-03-20T15:00:00+02:00", monday, monday)
    assert co_for_monday.date() == spring_sunday
    assert co_for_monday.timetz().replace(tzinfo=None) == time(15, 0)
    assert co_for_monday.utcoffset() == timedelta(hours=3)
    assert co_for_monday.tzname() == "EEST"

    elapsed_utc = co_for_monday.astimezone(timezone.utc) - co_for_sunday.astimezone(timezone.utc)
    assert elapsed_utc == timedelta(hours=23)


@pytest.mark.parametrize("year", [2026, 2027])
def test_cutoff_finnish_autumn_dst_transition_last_sunday_of_october(year):
    autumn_sunday = _last_sunday(year, 10)
    assert autumn_sunday.weekday() == 6
    saturday = autumn_sunday - timedelta(days=1)
    monday = autumn_sunday + timedelta(days=1)

    co_for_sunday = offers.cutoff(f"{year}-10-15T15:00:00+03:00", autumn_sunday, autumn_sunday)
    assert co_for_sunday.date() == saturday
    assert co_for_sunday.timetz().replace(tzinfo=None) == time(15, 0)
    assert co_for_sunday.utcoffset() == timedelta(hours=3)
    assert co_for_sunday.tzname() == "EEST"

    co_sunday_pre_fallback = offers.cutoff(f"{year}-10-15T02:00:00+03:00", monday, monday)
    assert co_sunday_pre_fallback.date() == autumn_sunday
    assert co_sunday_pre_fallback.timetz().replace(tzinfo=None) == time(2, 0)
    assert co_sunday_pre_fallback.utcoffset() == timedelta(hours=3)
    assert co_sunday_pre_fallback.tzname() == "EEST"

    co_for_monday = offers.cutoff(f"{year}-10-15T15:00:00+03:00", monday, monday)
    assert co_for_monday.date() == autumn_sunday
    assert co_for_monday.timetz().replace(tzinfo=None) == time(15, 0)
    assert co_for_monday.utcoffset() == timedelta(hours=2)
    assert co_for_monday.tzname() == "EET"

    elapsed_utc = co_for_monday.astimezone(timezone.utc) - co_for_sunday.astimezone(timezone.utc)
    assert elapsed_utc == timedelta(hours=25)


@pytest.mark.parametrize("use_utc_now", [False, True])
def test_get_offers_exact_cutoff_boundary_vs_one_second_before(monkeypatch, future_day, use_utc_now):
    p1 = _product("464073", "Valio Lactose Free Milk 1L")
    closing = "2026-11-15T15:00:00+02:00"
    _wire(
        monkeypatch,
        [p1],
        {"464073": _price_entry("464073", 1.25)},
        {"464073": _avail_entry("464073", future_day, 300.0, closing=closing)},
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="lactose-free milk", quantity=60, unit="L")],
    )
    exact_co = offers.cutoff(closing, future_day, future_day)
    assert exact_co == datetime.combine(future_day - timedelta(days=1), time(15, 0), tzinfo=HEL)

    one_sec_before = exact_co - timedelta(seconds=1)
    if use_utc_now:
        one_sec_before = one_sec_before.astimezone(timezone.utc)
    g_open = offers.get_offers(need, now=one_sec_before).items[0]
    assert g_open.shortfall == 0.0
    assert len(g_open.offers) == 1
    assert g_open.offers[0].product_id == "464073"
    assert g_open.offers[0].available_date == future_day
    assert g_open.offers[0].units_needed == 60.0
    assert "cutoff computed from Valio closing time 15:00, day before delivery" in g_open.offers[0].notes
    assert not any(n.startswith("cutoff passed") for n in g_open.offers[0].notes)
    assert g_open.options == []

    at_cutoff = exact_co.astimezone(timezone.utc) if use_utc_now else exact_co
    g_closed = offers.get_offers(need, now=at_cutoff).items[0]
    assert g_closed.offers == []
    assert g_closed.shortfall == 60.0
    expected_late_date = future_day + timedelta(days=1)
    assert g_closed.options == [
        {
            "kind": "late",
            "date": expected_late_date,
            "days_late": 1,
            "why": f"all 60 L available by {expected_late_date}, 1 day after {future_day}",
        }
    ]


@pytest.mark.parametrize(
    "days_past,at_exact_next_cutoff,expected_days_late",
    [
        (0, False, 1),
        (1, False, 2),
        (1, True, 2),
        (2, False, 3),
        (4, False, 5),
    ],
)
def test_get_offers_rolls_past_cutoff_forward_to_first_open_date(
    monkeypatch,
    future_day,
    days_past,
    at_exact_next_cutoff,
    expected_days_late,
):
    p1 = _product("464119", "Valio Fresh Cream 1L")
    closing = "2026-11-12T14:30:00+0200"
    _wire(
        monkeypatch,
        [p1],
        {"464119": _price_entry("464119", 2.10)},
        {"464119": _avail_entry("464119", future_day, 200.0, closing=closing)},
    )
    base_co = offers.cutoff(closing, future_day, future_day)
    if at_exact_next_cutoff:
        test_now = offers.cutoff(closing, future_day + timedelta(days=days_past), future_day)
    else:
        test_now = base_co + timedelta(days=days_past, minutes=15)

    need_strict = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="cream", quantity=40, unit="L")],
    )
    g_strict = offers.get_offers(need_strict, now=test_now).items[0]
    assert g_strict.offers == []
    assert g_strict.shortfall == 40.0

    rolled_date = future_day + timedelta(days=expected_days_late)
    plural = "" if expected_days_late == 1 else "s"
    assert g_strict.options == [
        {
            "kind": "late",
            "date": rolled_date,
            "days_late": expected_days_late,
            "why": f"all 40 L available by {rolled_date}, {expected_days_late} day{plural} after {future_day}",
        }
    ]

    priced = offers._priced(
        "hotel-aurora",
        future_day,
        need_strict.items[0],
        [p1],
        {"464119": 1.0},
        now=test_now,
    )
    assert len(priced) == 1
    assert priced[0].available_date == rolled_date
    rolled_co = offers.cutoff(closing, rolled_date, future_day)
    assert rolled_co > test_now
    assert priced[0].closing_time == rolled_co.isoformat()
    assert priced[0].notes == [
        f"cutoff passed for {future_day}, earliest delivery {rolled_date}",
        "cutoff computed from Valio closing time 14:30, day before delivery",
    ]


def test_get_offers_mixed_cutoffs_never_allocates_closed_product_on_time_and_surfaces_split(
    monkeypatch,
    future_day,
):
    p_stock = _product("464073", "Valio Lactose Free Milk 1L")
    p_terminal = _product("464119", "Valio Lactose Free Milk Terminal 1L")
    _wire(
        monkeypatch,
        [p_stock, p_terminal],
        {
            "464073": _price_entry("464073", 1.30),
            "464119": _price_entry("464119", 1.05),
        },
        {
            "464073": _avail_entry("464073", future_day, 40.0, closing="2026-11-15T17:00:00+0200"),
            "464119": _avail_entry("464119", future_day, 200.0, closing="2026-11-15T15:00:00+0200"),
        },
    )
    mandate = Mandate(
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        per_order_eur=2500.0,
        per_week_eur=15000.0,
    )
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="lactose-free milk", quantity=100, unit="L")],
    )
    between_cutoffs = datetime.combine(future_day - timedelta(days=1), time(16, 0), tzinfo=HEL)

    group = offers.get_offers(need, mandate=mandate, now=between_cutoffs).items[0]
    assert [(o.product_id, o.units_needed, o.available_date) for o in group.offers] == [
        ("464073", 40.0, future_day)
    ]
    assert group.shortfall == 60.0
    assert "cutoff computed from Valio closing time 17:00, day before delivery" in group.offers[0].notes
    assert any("464119" in n and f"after deliver_by {future_day}" in n for n in group.offers[0].notes)

    by_kind = {opt["kind"]: opt for opt in group.options}
    assert set(by_kind) == {"partial", "late", "split"}
    next_day = future_day + timedelta(days=1)
    assert by_kind["partial"] == {
        "kind": "partial",
        "date": future_day,
        "covered": 40.0,
        "shortfall": 60.0,
        "why": f"40 L of 100 L by {future_day}, 60 L short",
    }
    assert by_kind["late"] == {
        "kind": "late",
        "date": next_day,
        "days_late": 1,
        "why": f"all 100 L available by {next_day}, 1 day after {future_day}",
    }
    assert by_kind["split"] == {
        "kind": "split",
        "date": next_day,
        "on_time": 40.0,
        "later": 60.0,
        "days_late": 1,
        "why": f"40 L by {future_day}, remaining 60 L on {next_day}, 1 day late",
    }


def test_get_offers_missing_available_date_falls_back_to_day_when_rolling_past_cutoff(
    monkeypatch,
    future_day,
):
    p1 = _product("464201", "Valio Milk 1L")
    _wire(
        monkeypatch,
        [p1],
        {"464201": _price_entry("464201", 1.20)},
        {"464201": _avail_entry("464201", None, 150.0, closing="2026-11-15T12:00:00+02:00")},
    )
    past_noon = datetime.combine(future_day - timedelta(days=1), time(12, 0), tzinfo=HEL)
    need = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="milk", quantity=50, unit="L")],
    )
    g = offers.get_offers(need, now=past_noon).items[0]
    assert g.offers == []
    assert g.shortfall == 50.0
    assert g.options == [
        {
            "kind": "late",
            "date": future_day + timedelta(days=1),
            "days_late": 1,
            "why": f"all 50 L available by {future_day + timedelta(days=1)}, 1 day after {future_day}",
        }
    ]

    need_next = Need(
        customer="hotel-aurora",
        deliver_by=future_day + timedelta(days=1),
        items=[NeedItem(query="milk", quantity=50, unit="L")],
    )
    priced = offers._priced("hotel-aurora", future_day, need_next.items[0], [p1], {"464201": 1.0}, now=past_noon)
    assert priced[0].available_date == future_day + timedelta(days=1)
    assert priced[0].notes == [
        f"cutoff passed for {future_day}, earliest delivery {future_day + timedelta(days=1)}",
        "cutoff computed from Valio closing time 12:00, day before delivery",
    ]


def test_get_offers_rolls_across_2027_spring_and_autumn_dst_preserving_helsinki_clock(monkeypatch):
    p1 = _product("464073", "Valio Lactose Free Milk 1L")

    spring_sun = date(2027, 3, 28)
    _wire(
        monkeypatch,
        [p1],
        {"464073": _price_entry("464073", 1.30)},
        {"464073": _avail_entry("464073", spring_sun, 200.0, closing="2027-03-20T15:00:00+02:00")},
    )
    sat_after_cutoff = datetime(2027, 3, 27, 15, 0, 0, tzinfo=HEL)
    need_spring = Need(
        customer="hotel-aurora",
        deliver_by=spring_sun,
        items=[NeedItem(query="lactose-free milk", quantity=25, unit="L")],
    )
    g_spring = offers.get_offers(need_spring, now=sat_after_cutoff).items[0]
    assert g_spring.offers == []
    assert g_spring.options[0]["kind"] == "late"
    assert g_spring.options[0]["date"] == date(2027, 3, 29)
    assert g_spring.options[0]["days_late"] == 1

    priced_spring = offers._priced("hotel-aurora", spring_sun, need_spring.items[0], [p1], {"464073": 1.0}, now=sat_after_cutoff)
    assert priced_spring[0].available_date == date(2027, 3, 29)
    assert priced_spring[0].closing_time == "2027-03-28T15:00:00+03:00"
    assert "cutoff computed from Valio closing time 15:00, day before delivery" in priced_spring[0].notes

    autumn_sun = date(2027, 10, 31)
    _wire(
        monkeypatch,
        [p1],
        {"464073": _price_entry("464073", 1.30)},
        {"464073": _avail_entry("464073", autumn_sun, 200.0, closing="2027-10-20T15:00:00+03:00")},
    )
    oct_sat_after_cutoff = datetime(2027, 10, 30, 15, 0, 0, tzinfo=HEL)
    need_autumn = Need(
        customer="hotel-aurora",
        deliver_by=autumn_sun,
        items=[NeedItem(query="lactose-free milk", quantity=25, unit="L")],
    )
    priced_autumn = offers._priced(
        "hotel-aurora",
        autumn_sun,
        need_autumn.items[0],
        [p1],
        {"464073": 1.0},
        now=oct_sat_after_cutoff,
    )
    assert priced_autumn[0].available_date == date(2027, 11, 1)
    assert priced_autumn[0].closing_time == "2027-10-31T15:00:00+02:00"
    assert "cutoff computed from Valio closing time 15:00, day before delivery" in priced_autumn[0].notes


def test_price_lines_rest_and_mcp_reject_past_delivery_date_and_accept_today_in_helsinki(monkeypatch):
    p1 = _product("464073", "Valio Lactose Free Milk 1L")
    monkeypatch.setattr(catalog, "get", lambda pid: p1 if str(pid) == "464073" else None)
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {"464073": _price_entry("464073", 1.50, 1.71)})
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {"464073": _avail_entry("464073", d, 500.0, closing="2026-11-15T17:00:00+02:00")},
    )

    hel_today = datetime.now(HEL).date()
    hel_yesterday = hel_today - timedelta(days=1)

    with pytest.raises(QuoteError) as exc:
        offers.price_lines("hotel-aurora", hel_yesterday, [QuoteRequestLine(product_id="464073", quantity=10)])
    assert exc.value.code == "INVALID_QUOTE"
    assert "past" in exc.value.text.lower()

    lines, gross, valid = offers.price_lines(
        "hotel-aurora",
        hel_today,
        [QuoteRequestLine(product_id="464073", quantity=10)],
    )
    assert len(lines) == 1
    assert lines[0].line_total_net == 15.0
    assert gross == 17.1
    assert valid == datetime.combine(hel_today - timedelta(days=1), time(17, 0), tzinfo=HEL)

    with TestClient(app) as client:
        r_past = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "464073", "quantity": 10}], "delivery_date": hel_yesterday.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert r_past.status_code == 409
        assert r_past.json()["code"] == "INVALID_QUOTE"

        r_today = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "464073", "quantity": 10}], "delivery_date": hel_today.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert r_today.status_code == 200, r_today.text
        q_rest = r_today.json()
        assert q_rest["delivery_date"] == hel_today.isoformat()
        assert q_rest["total_net"] == 15.0
        verified_rest = quotes.verify(q_rest["jws"])
        assert verified_rest.delivery_date == hel_today

        mcp = McpHelper(client)
        err_mcp = mcp.call_err(
            "request_quote",
            {"offers": [{"product_id": "464073", "quantity": 10}], "delivery_date": hel_yesterday.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert err_mcp["code"] == "INVALID_QUOTE"

        ok_mcp = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "464073", "quantity": 10}], "delivery_date": hel_today.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert ok_mcp["delivery_date"] == hel_today.isoformat()
        verified_mcp = quotes.verify(ok_mcp["jws"])
        assert verified_mcp.delivery_date == hel_today


@pytest.mark.parametrize(
    "instant_utc,rejected_date,accepted_date",
    [
        (datetime(2026, 11, 15, 22, 30, 0, tzinfo=timezone.utc), date(2026, 11, 15), date(2026, 11, 16)),
        (datetime(2026, 7, 15, 21, 15, 0, tzinfo=timezone.utc), date(2026, 7, 15), date(2026, 7, 16)),
        (datetime(2026, 7, 15, 20, 59, 59, tzinfo=timezone.utc), date(2026, 7, 14), date(2026, 7, 15)),
    ],
)
def test_quotes_helsinki_midnight_boundary_when_utc_date_differs(
    monkeypatch,
    instant_utc,
    rejected_date,
    accepted_date,
):
    _freeze_clock(monkeypatch, instant_utc)
    p1 = _product("464073", "Valio Lactose Free Milk 1L")
    monkeypatch.setattr(catalog, "get", lambda pid: p1 if str(pid) == "464073" else None)
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {"464073": _price_entry("464073", 1.20, 1.37)})
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {"464073": _avail_entry("464073", d, 200.0, closing="2026-11-15T16:00:00+02:00")},
    )

    with pytest.raises(QuoteError) as exc:
        offers.price_lines("hotel-aurora", rejected_date, [QuoteRequestLine(product_id="464073", quantity=5)])
    assert exc.value.code == "INVALID_QUOTE"

    lines, gross, _ = offers.price_lines(
        "hotel-aurora",
        accepted_date,
        [QuoteRequestLine(product_id="464073", quantity=5)],
    )
    assert lines[0].line_total_net == 6.0
    assert gross == 6.85

    with TestClient(app) as client:
        r_bad = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "464073", "quantity": 5}], "delivery_date": rejected_date.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert r_bad.status_code == 409
        assert r_bad.json()["code"] == "INVALID_QUOTE"

        r_ok = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "464073", "quantity": 5}], "delivery_date": accepted_date.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert r_ok.status_code == 200, r_ok.text
        assert r_ok.json()["delivery_date"] == accepted_date.isoformat()

        mcp = McpHelper(client)
        mcp_bad = mcp.call_err(
            "request_quote",
            {"offers": [{"product_id": "464073", "quantity": 5}], "delivery_date": rejected_date.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert mcp_bad["code"] == "INVALID_QUOTE"

        mcp_ok = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "464073", "quantity": 5}], "delivery_date": accepted_date.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert mcp_ok["delivery_date"] == accepted_date.isoformat()


def test_price_lines_rest_and_mcp_multiline_picks_min_closes_as_valid_until(monkeypatch, future_day):
    products = {
        "p_late": _product("p_late", "Stock Milk 1L"),
        "p_mid": _product("p_mid", "Yogurt 1L"),
        "p_early": _product("p_early", "Terminal Cream 1L"),
        "p_no_ct": _product("p_no_ct", "Butter 1kg"),
    }
    monkeypatch.setattr(catalog, "get", lambda pid: products.get(str(pid)))
    monkeypatch.setattr(
        valio,
        "price",
        lambda c, d, ids: {
            "p_late": _price_entry("p_late", 1.20, 1.37),
            "p_mid": _price_entry("p_mid", 2.00, 2.28),
            "p_early": _price_entry("p_early", 3.00, 3.42),
            "p_no_ct": _price_entry("p_no_ct", 4.00, 4.56),
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {
            "p_late": _avail_entry("p_late", d, 200.0, closing="2026-11-15T17:00:00+02:00"),
            "p_mid": _avail_entry("p_mid", d, 200.0, closing="2026-11-15T12:00:00Z"),
            "p_early": _avail_entry("p_early", d, 200.0, closing="2026-11-15T11:15:00+02:00"),
            "p_no_ct": _avail_entry("p_no_ct", d, 200.0, closing=None),
        },
    )

    req_lines = [
        QuoteRequestLine(product_id="p_late", quantity=10),
        QuoteRequestLine(product_id="p_mid", quantity=5),
        QuoteRequestLine(product_id="p_early", quantity=2),
        QuoteRequestLine(product_id="p_no_ct", quantity=1),
    ]
    lines, gross, valid = offers.price_lines("hotel-aurora", future_day, req_lines)
    expected_min = datetime.combine(future_day - timedelta(days=1), time(11, 15), tzinfo=HEL)
    assert len(lines) == 4
    assert gross == round(10 * 1.37 + 5 * 2.28 + 2 * 3.42 + 1 * 4.56, 2)
    assert valid == expected_min
    assert valid.tzinfo == HEL

    payload_offers = [
        {"product_id": "p_late", "quantity": 10},
        {"product_id": "p_mid", "quantity": 5},
        {"product_id": "p_early", "quantity": 2},
        {"product_id": "p_no_ct", "quantity": 1},
    ]
    with TestClient(app) as client:
        r_rest = client.post(
            "/v1/quotes",
            json={"offers": payload_offers, "delivery_date": future_day.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert r_rest.status_code == 200, r_rest.text
        body_rest = r_rest.json()
        assert datetime.fromisoformat(body_rest["valid_until"]) == expected_min
        assert quotes.verify(body_rest["jws"]).valid_until == expected_min

        mcp = McpHelper(client)
        body_mcp = mcp.call_ok(
            "request_quote",
            {"offers": payload_offers, "delivery_date": future_day.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert datetime.fromisoformat(body_mcp["valid_until"]) == expected_min
        assert quotes.verify(body_mcp["jws"]).valid_until == expected_min


def test_price_lines_min_closes_accounts_for_earlier_schedule_line_date(monkeypatch, future_day):
    earlier_day = future_day - timedelta(days=1)
    monkeypatch.setattr(
        valio,
        "price",
        lambda c, d, ids: {
            "p_same_day": _price_entry("p_same_day", 1.50, 1.71),
            "p_prev_day": _price_entry("p_prev_day", 1.80, 2.05),
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {
            "p_same_day": _avail_entry("p_same_day", future_day, 100.0, closing="2026-11-15T10:00:00+02:00"),
            "p_prev_day": _avail_entry("p_prev_day", earlier_day, 100.0, closing="2026-11-15T18:00:00+02:00"),
        },
    )

    _, _, valid = offers.price_lines(
        "hotel-aurora",
        future_day,
        [
            QuoteRequestLine(product_id="p_same_day", quantity=4),
            QuoteRequestLine(product_id="p_prev_day", quantity=4),
        ],
    )
    expected = datetime.combine(earlier_day - timedelta(days=1), time(18, 0), tzinfo=HEL)
    assert valid == expected


@pytest.mark.parametrize(
    "avail_payload",
    [
        {},
        {"p1": {"productId": "p1", "closingTime": None, "scheduleLines": [{"availableDate": "2027-01-20"}]}},
        {"p1": {"productId": "p1", "closingTime": "", "scheduleLines": [{"availableDate": "2027-01-20"}]}},
        {"p1": {"productId": "p1", "scheduleLines": []}},
    ],
)
def test_price_lines_rest_and_mcp_fallback_to_noon_day_before_when_closing_time_absent(
    monkeypatch,
    future_day,
    avail_payload,
):
    p1 = _product("p1", "Valio Butter 500g")
    monkeypatch.setattr(catalog, "get", lambda pid: p1 if str(pid) == "p1" else None)
    monkeypatch.setattr(
        valio,
        "price",
        lambda c, d, ids: {
            "p1": _price_entry("p1", 2.50, 2.85),
            "p2": _price_entry("p2", 1.00, 1.14),
        },
    )
    monkeypatch.setattr(valio, "availability", lambda c, d, ids: avail_payload)

    req_lines = [
        QuoteRequestLine(product_id="p1", quantity=3),
        QuoteRequestLine(product_id="p2", quantity=2),
    ]
    _, _, valid = offers.price_lines("hotel-aurora", future_day, req_lines)

    expected_noon = offers._local_noon(future_day - timedelta(days=1))
    assert valid == expected_noon
    assert valid.tzinfo is not None
    assert valid.date() == future_day - timedelta(days=1)
    assert (valid.hour, valid.minute, valid.second, valid.microsecond) == (12, 0, 0, 0)

    with TestClient(app) as client:
        r_rest = client.post(
            "/v1/quotes",
            json={
                "offers": [{"product_id": "p1", "quantity": 3}, {"product_id": "p2", "quantity": 2}],
                "delivery_date": future_day.isoformat(),
            },
            headers=AGENT_HEADERS,
        )
        assert r_rest.status_code == 200, r_rest.text
        rest_valid = datetime.fromisoformat(r_rest.json()["valid_until"])
        assert rest_valid == expected_noon

        mcp = McpHelper(client)
        mcp_body = mcp.call_ok(
            "request_quote",
            {
                "offers": [{"product_id": "p1", "quantity": 3}, {"product_id": "p2", "quantity": 2}],
                "delivery_date": future_day.isoformat(),
            },
            headers=AGENT_HEADERS,
        )
        mcp_valid = datetime.fromisoformat(mcp_body["valid_until"])
        assert mcp_valid == expected_noon


def test_price_lines_empty_schedule_lines_with_closing_time_uses_delivery_date_cutoff(monkeypatch, future_day):
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {"p1": _price_entry("p1", 1.60, 1.82)})
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {
            "p1": {
                "productId": "p1",
                "closingTime": "2026-11-15T13:45:00+02:00",
                "scheduleLines": [],
            }
        },
    )
    _, _, valid = offers.price_lines("hotel-aurora", future_day, [QuoteRequestLine(product_id="p1", quantity=2)])
    assert valid == datetime.combine(future_day - timedelta(days=1), time(13, 45), tzinfo=HEL)


def test_simulate_stock_preserves_closing_time_cutoff_calculation(monkeypatch, future_day):
    p1 = _product("464073", "Valio Lactose Free Milk 1L")
    shifted_day = future_day + timedelta(days=2)
    _wire(
        monkeypatch,
        [p1],
        {"464073": _price_entry("464073", 1.35, 1.54)},
        {"464073": _avail_entry("464073", future_day, 500.0, closing="2026-11-15T16:30:00+02:00")},
    )
    offers.simulate(SimulateStock(product_id="464073", available_amount=80.0, available_date=shifted_day))

    _, _, valid = offers.price_lines("hotel-aurora", future_day, [QuoteRequestLine(product_id="464073", quantity=10)])
    assert valid == datetime.combine(shifted_day - timedelta(days=1), time(16, 30), tzinfo=HEL)


@pytest.mark.parametrize(
    "closing_time,avail_str,avail_dt,expected_offset_h",
    [
        ("2026-03-20T00:00:00+03:00", "2026-07-15", date(2026, 7, 15), 3),
        ("2026-03-20T15:00:00+02:00", "2026-03-30", date(2026, 3, 30), 3),
        ("2026-10-20T15:00:00+03:00", "2026-10-26", date(2026, 10, 26), 2),
    ],
)
def test_cutoff_string_vs_date_across_summer_midnight_and_dst_transitions(
    closing_time,
    avail_str,
    avail_dt,
    expected_offset_h,
):
    from_str = offers.cutoff(closing_time, avail_str, date(2026, 1, 10))
    from_dt = offers.cutoff(closing_time, avail_dt, date(2026, 12, 10))
    assert from_str == from_dt
    assert from_str.tzinfo == HEL
    assert from_str.utcoffset() == timedelta(hours=expected_offset_h)


@pytest.mark.parametrize(
    "closing_time,expected_clock,expected_hhmm",
    [
        ("2026-11-15T00:00:00+02:00", time(0, 0, 0), "00:00"),
        ("2026-11-15T23:59:59+02:00", time(23, 59, 59), "23:59"),
    ],
)
def test_get_offers_allocated_rolled_offer_exposes_notes_and_extreme_clock_boundaries(
    monkeypatch,
    future_day,
    closing_time,
    expected_clock,
    expected_hhmm,
):
    p1 = _product("464073", "Valio Lactose Free Milk 1L")
    initial_day = future_day - timedelta(days=1)
    _wire(
        monkeypatch,
        [p1],
        {"464073": _price_entry("464073", 1.25)},
        {"464073": _avail_entry("464073", initial_day, 250.0, closing=closing_time)},
    )
    exact_co = datetime.combine(initial_day - timedelta(days=1), expected_clock, tzinfo=HEL)

    need_initial = Need(
        customer="hotel-aurora",
        deliver_by=initial_day,
        items=[NeedItem(query="lactose-free milk", quantity=30, unit="L")],
    )
    g_before = offers.get_offers(need_initial, now=exact_co - timedelta(seconds=1)).items[0]
    assert len(g_before.offers) == 1
    assert g_before.offers[0].available_date == initial_day
    assert f"cutoff computed from Valio closing time {expected_hhmm}, day before delivery" in g_before.offers[0].notes

    need_rolled = Need(
        customer="hotel-aurora",
        deliver_by=future_day,
        items=[NeedItem(query="lactose-free milk", quantity=30, unit="L")],
    )
    g_rolled = offers.get_offers(need_rolled, now=exact_co).items[0]
    assert len(g_rolled.offers) == 1
    assert g_rolled.shortfall == 0.0
    assert g_rolled.offers[0].available_date == future_day
    assert g_rolled.offers[0].notes == [
        f"cutoff passed for {initial_day}, earliest delivery {future_day}",
        f"cutoff computed from Valio closing time {expected_hhmm}, day before delivery",
    ]


def test_today_quote_accepted_then_place_order_rejects_expired_yesterday_cutoff_on_rest_and_mcp(monkeypatch):
    p1 = _product("464073", "Valio Lactose Free Milk 1L")
    monkeypatch.setattr(catalog, "get", lambda pid: p1 if str(pid) == "464073" else None)
    monkeypatch.setattr(valio, "price", lambda c, d, ids: {"464073": _price_entry("464073", 1.50, 1.71)})
    monkeypatch.setattr(
        valio,
        "availability",
        lambda c, d, ids: {"464073": _avail_entry("464073", d, 500.0, closing="2026-11-15T15:00:00+02:00")},
    )
    hel_today = datetime.now(HEL).date()

    with TestClient(app) as client:
        q_rest = client.post(
            "/v1/quotes",
            json={"offers": [{"product_id": "464073", "quantity": 10}], "delivery_date": hel_today.isoformat()},
            headers=AGENT_HEADERS,
        )
        assert q_rest.status_code == 200
        quote_body = q_rest.json()
        expected_expired = datetime.combine(hel_today - timedelta(days=1), time(15, 0), tzinfo=HEL)
        assert datetime.fromisoformat(quote_body["valid_until"]) == expected_expired

        ord_rest = client.post(
            "/v1/orders",
            json={"quote_id": quote_body["quote_id"], "jws": quote_body["jws"]},
            headers=AGENT_HEADERS,
        )
        assert ord_rest.status_code == 409
        assert ord_rest.json()["code"] == "QUOTE_EXPIRED"

        mcp = McpHelper(client)
        q_mcp = mcp.call_ok(
            "request_quote",
            {"offers": [{"product_id": "464073", "quantity": 10}], "delivery_date": hel_today.isoformat()},
            headers=AGENT_HEADERS,
        )
        ord_mcp_err = mcp.call_err(
            "place_order",
            {"quote_id": q_mcp["quote_id"], "jws": q_mcp["jws"]},
            headers=AGENT_HEADERS,
        )
        assert ord_mcp_err["code"] == "QUOTE_EXPIRED"

