from datetime import date
from decimal import Decimal
import json

import httpx
import pytest

from agents import buyer, llm
from aimo.parse import parse_need


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)


def _set_env(monkeypatch, base="https://llm.example.test/v1", key="sk-test", model="gpt-test"):
    monkeypatch.setenv("LLM_BASE_URL", base)
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("LLM_MODEL", model)


def _candidate(pid="460396", name="Valio Milk", total=120.0, rel=0.95, avail=500, avail_date="2026-11-16", notes=None):
    return {
        "product_id": pid,
        "product": {"name": name},
        "total_net": total,
        "reliability": rel,
        "available_amount": avail,
        "available_date": avail_date,
        "notes": notes,
    }


def _http_reply(content="460396", status_code=200, payload=None, raw_text=None):
    req = httpx.Request("POST", "https://llm.example.test/v1/chat/completions")
    if raw_text is not None:
        return httpx.Response(status_code, text=raw_text, request=req)
    body = {"choices": [{"message": {"content": content}}]} if payload is None else payload
    return httpx.Response(status_code, json=body, request=req)


def test_available_false_when_unset():
    assert llm.available() is False


@pytest.mark.parametrize(
    "base,key,model",
    [
        ("", "sk-test", "gpt-test"),
        ("https://llm.example.test/v1", "", "gpt-test"),
        ("https://llm.example.test/v1", "sk-test", ""),
        ("   ", "sk-test", "gpt-test"),
        ("https://llm.example.test/v1", "\t\n ", "gpt-test"),
        ("https://llm.example.test/v1", "sk-test", "  \n"),
    ],
)
def test_available_false_when_blank_or_whitespace(monkeypatch, base, key, model):
    _set_env(monkeypatch, base=base, key=key, model=model)
    assert llm.available() is False


def test_available_true_when_all_set_including_padded_whitespace(monkeypatch):
    _set_env(monkeypatch, base="  https://llm.example.test/v1/  ", key="  sk-test  ", model="  gpt-test  ")
    assert llm.available() is True


def test_chat_strips_env_and_content(monkeypatch):
    _set_env(monkeypatch, base="  https://llm.example.test/v1/  ", key="  sk-test\n", model=" gpt-test ")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _http_reply("  hello kitchen  \n")

    monkeypatch.setattr(httpx, "post", fake_post)
    out = llm.chat("sys prompt", "user prompt")
    assert out == "hello kitchen"
    assert captured["url"] == "https://llm.example.test/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer sk-test"}
    assert captured["json"] == {
        "model": "gpt-test",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "user prompt"},
        ],
    }
    assert captured["timeout"] == 30


def test_chat_raises_when_env_missing_or_whitespace(monkeypatch):
    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: called.append(1))
    with pytest.raises(ValueError):
        llm.chat("sys", "user")
    _set_env(monkeypatch, base="   ", key="sk", model="m")
    with pytest.raises(ValueError):
        llm.chat("sys", "user")
    assert called == []


@pytest.mark.parametrize(
    "payload,raw_text",
    [
        (None, "not-json{{{"),
        ("scalar", None),
        ([], None),
        ({}, None),
        ({"choices": None}, None),
        ({"choices": []}, None),
        ({"choices": [None]}, None),
        ({"choices": [{}]}, None),
        ({"choices": [{"message": None}]}, None),
        ({"choices": [{"message": {}}]}, None),
        ({"choices": [{"message": {"content": None}}]}, None),
        ({"choices": [{"message": {"content": 123}}]}, None),
    ],
)
def test_chat_raises_value_error_on_malformed_payloads(monkeypatch, payload, raw_text):
    _set_env(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply(payload=payload, raw_text=raw_text))
    with pytest.raises(ValueError):
        llm.chat("sys", "user")


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_chat_raises_on_http_error_status(monkeypatch, status_code):
    _set_env(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply(status_code=status_code))
    with pytest.raises(httpx.HTTPStatusError):
        llm.chat("sys", "user")


def test_choose_returns_none_when_unavailable_or_empty_candidates(monkeypatch):
    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: called.append(1))
    assert llm.choose("1000 L milk", [_candidate("460396")]) is None
    _set_env(monkeypatch, base="  ", key="sk", model="m")
    assert llm.choose("1000 L milk", [_candidate("460396")]) is None
    _set_env(monkeypatch)
    assert llm.choose("1000 L milk", []) is None
    assert called == []


@pytest.mark.parametrize(
    "reply",
    [
        "460396",
        "  460396\n",
        '"460396"',
        "'460396'",
        "`460396`",
        "```\n460396\n```",
        "```text\n460396\n```",
        '{"product_id": "460396"}',
        '{"product_id": 460396}',
        '```json\n{"product_id": "460396"}\n```',
        '{"best_offer": "460396"}',
        '{"product_id": "460396"',
        "Best offer: 460396",
        "I recommend offer (460396).",
        "- 460396",
    ],
)
def test_choose_extracts_single_valid_candidate_id(monkeypatch, reply):
    _set_env(monkeypatch)
    candidates = [_candidate("460396"), _candidate("460397", total=135.0)]
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply(reply))
    assert llm.choose("1000 L lactose-free milk", candidates) == "460396"


def test_choose_preserves_int_product_id_and_hyphenated_sku(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply('```json\n{"product_id": "460396"}\n```'))
    assert llm.choose("milk", [_candidate(460396)]) == 460396

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply("Best offer: prod-1."))
    assert llm.choose("milk", [_candidate("prod-1"), _candidate("prod-2")]) == "prod-1"


@pytest.mark.parametrize(
    "reply,candidates",
    [
        ("Either 460396 or 460397 works well.", [_candidate("460396"), _candidate("460397")]),
        ('{"choices": ["460396", "460397"]}', [_candidate("460396"), _candidate("460397")]),
        ('{"product_id": "460396 or 460397"}', [_candidate("460396"), _candidate("460397")]),
        ('{"product_id": ["460396", "460397"]}', [_candidate("460396"), _candidate("460397")]),
        ("999999", [_candidate("460396"), _candidate("460397")]),
        ("Best offer: 999999", [_candidate("460396"), _candidate("999")]),
        ("Best offer: 460396", [_candidate("46")]),
        ("I cannot select an offer", [_candidate("a"), _candidate("b")]),
        ('{"product_id": "999999", "rejected": "460396"}', [_candidate("460396")]),
        ('{"product_id": null, "note": "460396"}', [_candidate("460396")]),
        ("", [_candidate("460396")]),
        ("   ", [_candidate("460396")]),
    ],
)
def test_choose_returns_none_on_ambiguous_unknown_or_empty(monkeypatch, reply, candidates):
    _set_env(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply(reply))
    assert llm.choose("1000 L milk", candidates) is None


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "connect_error",
        "http_429",
        "http_500",
        "bad_json",
        "empty_choices",
        "none_content",
    ],
)
def test_choose_graceful_fallback_on_transport_and_payload_errors(monkeypatch, failure):
    _set_env(monkeypatch)
    req = httpx.Request("POST", "https://llm.example.test/v1/chat/completions")

    def fake_post(*a, **kw):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=req)
        if failure == "connect_error":
            raise httpx.ConnectError("connection refused", request=req)
        if failure == "http_429":
            return _http_reply(status_code=429)
        if failure == "http_500":
            return _http_reply(status_code=500)
        if failure == "bad_json":
            return _http_reply(raw_text="<html>502 Bad Gateway</html>")
        if failure == "empty_choices":
            return _http_reply(payload={"choices": []})
        return _http_reply(payload={"choices": [{"message": {"content": None}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    cand = _candidate("460396", avail_date=date(2026, 11, 16))
    assert llm.choose("1000 L milk", [cand]) is None


def test_explain_success_and_default_str_serialization(monkeypatch):
    _set_env(monkeypatch)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["user"] = json["messages"][1]["content"]
        return _http_reply("  Ordered 1000 L milk within mandate. Delivery arrives Monday.  ")

    monkeypatch.setattr(httpx, "post", fake_post)
    facts = {
        "deliver_by": date(2026, 11, 16),
        "total": Decimal("1150.50"),
        "tags": {"lactose_free"},
    }
    out = llm.explain(facts)
    assert out == "Ordered 1000 L milk within mandate. Delivery arrives Monday."
    sent = json.loads(captured["user"])
    assert sent["deliver_by"] == "2026-11-16"
    assert sent["total"] == "1150.50"


def test_explain_returns_none_when_unavailable(monkeypatch):
    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: called.append(1))
    assert llm.explain({"request": "milk"}) is None
    _set_env(monkeypatch, base=" ", key="sk", model="m")
    assert llm.explain({"request": "milk"}) is None
    assert called == []


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "connect_error",
        "http_429",
        "http_500",
        "bad_json",
        "empty_choices",
        "none_content",
        "empty_reply",
        "whitespace_reply",
    ],
)
def test_explain_returns_none_on_errors_or_empty_reply(monkeypatch, failure):
    _set_env(monkeypatch)
    req = httpx.Request("POST", "https://llm.example.test/v1/chat/completions")

    def fake_post(*a, **kw):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=req)
        if failure == "connect_error":
            raise httpx.ConnectError("connection refused", request=req)
        if failure == "http_429":
            return _http_reply(status_code=429)
        if failure == "http_500":
            return _http_reply(status_code=500)
        if failure == "bad_json":
            return _http_reply(raw_text="{bad")
        if failure == "empty_choices":
            return _http_reply(payload={"choices": []})
        if failure == "none_content":
            return _http_reply(payload={"choices": [{"message": {"content": None}}]})
        if failure == "empty_reply":
            return _http_reply("")
        return _http_reply("   \n  ")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert llm.explain({"request": "1000 L milk"}) is None


def test_explain_handles_broken_str_object_without_crashing(monkeypatch):
    class BrokenStr:
        def __str__(self):
            raise RuntimeError("boom")

    _set_env(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply("ok"))
    assert llm.explain({"bad": BrokenStr()}) is None


@pytest.mark.parametrize(
    "mode",
    ["no_key", "whitespace_key", "timeout", "connect_error", "http_429", "http_500", "empty_choices", "none_content", "bad_json"],
)
def test_parse_need_falls_back_gracefully_when_llm_unavailable_or_failing(monkeypatch, mode):
    req = httpx.Request("POST", "https://llm.example.test/v1/chat/completions")
    if mode == "whitespace_key":
        _set_env(monkeypatch, key="   ")
    elif mode != "no_key":
        _set_env(monkeypatch)

    def fake_post(*a, **kw):
        if mode in ("no_key", "whitespace_key"):
            raise AssertionError("httpx.post must not be called when LLM key is unset or blank")
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=req)
        if mode == "connect_error":
            raise httpx.ConnectError("refused", request=req)
        if mode == "http_429":
            return _http_reply(status_code=429)
        if mode == "http_500":
            return _http_reply(status_code=500)
        if mode == "empty_choices":
            return _http_reply(payload={"choices": []})
        if mode == "none_content":
            return _http_reply(payload={"choices": [{"message": {"content": None}}]})
        return _http_reply("```json\n{malformed\n```")

    monkeypatch.setattr(httpx, "post", fake_post)
    need = parse_need("25 kg butter by Monday", "hotel-aurora", date(2026, 11, 14))
    assert need.items[0].query == "butter"
    assert need.items[0].quantity == 25.0
    assert need.items[0].unit == "kg"


def test_parse_need_accepts_fenced_json_from_llm_chat(monkeypatch):
    _set_env(monkeypatch)
    fenced = '```json\n{"deliver_by": "2026-11-16", "items": [{"query": "milk", "quantity": 1000, "unit": "L", "constraints": {"lactose_free": true}}]}\n```'
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _http_reply(fenced))
    need = parse_need("1000 L lactose-free milk by Monday", "hotel-aurora", date(2026, 11, 14))
    assert need.deliver_by == date(2026, 11, 16)
    assert need.items[0].query == "milk"
    assert need.items[0].quantity == 1000.0
    assert need.items[0].constraints.lactose_free is True


def test_direct_chat_monkeypatch_compatibility_and_buyer_fallback(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", lambda s, u: "")
    assert llm.choose("milk", [_candidate("460396")]) is None
    assert llm.explain({"request": "milk"}) is None

    offers = [
        {**_candidate("460396", total=50.0), "units_needed": 10, "unit_price_net": 5.0, "sales_block": False, "currency": "EUR"},
        {**_candidate("460397", total=40.0), "units_needed": 10, "unit_price_net": 4.0, "sales_block": False, "currency": "EUR"},
    ]
    offers[0]["product"]["allergens"] = []
    offers[1]["product"]["allergens"] = []
    ni = {"query": "milk", "constraints": {"exclude_allergens": []}}

    monkeypatch.setattr(llm, "chat", lambda s, u: '```json\n{"product_id": "460396"}\n```')
    first = llm.choose("10 L milk", offers)
    chosen, left = buyer.pick(ni, offers, "2026-11-17", first)
    assert first == "460396"
    assert chosen[0]["product_id"] == "460396"
    assert left == 0

    def boom(s, u):
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(llm, "chat", boom)
    first_err = llm.choose("10 L milk", offers)
    chosen_fallback, left_fallback = buyer.pick(ni, offers, "2026-11-17", first_err)
    assert first_err is None
    assert chosen_fallback[0]["product_id"] == "460397"
    assert left_fallback == 0

