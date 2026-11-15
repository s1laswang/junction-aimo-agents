"""Bad-input hardening tests for voice helpers, plan_need, and every /v1 HTTP route."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aimo import config
from aimo.backend import catalog_store, client as bc, orders as store
from aimo.catalog import plan as catalog_plan
from aimo.catalog import textsearch
from aimo.gateway import fallback_catalog, human_auth, service
from aimo.models import Need
from aimo.voice import core as voice_core
from test_gateway import FixtureClient, H

H_AURORA = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
H_SELLER = {"X-Agent-Id": "valio-aimo/seller-1"}
ANNA = "anna@hotel-aurora.example"
PIN = "test-zero-500-pin"
DELIVERY = "2026-11-16"


def _synth_catalog_items() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    base_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]

    def _item(
        pid: str,
        sales: str,
        name_en: str,
        brand: str,
        cat: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": cat,
            "vendorName": brand,
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": u, "sizeInBaseUnits": s} for u, s in units],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "milk, vitamin D"}],
                "marketingTexts": [{"language": "en", "value": name_en}],
            },
        }

    return [
        _item("463038", "KI", "Valio Eila semi skimmed milk drink 10 l novobox lactose free", "Valio", "10400", [("KI", 2), ("ST", 1)], [{"synkkaPackagingType": "CASE"}]),
        _item("464306", "ST", "Valio Eila semi skimmed milk drink 1 l lactose free", "Valio", "10400", [("ST", 1)], base_1l),
        _item("469363", "ST", "Arla Semi skimmed milkdrink 1 L UHT lactose free", "Arla", "10400", [("ST", 1)], base_1l),
        _item("900001", "ST", "Valio lactose free butter 1 kg", "Valio", "10500", [("ST", 1)], base_1kg),
        _item("900002", "ST", "Myllyn Paras oat flakes 1 kg", "Myllyn Paras", "21370", [("ST", 1)], base_1kg),
    ]


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory: pytest.TempPathFactory):
    if config.PRODUCT_JSON.exists():
        return None
    d = tmp_path_factory.mktemp("zero500cat")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_catalog_items()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def _no_external_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


@pytest.fixture
def gateway_env(tmp_path, monkeypatch: pytest.MonkeyPatch, synth_catalog_dir):
    import aimo.catalog as cat
    from aimo.gateway.app import create_app

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_APPROVER_PIN", PIN)
    if synth_catalog_dir is not None:
        pjson = synth_catalog_dir / "valio_aimo_junction26_product_data.json"
        cdb = synth_catalog_dir / "catalog.sqlite"
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synth_catalog_dir / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", synth_catalog_dir / "missing_co2.csv")
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
    human_auth.reset_for_tests()
    bc.set_client(FixtureClient())
    monkeypatch.setattr(
        service,
        "_catalog",
        lambda: SimpleNamespace(
            plan_need=service._plan_deterministic,
            search=lambda q, **kw: textsearch.search(q, semantic=False, **kw),
            get_product=cat.get_product,
        ),
    )
    app = create_app(with_mcp=False)
    client = TestClient(app, raise_server_exceptions=False)
    sess_resp = client.post("/v1/auth/approver-session", headers={"X-Approver-Pin": PIN}, json={"approver": ANNA})
    assert sess_resp.status_code == 200, sess_resp.text
    sess = sess_resp.json()["session"]
    yield client, sess
    human_auth.reset_for_tests()
    bc.set_client(None)
    if synth_catalog_dir is not None:
        monkeypatch.setattr(fallback_catalog, "_index", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def _assert_clean_error(resp, *, max_status: int = 499) -> dict[str, Any]:
    assert resp.status_code <= max_status, (resp.status_code, resp.text[:300])
    assert resp.status_code >= 400, (resp.status_code, resp.text[:300])
    body = resp.json()
    assert isinstance(body, dict), body
    assert isinstance(body.get("error"), str) and body["error"], body
    assert isinstance(body.get("message"), str) and body["message"], body
    return body


def _assert_no_500_json_if_err(resp, label: str = "") -> None:
    assert resp.status_code < 500, f"{label}: got {resp.status_code} {resp.text[:300]}"
    if resp.status_code >= 400:
        body = resp.json()
        assert isinstance(body, dict), (label, body)
        assert isinstance(body.get("error"), str) and body["error"], (label, body)
        assert isinstance(body.get("message"), str) and body["message"], (label, body)


def test_voice_core_num_and_eur_guard_bad_inputs():
    bad_values: list[Any] = [
        True,
        False,
        None,
        "100",
        "",
        [],
        {},
        math.nan,
        math.inf,
        -math.inf,
        10**400,
        -(10**400),
    ]
    for bad in bad_values:
        assert voice_core._num(bad) == "0"
        assert voice_core._eur(bad) == "0 euros"

    assert voice_core._eur(1e308) == "0 euros"
    assert voice_core._eur(-1e308) == "0 euros"
    assert isinstance(voice_core._num(1e308), str)
    assert voice_core._num(0) == "0"
    assert voice_core._num(-0.0) == "0"
    assert voice_core._eur(0) == "0 euros"
    assert voice_core._eur(-0.0) == "0 euros"
    assert voice_core._num(1000) == "1,000"
    assert voice_core._num(2.5) == "2.50"
    assert voice_core._eur(1010.5) == "1,010 euros 50 cents"
    assert voice_core._eur(-12.34) == "-12 euros 34 cents"


def test_voice_core_approval_prompt_handles_malformed_dicts_lists_and_entries():
    for bad_order in (None, [], "bad", 123, True):
        for bad_quote in (None, [], "bad", 456, False):
            out = voice_core.approval_prompt(bad_order, bad_quote, customer_name=123)
            assert "Approval request for the customer: an order." in out
            assert "Total 0 euros before VAT" in out
            assert "confirm on screen" in out
            assert "\x00" not in out

    weird_order = {
        "customer_id": "hotel\x00-aurora",
        "decision": {
            "approver_role": ["not-a-role"],
            "hits": [
                None,
                "str-hit",
                123,
                {"outcome": "needs_approval", "message": None},
                {"outcome": "needs_approval", "message": 999},
                {"outcome": "needs_approval", "message": "   "},
                {"outcome": "needs_approval", "message": "Over mandate\x00."},
            ],
        },
    }
    weird_quote = {
        "lines": [
            None,
            "bad-line",
            42,
            {"qty_sales_units": True, "sales_unit": 99, "product_name": None, "product_id": False},
            {"qty_sales_units": math.nan, "sales_unit": "ST\x00", "product_name": "Milk\x00 1 L"},
            {"qty_sales_units": 10**400, "sales_unit": "", "product_id": 464306},
            {"qty_sales_units": 5, "sales_unit": "KI", "product_name": "Extra line 1"},
            {"qty_sales_units": 2, "sales_unit": "KI", "product_name": "Extra line 2"},
        ],
        "total_net": 1e308,
        "delivery_date": ["bad-date"],
    }
    txt = voice_core.approval_prompt(weird_order, weird_quote, customer_name="Hotel\x00 Aurora")
    assert "Hotel Aurora" in txt
    assert "Over mandate." in txt
    assert "and 2 more lines" in txt
    assert "Total 0 euros before VAT" in txt
    assert "\x00" not in txt

    non_list_structures = voice_core.approval_prompt(
        {"customer_id": True, "decision": "not-a-dict"},
        {"lines": "not-a-list", "total_net": math.inf, "delivery_date": None},
    )
    assert "Approval request for the customer: an order." in non_list_structures


@pytest.mark.parametrize(
    "bad_transcript",
    [None, True, False, 123, 0, -1.5, ["yes"], ["approve"], {"transcript": "yes"}, b"approve"],
)
def test_voice_core_rule_and_classify_intent_reject_non_str(bad_transcript):
    r = voice_core.rule_intent(bad_transcript)
    assert r["intent"] == "unclear"
    assert r["confidence"] == 0.0

    c = voice_core.classify_intent(bad_transcript)
    assert c["intent"] == "unclear"
    assert c["confidence"] == 0.0
    assert c["transcript"] == ""


def test_voice_core_null_bytes_and_gemini_intent_hardening(monkeypatch: pytest.MonkeyPatch):
    assert voice_core.rule_intent("\x00")["intent"] == "unclear"
    assert voice_core.rule_intent("\x00")["confidence"] == 0.0
    c_null = voice_core.classify_intent(" \x00\t ")
    assert c_null["intent"] == "unclear" and c_null["confidence"] == 0.0 and c_null["transcript"] == ""

    c_yes = voice_core.classify_intent("yes\x00 approve")
    assert c_yes["intent"] == "approve" and "\x00" not in c_yes["transcript"]

    c_neg = voice_core.classify_intent("don't\x00 approve")
    assert c_neg["intent"] == "unclear" and "\x00" not in c_neg["transcript"]

    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    import aimo.gemini as gemini_mod

    for non_dict_json in ("[1, 2]", '"approve"', "123", "true", "null", "{bad-json"):
        monkeypatch.setattr(gemini_mod, "generate", lambda *a, _raw=non_dict_json, **kw: SimpleNamespace(text=_raw))
        assert voice_core.gemini_intent("yes approve") is None
        fallback = voice_core.classify_intent("yes approve")
        assert fallback["intent"] == "approve" and fallback["source"] == "rules"

    for bad_conf in (math.nan, math.inf, -math.inf, True, "bad", 99.0, -5.0):
        def _fake_gen(*a, _c=bad_conf, **kw):
            payload = {"intent": "reject", "confidence": _c, "note": "hold\x00it"}
            return SimpleNamespace(text=json.dumps(payload, allow_nan=True))

        monkeypatch.setattr(gemini_mod, "generate", _fake_gen)
        g = voice_core.gemini_intent("reject this")
        assert g is not None
        assert g["intent"] == "reject"
        assert math.isfinite(g["confidence"]) and 0.0 <= g["confidence"] <= 1.0
        assert g["note"] == "holdit"
        json.dumps(g, allow_nan=False)


def test_voice_core_tts_and_stt_validation_and_upstream_json_guards(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(voice_core.VoiceError) as no_key_tts:
        voice_core.tts("")
    assert no_key_tts.value.status == 503 and no_key_tts.value.error == "voice_unavailable"

    with pytest.raises(voice_core.VoiceError) as no_key_stt:
        voice_core.stt(b"")
    assert no_key_stt.value.status == 503 and no_key_stt.value.error == "voice_unavailable"

    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-eleven-key")
    called: list[str] = []

    class _NeverClient:
        def __enter__(self):
            called.append("entered")
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(voice_core, "_client", lambda timeout=20.0: _NeverClient())

    for bad_text in (None, 123, True, ["hello"], {}, "", "   ", "\x00 \t\n"):
        with pytest.raises(voice_core.VoiceError) as exc_tts:
            voice_core.tts(bad_text)
        assert exc_tts.value.status == 422 and exc_tts.value.error == "text_required"
    assert called == []

    for bad_audio in (None, "", 123, b""):
        with pytest.raises(voice_core.VoiceError) as exc_stt:
            voice_core.stt(bad_audio)
        assert exc_stt.value.status == 422 and exc_stt.value.error == "audio_required"

    with pytest.raises(voice_core.VoiceError) as exc_big:
        voice_core.stt(b"\x00" * (voice_core.MAX_AUDIO_BYTES + 1))
    assert exc_big.value.status == 413 and exc_big.value.error == "audio_too_large"
    assert called == []

    class _BadJsonResp:
        status_code = 200
        content = b"not-json"

        def json(self):
            raise ValueError("Expecting value")

    class _MockClient:
        def __init__(self, resp):
            self._resp = resp

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return self._resp

    monkeypatch.setattr(voice_core, "_client", lambda timeout=20.0: _MockClient(_BadJsonResp()))
    with pytest.raises(voice_core.VoiceError) as exc_bad_json:
        voice_core.stt(b"OggS")
    assert exc_bad_json.value.status == 502 and exc_bad_json.value.error == "voice_upstream"

    for non_dict in (None, ["Approve"], "Approve", 123, True):
        resp_obj = SimpleNamespace(status_code=200, content=b"x", json=lambda _v=non_dict: _v)
        monkeypatch.setattr(voice_core, "_client", lambda timeout=20.0, _r=resp_obj: _MockClient(_r))
        with pytest.raises(voice_core.VoiceError) as exc_shape:
            voice_core.stt(b"OggS")
        assert exc_shape.value.status == 502 and exc_shape.value.error == "voice_upstream"

    weird_fields = SimpleNamespace(
        status_code=200,
        content=b"{}",
        json=lambda: {"text": 12345, "language_code": ["fi"]},
    )
    monkeypatch.setattr(voice_core, "_client", lambda timeout=20.0: _MockClient(weird_fields))
    res = voice_core.stt(b"OggS", mime=None, language="fi\x00")
    assert res["transcript"] == "" and res["language"] is None


def test_voice_routes_bad_inputs_with_human_session(gateway_env):
    client, sess = gateway_env
    sh = {"X-Approver-Session": sess}

    q = client.post(
        "/v1/quotes",
        headers=H_AURORA,
        json={"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]},
    ).json()
    o = client.post("/v1/orders", headers=H_AURORA, json={"quote_id": q["quote_id"], "quote_hash": q["quote_hash"]}).json()
    oid = o["order_id"]

    bad_order_ids: list[Any] = [123, ["O-1"], True, False, {}, None, "", "   ", "O-1\x00", "O-1\n", "x" * 129]
    for bad_oid in bad_order_ids:
        r = client.post("/v1/voice/prompt", headers=sh, json={"order_id": bad_oid})
        err = _assert_clean_error(r)
        assert r.status_code == 422 and err["error"] == "invalid_request"

    r_empty_prompt = client.post("/v1/voice/prompt", headers=sh, json={})
    assert r_empty_prompt.status_code == 422 and r_empty_prompt.json()["error"] == "invalid_request"

    for bad_audio_flag in ("false", 123, [], {}):
        r_flag = client.post("/v1/voice/prompt", headers=sh, json={"order_id": oid, "audio": bad_audio_flag})
        assert r_flag.status_code == 422 and r_flag.json()["error"] == "invalid_request"

    for bad_t in (["yes"], ["approve"], True, False, 123, {}, None):
        r_int = client.post("/v1/voice/intent", headers=sh, json={"transcript": bad_t})
        err = _assert_clean_error(r_int)
        assert r_int.status_code == 422
        assert err["error"] == "invalid_request"
        assert "transcript must be a string" in err["message"]
        assert "intent" not in err

    assert client.post("/v1/voice/intent", headers=sh, json={}).status_code == 422
    assert client.post("/v1/voice/intent", headers=sh, json={"transcript": "yes\x00approve"}).status_code == 422
    assert client.post("/v1/voice/intent", headers=sh, json={"transcript": "a" * 4001}).status_code == 422

    for bad_opt_oid in (123, ["O-1"], True, False, {}, "O-1\x00", "O-1\r", "x" * 129):
        r_opt = client.post("/v1/voice/intent", headers=sh, json={"transcript": "yes", "order_id": bad_opt_oid})
        err = _assert_clean_error(r_opt)
        assert r_opt.status_code == 422 and err["error"] == "invalid_request"

    r_blank_ok = client.post("/v1/voice/intent", headers=sh, json={"transcript": "", "order_id": ""})
    assert r_blank_ok.status_code == 200
    assert r_blank_ok.json()["intent"] == "unclear" and r_blank_ok.json()["order_id"] is None

    r_no_boundary = client.post(
        "/v1/voice/transcribe",
        headers={**sh, "Content-Type": "multipart/form-data"},
        content=b"--broken",
    )
    err_nb = _assert_clean_error(r_no_boundary)
    assert r_no_boundary.status_code == 400 and err_nb["error"] == "invalid_multipart"

    r_corrupt_mp = client.post(
        "/v1/voice/transcribe",
        headers={**sh, "Content-Type": "multipart/form-data; boundary=aimobound"},
        content=b"--aimobound\r\nContent-Disposition: form-data\r\n\r\nmissing-name\r\n--aimobound--\r\n",
    )
    err_cmp = _assert_clean_error(r_corrupt_mp)
    assert r_corrupt_mp.status_code == 400 and err_cmp["error"] == "invalid_multipart"

    r_str_field = client.post("/v1/voice/transcribe", headers=sh, data={"audio": "not-binary"}, files={"dummy": ("d.txt", b"x", "text/plain")})
    assert r_str_field.status_code == 422 and r_str_field.json()["error"] == "audio_required"

    r_missing_audio = client.post("/v1/voice/transcribe", headers=sh, files={"other": ("a.webm", b"OggS", "audio/webm")})
    assert r_missing_audio.status_code == 422 and r_missing_audio.json()["error"] == "audio_required"

    r_empty_raw = client.post("/v1/voice/transcribe", headers={**sh, "Content-Type": "audio/webm"}, content=b"")
    assert r_empty_raw.status_code == 422 and r_empty_raw.json()["error"] == "audio_required"

    r_empty_file = client.post("/v1/voice/transcribe", headers=sh, files={"audio": ("a.webm", b"", "audio/webm")})
    assert r_empty_file.status_code == 422 and r_empty_file.json()["error"] == "audio_required"

    oversized = b"\x00" * (voice_core.MAX_AUDIO_BYTES + 1)
    r_big_raw = client.post("/v1/voice/transcribe", headers={**sh, "Content-Type": "audio/webm"}, content=oversized)
    assert r_big_raw.status_code == 413 and r_big_raw.json()["error"] == "audio_too_large"

    r_big_mp = client.post("/v1/voice/transcribe", headers=sh, files={"audio": ("a.webm", oversized, "audio/webm")})
    assert r_big_mp.status_code == 413 and r_big_mp.json()["error"] == "audio_too_large"


def test_plan_need_and_http_plan_reject_bad_inputs_before_coverage_division(gateway_env):
    client, _ = gateway_env
    base_need = {
        "customer_id": "hotel-aurora",
        "product_query": "lactose-free milk",
        "required_claims": ["FREE_FROM_LACTOSE"],
        "unit": "L",
        "delivery_by": DELIVERY,
        "quantity": 10.0,
    }

    bad_quantities: list[Any] = [
        0,
        0.0,
        -0.0,
        -1,
        -0.0001,
        -1e9,
        True,
        False,
        math.nan,
        math.inf,
        -math.inf,
        1_000_001,
        10**20,
        10**400,
        None,
        "abc",
        [10],
        {},
    ]
    for bad_q in bad_quantities:
        constructed = Need.model_construct(
            raw_text="",
            customer_id="hotel-aurora",
            product_query="milk",
            required_claims=[],
            excluded_allergens=[],
            quantity=bad_q,
            unit="L",
            delivery_by=base_need["delivery_by"],
            preferred_product_ids=[],
            preferences={},
        )
        with pytest.raises(service.ServiceError) as exc_svc:
            service.plan_need(constructed, identity=service.Identity(agent_id="buyer-1", customer_id="hotel-aurora"))
        assert exc_svc.value.status == 422

        with pytest.raises(ValueError):
            catalog_plan.fill(constructed, [])

    http_bad_cases: list[dict[str, Any]] = [
        {**base_need, "quantity": 0},
        {**base_need, "quantity": 0.0},
        {**base_need, "quantity": -0.0},
        {**base_need, "quantity": -1},
        {**base_need, "quantity": -0.001},
        {**base_need, "quantity": True},
        {**base_need, "quantity": False},
        {**base_need, "quantity": 1_000_001},
        {**base_need, "quantity": 1e308},
        {**base_need, "quantity": "not-a-number"},
        {**base_need, "quantity": None},
        {**base_need, "unit": ""},
        {**base_need, "unit": "GALLONS"},
        {**base_need, "unit": 123},
        {**base_need, "delivery_by": ""},
        {**base_need, "delivery_by": "2026-13-45"},
        {**base_need, "delivery_by": "not-a-date"},
        {**base_need, "delivery_by": 12345},
        {**base_need, "product_query": ""},
        {**base_need, "product_query": "   "},
        {**base_need, "product_query": "x" * 501},
        {**base_need, "product_query": 123},
        {**base_need, "product_query": ["milk"]},
        {**base_need, "raw_text": "x" * 4001},
        {**base_need, "preferred_product_ids": "464306"},
        {**base_need, "excluded_allergens": "milk"},
    ]
    for payload in http_bad_cases:
        r = client.post("/v1/plan", headers=H_AURORA, json=payload)
        _assert_clean_error(r)
        assert r.status_code == 422, (payload, r.status_code, r.text)
        assert "ZeroDivision" not in r.text

    for raw_non_std in (
        b'{"customer_id":"hotel-aurora","product_query":"milk","quantity":-0.0,"unit":"L","delivery_by":"2026-11-16"}',
        b'{"customer_id":"hotel-aurora","product_query":"milk","quantity":NaN,"unit":"L","delivery_by":"2026-11-16"}',
        b'{"customer_id":"hotel-aurora","product_query":"milk","quantity":Infinity,"unit":"L","delivery_by":"2026-11-16"}',
        b'{"customer_id":"hotel-aurora","product_query":"milk","quantity":-Infinity,"unit":"L","delivery_by":"2026-11-16"}',
        b'{"customer_id":"hotel-aurora","product_query":"milk","quantity":1e309,"unit":"L","delivery_by":"2026-11-16"}',
    ):
        r_raw = client.post("/v1/plan", headers={**H_AURORA, "Content-Type": "application/json"}, content=raw_non_std)
        _assert_clean_error(r_raw)
        assert r_raw.status_code in (400, 422)


def test_http_zero_500_sweep_across_all_v1_route_families(gateway_env):
    client, sess = gateway_env
    sh = {"X-Approver-Session": sess}

    raw_malformed_payloads = (b"", b"{", b"null", b"[]", b'"str"', b"42", b"\xff\xfe")
    post_targets = (
        ("/v1/plan", H_AURORA),
        ("/v1/offers", H_AURORA),
        ("/v1/quotes", H_AURORA),
        ("/v1/orders", H_AURORA),
        ("/v1/approvals/O-missing/decision", sh),
        ("/v1/auth/approver-session", {"X-Approver-Pin": PIN}),
        ("/v1/recipes/plan", H_AURORA),
        ("/v1/proposals", H_SELLER),
        ("/v1/proposals/P-missing/accept", H_AURORA),
        ("/v1/proposals/P-missing/decline", H_AURORA),
        ("/v1/voice/prompt", sh),
        ("/v1/voice/intent", sh),
    )
    for path, hdrs in post_targets:
        for raw in raw_malformed_payloads:
            r = client.post(path, headers={**hdrs, "Content-Type": "application/json"}, content=raw)
            _assert_no_500_json_if_err(r, f"raw POST {path} {raw!r}")

    sweep_requests: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", "/v1/products/search", {"headers": H_AURORA}),
        ("GET", "/v1/products/search", {"headers": H_AURORA, "params": {"q": ""}}),
        ("GET", "/v1/products/search", {"headers": H_AURORA, "params": {"q": "   "}}),
        ("GET", "/v1/products/search", {"headers": H_AURORA, "params": {"q": "x" * 501}}),
        ("GET", "/v1/products/search", {"headers": H_AURORA, "params": {"q": "milk", "limit": 0}}),
        ("GET", "/v1/products/search", {"headers": H_AURORA, "params": {"q": "milk", "limit": -5}}),
        ("GET", "/v1/products/search", {"headers": H_AURORA, "params": {"q": "milk", "limit": 101}}),
        ("GET", "/v1/products/search", {"headers": H_AURORA, "params": {"q": "milk", "limit": "abc"}}),
        ("GET", "/v1/products/search", {"headers": {"X-Customer-Id": "unknown-corp"}, "params": {"q": "milk"}}),
        ("GET", "/v1/products/does-not-exist", {"headers": H_AURORA}),
        ("GET", "/v1/products/..%2Fetc", {"headers": H_AURORA}),
        ("GET", "/v1/products/464306", {"headers": {"X-Customer-Id": "unknown-corp"}}),
        ("POST", "/v1/offers", {"headers": H_AURORA, "json": {"product_ids": [], "requested_date": DELIVERY}}),
        ("POST", "/v1/offers", {"headers": H_AURORA, "json": {"product_ids": [""], "requested_date": DELIVERY}}),
        ("POST", "/v1/offers", {"headers": H_AURORA, "json": {"product_ids": ["bad id"], "requested_date": DELIVERY}}),
        ("POST", "/v1/offers", {"headers": H_AURORA, "json": {"product_ids": ["x" * 65], "requested_date": DELIVERY}}),
        ("POST", "/v1/offers", {"headers": H_AURORA, "json": {"product_ids": [True], "requested_date": DELIVERY}}),
        ("POST", "/v1/offers", {"headers": H_AURORA, "json": {"product_ids": ["464306"], "requested_date": "bad-date"}}),
        ("POST", "/v1/offers", {"headers": H_AURORA, "json": {"product_ids": ["464306"], "requested_date": DELIVERY, "customer_id": "rogue"}}),
        ("POST", "/v1/quotes", {"headers": {}, "json": {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1}]}}),
        ("POST", "/v1/quotes", {"headers": H_AURORA, "json": {"delivery_date": DELIVERY, "lines": []}}),
        ("POST", "/v1/quotes", {"headers": H_AURORA, "json": {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 0}]}}),
        ("POST", "/v1/quotes", {"headers": H_AURORA, "json": {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": -5}]}}),
        ("POST", "/v1/quotes", {"headers": H_AURORA, "json": {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 2.5}]}}),
        ("POST", "/v1/quotes", {"headers": H_AURORA, "json": {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1_000_001}]}}),
        ("POST", "/v1/quotes", {"headers": H_AURORA, "json": {"delivery_date": DELIVERY, "lines": [{"product_id": "464306", "qty_sales_units": 1, "tier": "bogus"}]}}),
        ("POST", "/v1/quotes", {"headers": H_AURORA, "json": {"delivery_date": "2026-99-99", "lines": [{"product_id": "464306", "qty_sales_units": 1}]}}),
        ("GET", "/v1/quotes/Q-does-not-exist", {"headers": H_AURORA}),
        ("GET", f"/v1/quotes/{'x' * 300}", {"headers": H_AURORA}),
        ("POST", "/v1/orders", {"headers": {}, "json": {"quote_id": "Q-1", "quote_hash": "0" * 64}}),
        ("POST", "/v1/orders", {"headers": H_AURORA, "json": {"quote_id": "   ", "quote_hash": "0" * 64}}),
        ("POST", "/v1/orders", {"headers": H_AURORA, "json": {"quote_id": "Q-missing", "quote_hash": "0" * 64}}),
        ("POST", "/v1/orders", {"headers": H_AURORA, "json": {"quote_id": 123, "quote_hash": True}}),
        ("GET", "/v1/orders/O-does-not-exist", {"headers": H_AURORA}),
        ("GET", "/v1/orders/O-does-not-exist/peppol-order-response.xml", {"headers": H_AURORA}),
        ("GET", "/v1/approvals", {"headers": {}}),
        ("GET", "/v1/approvals", {"headers": {"X-Customer-Id": "rogue-corp"}}),
        ("GET", "/v1/approvals", {"headers": H_AURORA, "params": {"customer_id": "school-tampere"}}),
        ("GET", "/v1/approvals", {"headers": H_AURORA, "params": {"status": "weird-status"}}),
        ("POST", "/v1/approvals/O-missing/decision", {"headers": H_AURORA, "json": {"decision": "approve", "quote_hash": "0" * 64}}),
        ("POST", "/v1/approvals/O-missing/decision", {"headers": sh, "json": {"decision": "maybe", "quote_hash": "0" * 64}}),
        ("POST", "/v1/approvals/O-missing/decision", {"headers": sh, "json": {"decision": "approve", "quote_hash": "0" * 64}}),
        ("POST", "/v1/auth/approver-session", {"headers": H_AURORA, "json": {"approver": ANNA}}),
        ("POST", "/v1/auth/approver-session", {"headers": {"X-Approver-Pin": PIN}, "json": {"approver": "nobody@nowhere.invalid"}}),
        ("GET", "/v1/customers", {"headers": {"X-Approver-Session": "invalid.token.sig"}}),
        ("GET", "/v1/customers/unknown-corp/learned", {"headers": {"X-Customer-Id": "unknown-corp"}}),
        ("GET", "/v1/customers/school-tampere/learned", {"headers": H_AURORA}),
        ("GET", "/v1/audit", {"params": {"subject_id": "missing-subject"}}),
        ("GET", "/v1/audit", {"params": {"subject_id": "x" * 1000}}),
        ("GET", "/v1/audit/verify", {}),
        ("GET", "/v1/audit/report/O-missing", {}),
        ("GET", "/v1/recipes", {}),
        ("POST", "/v1/recipes/plan", {"headers": H_AURORA, "json": {"recipe_id": "unknown-dish", "portions": 10}}),
        ("POST", "/v1/recipes/plan", {"headers": H_AURORA, "json": {"recipe_id": "lasagne", "portions": 0}}),
        ("POST", "/v1/recipes/plan", {"headers": H_AURORA, "json": {"recipe_id": "lasagne", "portions": -10}}),
        ("POST", "/v1/recipes/plan", {"headers": H_AURORA, "json": {"recipe_id": "lasagne", "portions": 100_001}}),
        ("POST", "/v1/recipes/plan", {"headers": H_AURORA, "json": {"text": "", "portions": 10}}),
        ("POST", "/v1/recipes/plan", {"headers": H_AURORA, "json": {"recipe_id": "lasagne", "portions": 10, "delivery_by": "bad-date"}}),
        ("POST", "/v1/recipes/plan", {"headers": {"X-Customer-Id": "rogue-corp"}, "json": {"recipe_id": "lasagne", "portions": 10}}),
        ("POST", "/v1/proposals", {"headers": {}, "json": {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5}}),
        ("POST", "/v1/proposals", {"headers": H_AURORA, "json": {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5}}),
        ("POST", "/v1/proposals", {"headers": H_SELLER, "json": {"customer_id": "rogue-corp", "product_id": "464306", "qty_sales_units": 5}}),
        ("POST", "/v1/proposals", {"headers": H_SELLER, "json": {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 0}}),
        ("POST", "/v1/proposals", {"headers": H_SELLER, "json": {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": -3}}),
        ("POST", "/v1/proposals", {"headers": H_SELLER, "json": {"customer_id": "hotel-aurora", "product_id": "unknown-999", "qty_sales_units": 5}}),
        ("GET", "/v1/proposals", {"params": {"customer_id": "rogue-corp", "status": "weird"}}),
        ("GET", "/v1/proposals/P-does-not-exist", {}),
        ("POST", "/v1/proposals/P-does-not-exist/accept", {"headers": {}}),
        ("POST", "/v1/proposals/P-does-not-exist/accept", {"headers": H_SELLER}),
        ("POST", "/v1/proposals/P-does-not-exist/accept", {"headers": H_AURORA}),
        ("POST", "/v1/proposals/P-does-not-exist/decline", {"headers": {}}),
        ("POST", "/v1/proposals/P-does-not-exist/decline", {"headers": H_SELLER}),
        ("POST", "/v1/proposals/P-does-not-exist/decline", {"headers": H_AURORA}),
        ("GET", "/v1/seller/unknown", {}),
        ("POST", "/v1/seller/propose", {"headers": H_SELLER, "json": {"customer_id": "hotel-aurora"}}),
        ("GET", "/v1/drift/unknown-corp", {}),
        ("GET", "/v1/drift/hotel-aurora", {"params": {"rebaseline": "not-a-bool"}}),
        ("GET", "/v1/drift/hotel-aurora", {"params": {"rebaseline": "true"}}),
        ("GET", "/v1/drift/school-tampere", {"headers": H_AURORA, "params": {"rebaseline": "true"}}),
        ("GET", "/v1/voice/status", {}),
        ("POST", "/v1/voice/prompt", {"headers": {}, "json": {"order_id": "O-1"}}),
        ("POST", "/v1/voice/prompt", {"headers": H_AURORA, "json": {"order_id": "O-1"}}),
        ("POST", "/v1/voice/prompt", {"headers": sh, "json": {"order_id": "O-does-not-exist"}}),
        ("POST", "/v1/voice/intent", {"headers": {}, "json": {"transcript": "yes"}}),
        ("POST", "/v1/voice/intent", {"headers": H_AURORA, "json": {"transcript": "yes"}}),
        ("POST", "/v1/voice/intent", {"headers": sh, "json": {"transcript": ["yes"]}}),
        ("POST", "/v1/voice/transcribe", {"headers": {}, "content": b"OggS"}),
        ("POST", "/v1/voice/transcribe", {"headers": H_AURORA, "content": b"OggS"}),
    ]
    for method, url, kw in sweep_requests:
        r = client.request(method, url, **kw)
        _assert_no_500_json_if_err(r, f"{method} {url} {kw}")
