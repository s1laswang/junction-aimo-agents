from __future__ import annotations

import io
import json
import re
import tokenize
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.genai import types as genai_types

from aimo import config, gemini
from aimo.buyer import eval_gold
from aimo.buyer.eval_gold import (
    APPROVERS,
    Approver,
    InprocApprover,
    LiveApprover,
    _extract_requote_target,
    _now,
    _ranges,
    gold_answer,
    load_gold,
    score,
    summarize,
    write_report,
)
from aimo.buyer.mock_model import MOCK_MODEL_NAME, mock_wanted
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import PROFILES, get_profile, mandate_for
from aimo.buyer.vision import (
    MAX_IMAGE_BYTES,
    SUPPORTED_MIMES,
    ImageOrder,
    extract_order_from_image,
    fallback_image_order,
)
from aimo.gateway import human_auth, service
from aimo.models import AgentStep, AllergenStatus, Mandate

ROOT = Path(__file__).resolve().parents[1]
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
PNG_VALID = (ROOT / "tests" / "fixtures" / "images" / "order_fi_milk_butter.png").read_bytes()

HYGIENE_FILES = (
    ROOT / "aimo" / "buyer" / "eval_gold.py",
    ROOT / "aimo" / "buyer" / "vision.py",
    ROOT / "aimo" / "buyer" / "profiles.py",
    ROOT / "aimo" / "buyer" / "costs.py",
    ROOT / "aimo" / "buyer" / "prompts.py",
    ROOT / "aimo" / "buyer" / "tool_schemas.py",
    Path(__file__).resolve(),
)


def _txt(text: str) -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)])
            )
        ]
    )


def _set_gemini_keys(monkeypatch: pytest.MonkeyPatch, gemini_key: str, google_key: str = "") -> None:
    monkeypatch.setattr(config, "GEMINI_API_KEY", gemini_key or google_key)
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", google_key)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("GOOGLE_API_KEY", google_key)


@pytest.mark.parametrize("target", HYGIENE_FILES, ids=lambda p: p.name)
def test_zero_comments_and_zero_em_en_dashes(target: Path) -> None:
    src = target.read_text(encoding="utf-8")
    comments = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == [], f"{target.name} has {len(comments)} comment token(s): {comments[:3]}"
    assert "\u2014" not in src, f"{target.name} contains em dash"
    assert "\u2013" not in src, f"{target.name} contains en dash"


def test_this_test_file_has_no_hash_char() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    assert "\x23" not in src


def test_load_gold_validates_jsonl_and_assigns_defaults(tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl"
    good.write_text(
        '\n{"request": "10 kg butter", "customer_id": "hotel-aurora", "gold": {}}\n\n'
        '{"id": "custom-2", "request": "20 L milk", "customer_id": "hotel-aurora", "gold": {}}\n',
        encoding="utf-8",
    )
    rows = load_gold([str(good)])
    assert len(rows) == 2
    assert rows[0]["id"] == "good:1"
    assert rows[0]["_file"] == str(good)
    assert rows[1]["id"] == "custom-2"
    assert load_gold([]) == []

    bad_syntax = tmp_path / "bad_syntax.jsonl"
    bad_syntax.write_text("{not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad_syntax.jsonl:1"):
        load_gold([str(bad_syntax)])

    bad_type = tmp_path / "bad_type.jsonl"
    bad_type.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_gold([str(bad_type)])


def test_ranges_and_score_resilience_on_malformed_inputs() -> None:
    assert _ranges(None, 3) == [None, None, None]
    assert _ranges([], 2) == [None, None]
    assert _ranges([10, 20], 0) == []
    assert _ranges("not-a-range", 2) == [None, None]
    assert _ranges([True, False], 2) == [None, None]
    assert _ranges([10], 2) == [None, None]
    assert _ranges(["x", "y"], 2) == [None, None]
    assert _ranges([float("nan"), 20], 1) == [None]
    assert _ranges([[10, 20], [5], [True, 9], [30, 40]], 3) == [[10.0, 20.0], None, None]

    s_empty = score(None, None, None)
    assert s_empty["zero_correction"] is True
    assert s_empty["allergen_safe"] is True
    assert s_empty["cart"] == []

    step_obj = AgentStep(
        run_id="run-1",
        seq=1,
        ts=SAT,
        kind="decision",
        title="Policy check",
        data={"decision": {"outcome": "needs_approval"}},
    )
    ex = {
        "id": "bad-1",
        "gold": {
            "acceptable_product_ids": [["P1"], "not-a-list", ["P2"]],
            "qty_range": [[10, 12], [5, 6]],
            "must_escalate": True,
            "forbidden_allergens": ["milk", "nuts"],
        },
    }
    res = {
        "status": "placed",
        "quote": {
            "lines": [
                "garbage-line",
                {"product_id": "P1", "qty_sales_units": 10},
                {"product_id": "P2", "qty_sales_units": 5},
            ],
            "total_net": 123.45,
        },
        "plan": {
            "lines": [
                {"product_id": "P1", "covered_amount": "11"},
                {"product_id": "P2", "covered_amount": 5.5},
            ]
        },
        "steps": ["bad-step", {"kind": "thought", "data": None}, step_obj],
    }

    def flaky_lookup(pid: str) -> dict[str, Any]:
        if pid == "P1":
            return {"milk": AllergenStatus.free, "nuts": AllergenStatus.free}
        raise RuntimeError("catalog glitch")

    scored = score(ex, res, flaky_lookup)
    assert scored["zero_correction"] is True
    assert scored["escalated"] is True
    assert scored["escalation_match"] is True
    assert scored["allergen_safe"] is False
    assert scored["unsafe"] == ["P2:milk=unknown", "P2:nuts=unknown"]

    fallback_order_dec = score(
        {"gold": {"acceptable_product_ids": [], "must_escalate": True}},
        {"status": "placed", "steps": [], "order": {"decision": {"outcome": "needs_approval"}}},
        lambda _: {},
    )
    assert fallback_order_dec["escalated"] is True
    assert fallback_order_dec["escalation_match"] is True


def test_gold_answer_and_now_resilience(monkeypatch: pytest.MonkeyPatch) -> None:
    ans_fn = gold_answer({"gold": {"acceptable_product_ids": [["470863"], "bad"]}})
    assert ans_fn({"options": [{"id": "opt-1", "product_id": "111"}, {"id": "opt-2", "product_id": "470863"}]}) == {
        "option_id": "opt-2"
    }
    assert ans_fn({"options": [{"id": "opt-1", "product_id": "111"}]}) == {"option_id": "opt-1"}
    assert ans_fn({"options": []}) == {}
    assert ans_fn({"options": [None, {"no_id": True}]}) == {}
    assert ans_fn("not-a-dict") == {}

    monkeypatch.setenv("AIMO_NOW", "not-an-iso-timestamp")
    assert _now() is None


def test_extract_requote_target_and_live_and_inproc_approver_edge_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert LiveApprover is Approver
    seen: set[str] = set()
    assert _extract_requote_target(None, "hotel-aurora", seen) is None
    assert (
        _extract_requote_target(
            {"old_order_id": "O-old", "new_order": {"order_id": "O-exp", "expired": True}},
            "hotel-aurora",
            seen,
        )
        is None
    )
    assert "O-old" in seen and "O-exp" in seen

    assert (
        _extract_requote_target(
            {"new_order": {"order_id": "O-unk", "customer_id": "unknown-co", "status": "pending_approval"}},
            "unknown-co",
            seen,
        )
        is None
    )
    target = _extract_requote_target(
        {
            "old_order_id": "O-1",
            "new_order": {"order_id": "O-2", "customer_id": "hotel-aurora", "status": "pending_approval"},
            "approve_with": {"body": {"quote_hash": "qh-from-approve-with"}},
        },
        "hotel-aurora",
        seen,
    )
    assert target == ("O-2", "hotel-aurora", "qh-from-approve-with")

    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_APPROVER_PIN", raising=False)
    monkeypatch.delenv("AIMO_APPROVER_PINS", raising=False)
    human_auth.reset_for_tests()

    ap = Approver("http://gw.local/", poll_s=0.005)
    posted: list[tuple[str, str]] = []

    def fake_get(url: str, *, params=None, headers=None, timeout=None):
        return httpx.Response(
            200,
            json=[
                "not-a-dict",
                {"order": "bad"},
                {"expired": True, "order": {"order_id": "O-stale", "customer_id": "hotel-aurora"}},
                {"order": {"order_id": "O-chain-1", "customer_id": "hotel-aurora", "quote_hash": "qh-1"}},
            ],
        )

    def fake_post(url: str, *, json=None, headers=None, timeout=None):
        oid = url.rsplit("/", 2)[-2]
        posted.append((oid, (json or {}).get("quote_hash", "")))
        if oid == "O-chain-1":
            return httpx.Response(
                409,
                json={
                    "error": "requote_needed",
                    "detail": {
                        "old_order_id": "O-chain-1",
                        "new_order": {"order_id": "O-chain-2", "customer_id": "hotel-aurora", "status": "pending_approval"},
                        "new_quote": {"quote_hash": "qh-2"},
                    },
                },
            )
        ap.stop.set()
        return httpx.Response(200, json={"order_id": oid, "status": "placed"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    ap.start()
    try:
        ap.join(timeout=2.0)
        assert not ap.is_alive()
    finally:
        ap.stop.set()
    assert posted == [("O-chain-1", "qh-1"), ("O-chain-2", "qh-2")]

    inproc_ap = InprocApprover(poll_s=0.005)
    inproc_calls: list[tuple[str, str]] = []
    transient_count = 0

    def stub_list_approvals(status: str = "pending"):
        return [
            "bad",
            {"expired": True, "order": {"order_id": "O-inproc-exp", "customer_id": "school-tampere"}},
            {"order": {"order_id": "O-inproc-1", "customer_id": "school-tampere", "quote_hash": "iqh-1"}},
        ]

    def stub_decide(order_id: str, **kw: Any):
        nonlocal transient_count
        inproc_calls.append((order_id, kw.get("quote_hash", "")))
        if order_id == "O-inproc-1":
            if transient_count == 0:
                transient_count += 1
                raise service.ServiceError(503, "upstream_busy", "try again")
            raise service.ServiceError(
                409,
                "requote_needed",
                "expired",
                {
                    "old_order_id": "O-inproc-1",
                    "new_order": {
                        "order_id": "O-inproc-2",
                        "customer_id": "school-tampere",
                        "status": "pending_approval",
                        "quote_hash": "iqh-2",
                    },
                },
            )
        inproc_ap.stop.set()
        return {"order_id": order_id, "status": "placed"}

    monkeypatch.setattr(service, "list_approvals", stub_list_approvals)
    monkeypatch.setattr(service, "decide", stub_decide)
    if hasattr(service, "decide_approval"):
        monkeypatch.setattr(service, "decide_approval", stub_decide)

    inproc_ap.start()
    try:
        inproc_ap.join(timeout=2.0)
        assert not inproc_ap.is_alive()
    finally:
        inproc_ap.stop.set()
        human_auth.reset_for_tests()

    assert inproc_calls == [
        ("O-inproc-1", "iqh-1"),
        ("O-inproc-1", "iqh-1"),
        ("O-inproc-2", "iqh-2"),
    ]


def test_eval_gold_keyless_mock_main_and_bad_row_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T15:00:00+02:00")
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    _set_gemini_keys(monkeypatch, "", "")
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini must never be invoked in keyless eval_gold")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)
    assert mock_wanted() is True

    gold_file = tmp_path / "gold_mixed.jsonl"
    rows = [
        {
            "id": "ok-1",
            "segment": "hotel",
            "lang": "en",
            "request": "We need 20 kg of lactose-free butter by Monday.",
            "customer_id": "hotel-aurora",
            "gold": {
                "acceptable_product_ids": [["402105"]],
                "qty_range": [[20, 22]],
                "must_escalate": False,
                "forbidden_allergens": [],
            },
        },
        {
            "id": "ok-2",
            "segment": "hotel",
            "lang": "en",
            "request": "We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.",
            "customer_id": "hotel-aurora",
            "gold": {
                "acceptable_product_ids": [["464306"]],
                "qty_range": [[1000, 1100]],
                "must_escalate": True,
                "forbidden_allergens": [],
            },
        },
        {
            "id": "bad-missing-cust",
            "segment": "broken",
            "lang": "en",
            "request": "10 kg butter by Monday",
            "customer_id": "",
            "gold": {"acceptable_product_ids": [], "must_escalate": False},
        },
    ]
    gold_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    out_dir = tmp_path / "eval_out"
    rc = eval_gold.main([str(gold_file), "--transport", "fake", "--concurrency", "2", "--out", str(out_dir)])
    assert rc == 0

    results = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    report_md = (out_dir / "report.md").read_text(encoding="utf-8")
    assert results["model"] == MOCK_MODEL_NAME
    assert f"model {MOCK_MODEL_NAME} thinking LOW" in report_md
    assert "Mode: deterministic scripted buyer" in report_md
    assert results["summary"]["n"] == 3
    assert results["summary"]["errors"] == 1
    by_id = {r["id"]: r for r in results["rows"]}
    assert by_id["ok-1"]["status"] == "placed" and by_id["ok-1"]["zero_correction"] is True
    assert by_id["ok-2"]["status"] == "placed" and by_id["ok-2"]["escalation_match"] is True
    assert by_id["bad-missing-cust"]["status"] == "error"

    empty_summ = summarize([])
    assert empty_summ["n"] == 0 and empty_summ["overall"]["n"] == 0
    partial_ns = types.SimpleNamespace(model="mock")
    write_report(tmp_path / "empty_rep", [], empty_summ, partial_ns, "2026-11-15 05:00", None)
    assert (tmp_path / "empty_rep" / "report.md").is_file()


def test_vision_rejects_empty_oversized_malformed_bytes_and_bad_mimes() -> None:
    profile = get_profile("hotel-aurora")
    called = False

    def should_not_run(**kw: Any) -> Any:
        nonlocal called
        called = True
        return _txt("{}")

    with pytest.raises(ValueError, match="bytes"):
        extract_order_from_image(None, "image/png", profile, SAT, generate=should_not_run)
    with pytest.raises(ValueError, match="bytes"):
        extract_order_from_image("not-bytes", "image/png", profile, SAT, generate=should_not_run)
    with pytest.raises(ValueError, match="empty"):
        extract_order_from_image(b"", "image/png", profile, SAT, generate=should_not_run)
    with pytest.raises(ValueError, match="maximum size"):
        extract_order_from_image(
            b"\x89PNG\r\n\x1a\n" + (b"0" * MAX_IMAGE_BYTES),
            "image/png",
            profile,
            SAT,
            generate=should_not_run,
        )

    for bad_mime in ("", "   ", None, "text/plain", "image/svg+xml", "video/mp4", "application/json"):
        with pytest.raises(ValueError, match="mime"):
            extract_order_from_image(PNG_VALID, bad_mime, profile, SAT, generate=should_not_run)

    malformed_cases = [
        (b"plain-text-not-png", "image/png"),
        (b"\x89PNG", "image/png"),
        (PNG_VALID, "image/jpeg"),
        (b"not-a-gif-header", "image/gif"),
        (b"RIFF0000NOTW", "image/webp"),
        (b"0000notheic!", "image/heic"),
        (b"not-pdf-data", "application/pdf"),
    ]
    for bad_bytes, mime in malformed_cases:
        with pytest.raises(ValueError, match="malformed"):
            extract_order_from_image(bad_bytes, mime, profile, SAT, generate=should_not_run)

    assert called is False
    assert "image/png" in SUPPORTED_MIMES and "image/jpeg" in SUPPORTED_MIMES


def test_vision_coerces_fenced_and_loose_json_and_retries_transient_failures() -> None:
    profile = get_profile("hotel-aurora")
    fenced_payload = (
        "```json\n"
        + json.dumps(
            {
                "document_type": "receipt",
                "language": None,
                "lines": [
                    {
                        "product": "lactose-free milk",
                        "original_text": "Maito laktoositon 25 l",
                        "quantity": "25",
                        "unit": "l",
                        "notes": None,
                    },
                    {
                        "product": "butter",
                        "original_text": "Voi -5 kg",
                        "quantity": -5,
                        "unit": "kg",
                        "notes": "unsalted",
                    },
                ],
                "delivery_expression": "by Monday",
                "request_text": "",
                "unreadable": None,
            }
        )
        + "\n```"
    )

    seen_mimes: list[str] = []
    attempts = 0

    def flaky_gen(**kw: Any) -> Any:
        nonlocal attempts
        attempts += 1
        seen_mimes.append(kw["meta"]["mime"])
        if attempts == 1:
            raise RuntimeError("transient 503 from upstream")
        return _txt(fenced_payload)

    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    out = extract_order_from_image(
        bytearray(jpeg_bytes),
        "IMAGE/JPG; charset=binary",
        None,
        None,
        generate=flaky_gen,
    )
    assert attempts == 2
    assert seen_mimes == ["image/jpeg", "image/jpeg"]
    assert out["document_type"] == "other"
    assert len(out["lines"]) == 2
    assert out["lines"][0]["quantity"] == 25.0
    assert out["lines"][1]["quantity"] is None
    assert any("non-positive quantity" in u for u in out["unreadable"])
    assert "25 l lactose-free milk" in out["request_text"]
    assert "butter" in out["request_text"]
    assert ImageOrder.model_validate(out).request_text == out["request_text"]


def test_vision_safe_fallback_when_keyless_or_when_gemini_returns_empty_or_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    _set_gemini_keys(monkeypatch, "", "")

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Gemini must not be called in keyless vision fallback")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    assert mock_wanted() is True

    keyless_out = extract_order_from_image(
        PNG_VALID,
        "image/png",
        get_profile("hotel-aurora"),
        SAT,
        hint="  20 kg lactose-free butter by Monday  ",
    )
    assert keyless_out["document_type"] == "other"
    assert keyless_out["lines"] == []
    assert keyless_out["request_text"] == "20 kg lactose-free butter by Monday"
    assert len(keyless_out["unreadable"]) == 1

    class BrokenTextResponse:
        @property
        def text(self) -> str:
            raise ValueError("safety block: no text parts")

        candidates: list[Any] = []

    bad_calls = 0

    def broken_gen(**kw: Any) -> Any:
        nonlocal bad_calls
        bad_calls += 1
        if bad_calls == 1:
            return BrokenTextResponse()
        return _txt("{}")

    fb_out = extract_order_from_image(
        PNG_VALID,
        "image/png",
        get_profile("hotel-aurora"),
        SAT,
        generate=broken_gen,
        hint="100 L oat drink by Wednesday",
        fallback=True,
    )
    assert bad_calls == 2
    assert fb_out["document_type"] == "other"
    assert fb_out["lines"] == []
    assert fb_out["request_text"] == "100 L oat drink by Wednesday"
    assert any("vision failed" in u for u in fb_out["unreadable"])

    direct_fb = fallback_image_order("15 kg cheese", "illegible ink")
    assert direct_fb == {
        "document_type": "other",
        "language": "",
        "lines": [],
        "delivery_expression": "",
        "request_text": "15 kg cheese",
        "unreadable": ["illegible ink"],
    }


def test_profiles_and_mandate_for_contracts() -> None:
    assert set(PROFILES) == {"hotel-aurora", "school-tampere", "hospital-meilahti"}
    required_keys = {
        "customer_id",
        "name",
        "segment",
        "agent_id",
        "policy_id",
        "site",
        "approver",
        "preferred_product_ids",
        "preferences",
        "excluded_allergens",
        "standing_order",
        "mandate_summary",
    }
    for cid in ("hotel-aurora", "school-tampere", "hospital-meilahti"):
        prof = get_profile(cid)
        assert prof is PROFILES[cid]
        assert required_keys <= set(prof)
        m = mandate_for(cid)
        assert isinstance(m, Mandate)
        assert m.customer_id == cid
        assert m.agent_id == f"{cid}/buyer-1"
        assert m.policy_id == cid
        assert m.scopes == ["catalog:read", "quote:create", "order:submit"]
        assert cid in APPROVERS

    rx = re.compile(PROFILES["hotel-aurora"]["usual_match"], re.I)
    assert rx.search("200 L lactose-free milk")
    assert rx.search("laktoositonta maitoa")
    assert not rx.search("200 L oat milk")
    assert not rx.search("kauramaitoa")

    unk = get_profile("cafe-esplanadi")
    assert required_keys <= set(unk)
    assert unk["segment"] == "unknown"
    assert unk["excluded_allergens"] == []
    assert mandate_for("cafe-esplanadi").agent_id == "cafe-esplanadi/buyer-1"

    empty_prof = get_profile(None)
    assert empty_prof["customer_id"] == ""
    assert mandate_for(None).agent_id == "/buyer-1"
