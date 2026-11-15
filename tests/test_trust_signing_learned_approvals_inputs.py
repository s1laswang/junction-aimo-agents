"""Unit tests for malformed and hostile inputs across aimo.trust.signing, learned, and approvals."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import date, datetime, timedelta, timezone, tzinfo
import json
import os
import sqlite3
import threading
from typing import Any

import pytest

from aimo import config
from aimo.catalog import textsearch
from aimo.models import Need, Order, PolicyDecision, Product, Quote, QuoteLine, RuleHit
from aimo.trust import approvals, learned, signing
from aimo.trust.audit import AuditLog
from aimo.trust.policy import evaluate, get_mandate
from conformance import verify as conformance_verify

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
MON = date(2026, 11, 16)


class _BrokenTz(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "BROKEN"


@pytest.fixture(autouse=True)
def isolated_trust_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    yield


@pytest.fixture
def audit_log(tmp_path):
    return AuditLog(tmp_path / "audit.sqlite")


def _make_product(pid: str = "464306") -> Product:
    return Product(
        id=pid,
        name="Valio Eila semi skimmed milk drink 1 l lactose free",
        sales_unit="ST",
        category="10400",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        allergens={"milk": "contains"},
        allergen_source={"milk": "structured"},
        brand="Valio",
    )


def _make_quote(
    customer: str = "hotel-aurora",
    agent: str = "buyer-1",
    pid: str = "464306",
    qty: float = 600.0,
    price: float = 1.01,
    now: datetime = NOW,
    minutes: int = 15,
) -> Quote:
    need = Need(
        customer_id=customer,
        product_query="lactose-free milk",
        required_claims=["FREE_FROM_LACTOSE"],
        quantity=qty,
        unit="L",
        delivery_by=MON,
    )
    total = round(qty * price, 2)
    return Quote(
        quote_id=f"Q-{customer}-{pid}",
        customer_id=customer,
        agent_id=f"{customer}/{agent}",
        delivery_date=MON,
        lines=[
            QuoteLine(
                product_id=pid,
                product_name="Valio Eila semi skimmed milk drink 1 l lactose free",
                qty_sales_units=qty,
                sales_unit="ST",
                unit_price=price,
                line_total=total,
                tier="best",
            )
        ],
        total_net=total,
        created_at=now,
        expires_at=now + timedelta(minutes=minutes),
        need=need,
    )


def _raw_token(header_raw: bytes, payload_raw: bytes, sig_raw: bytes) -> str:
    return f"{signing.b64url(header_raw)}.{signing.b64url(payload_raw)}.{signing.b64url(sig_raw)}"


def _sign_custom(header_obj: dict[str, Any], payload_obj: dict[str, Any]) -> str:
    h64 = signing.b64url(json.dumps(header_obj, separators=(",", ":")).encode("utf-8"))
    p64 = signing.b64url(json.dumps(payload_obj, separators=(",", ":")).encode("utf-8"))
    sig = signing._private_key().sign(f"{h64}.{p64}".encode("ascii"))
    return f"{h64}.{p64}.{signing.b64url(sig)}"


def test_b64url_and_canonical_json_strict_validation():
    for raw in (b"", b"a", b"ab", b"abc", b"abcd", os.urandom(32), os.urandom(64)):
        enc = signing.b64url(raw)
        assert signing.b64url_decode(enc) == raw

    for bad_bytes in (None, "abc", 123, ["a"]):
        with pytest.raises(ValueError):
            signing.b64url(bad_bytes)

    for bad_str in (
        None,
        123,
        b"YWJj",
        ["YWJj"],
        "a",
        "abcde",
        "ab$c",
        "ab+c",
        "ab/c",
        "ab c",
        "ab=c",
        "abc===",
        "äbc",
    ):
        with pytest.raises(ValueError):
            signing.b64url_decode(bad_str)

    for bad_obj in (
        float("nan"),
        float("inf"),
        float("-inf"),
        {"x": float("nan")},
        [1, float("inf")],
        {"s": {1, 2}},
        {"o": object()},
    ):
        with pytest.raises(ValueError):
            signing.canonical_json(bad_obj)

    for bad_data in (None, 123, ["x"]):
        with pytest.raises(ValueError):
            signing.sha256_hex(bad_data)


@pytest.mark.parametrize(
    "bad_token",
    [
        None,
        "",
        "   ",
        123,
        True,
        False,
        b"a.b.c",
        [],
        {},
        "onlyone",
        "two.parts",
        "a.b.c.d",
        "..",
        ".YWJj.YWJj",
        "YWJj..YWJj",
        "ab$c.e30." + "A" * 86,
        "e30.ab+c." + "A" * 86,
        "e30.e30.ab$c",
        "e30.e30.a",
    ],
)
def test_verify_jws_rejects_malformed_tokens_with_jws_error(bad_token):
    with pytest.raises(signing.JWSError):
        signing.verify_jws(bad_token)


@pytest.mark.parametrize(
    "header_bytes,payload_bytes",
    [
        (b"[]", b"{}"),
        (b"null", b"{}"),
        (b"123", b"{}"),
        (b'"str"', b"{}"),
        (b"true", b"{}"),
        (b'{"alg":"EdDSA","kid":"k"}', b"[]"),
        (b'{"alg":"EdDSA","kid":"k"}', b"null"),
        (b'{"alg":"EdDSA","kid":"k"}', b"123"),
        (b'{"alg":"EdDSA","kid":"k"}', b'"str"'),
        (b'{"alg":"EdDSA","kid":"k"}', b"false"),
        (b'{"alg":"EdDSA","kid":"k","x":NaN}', b"{}"),
        (b'{"alg":"EdDSA","kid":"k","x":Infinity}', b"{}"),
        (b'{"alg":"EdDSA","kid":"k","x":-Infinity}', b"{}"),
        (b'{"alg":"EdDSA","kid":"k"}', b'{"v":NaN}'),
        (b'{"alg":"EdDSA","kid":"k"}', b'{"v":Infinity}'),
        (b'{"alg":"EdDSA","kid":"k"}', b'{"v":-Infinity}'),
        (b"\xff\xfe", b"{}"),
        (b'{"alg":"EdDSA","kid":"k"}', b"\xff\xfe"),
    ],
)
def test_verify_jws_rejects_non_dict_and_nan_inf_json(header_bytes, payload_bytes):
    tok = _raw_token(header_bytes, payload_bytes, b"\x00" * 64)
    with pytest.raises(signing.JWSError):
        signing.verify_jws(tok)


@pytest.mark.parametrize("bad_kid", [None, "", "   ", 123, True, False, [], {}])
def test_verify_jws_rejects_missing_empty_or_non_str_kid(bad_kid):
    header: dict[str, Any] = {"alg": "EdDSA"}
    if bad_kid is not None:
        header["kid"] = bad_kid
    tok = _sign_custom(header, {"hello": "world"})
    with pytest.raises(signing.JWSError):
        signing.verify_jws(tok)


@pytest.mark.parametrize("bad_alg", ["none", "None", "HS256", "RS256", "", None, 123, ["EdDSA"]])
def test_verify_jws_rejects_non_eddsa_algorithms(bad_alg):
    tok = _sign_custom({"alg": bad_alg, "kid": signing.kid()}, {"hello": "world"})
    with pytest.raises(signing.JWSError):
        signing.verify_jws(tok)


@pytest.mark.parametrize("sig_len", [0, 1, 31, 32, 63, 65, 128])
def test_verify_jws_enforces_64_byte_signature_and_valid_jwks(sig_len):
    valid_tok = signing.sign_jws({"ok": 1})
    h64, p64, s64 = valid_tok.split(".")
    wrong_len_tok = f"{h64}.{p64}.{signing.b64url(b'x' * sig_len)}"
    with pytest.raises(signing.JWSError):
        signing.verify_jws(wrong_len_tok)

    raw_sig = bytearray(signing.b64url_decode(s64))
    raw_sig[0] ^= 0x01
    tampered_tok = f"{h64}.{p64}.{signing.b64url(bytes(raw_sig))}"
    with pytest.raises(signing.JWSError):
        signing.verify_jws(tampered_tok)

    std_b64_sig = base64.b64encode(b"\xff" * 64).decode("ascii")
    if "+" in std_b64_sig or "/" in std_b64_sig or std_b64_sig.endswith("=="):
        with pytest.raises(signing.JWSError):
            signing.verify_jws(f"{h64}.{p64}.{std_b64_sig}")

    for bad_jwks in ([], "bad", 123, True, {}, {"keys": None}, {"keys": "bad"}, {"keys": 123}):
        with pytest.raises(signing.JWSError):
            signing.verify_jws(valid_tok, jwks_=bad_jwks)


def test_thumbprint_and_public_key_for_hostile_jwks_inputs():
    valid_jwk = signing.public_jwk()
    assert signing.thumbprint(valid_jwk) == conformance_verify.thumbprint(valid_jwk)

    bad_jwks_entries = [
        None,
        123,
        "str",
        [],
        {},
        {"kty": "OKP", "crv": "Ed25519"},
        {"kty": "RSA", "crv": "Ed25519", "x": valid_jwk["x"]},
        {"kty": "OKP", "crv": "X25519", "x": valid_jwk["x"]},
        {"kty": "OKP", "crv": "Ed25519", "x": ""},
        {"kty": "OKP", "crv": "Ed25519", "x": None},
        {"kty": "OKP", "crv": "Ed25519", "x": 123},
        {"kty": "OKP", "crv": "Ed25519", "x": ["abc"]},
        {"kty": "OKP", "crv": "Ed25519", "x": "not$base64!"},
        {"kty": "OKP", "crv": "Ed25519", "x": signing.b64url(b"short")},
        {"kty": "OKP", "crv": "Ed25519", "x": signing.b64url(b"x" * 31)},
        {"kty": "OKP", "crv": "Ed25519", "x": signing.b64url(b"x" * 33)},
        {"kty": "OKP", "crv": "Ed25519", "x": signing.b64url(b"x" * 64)},
    ]
    for bad_jwk in bad_jwks_entries:
        with pytest.raises(signing.JWSError):
            signing.thumbprint(bad_jwk)

    k_id = valid_jwk["kid"]
    for bad_kid in (None, "", "   ", 123, True, [], {}):
        assert signing._public_key_for(bad_kid, signing.jwks()) is None

    for bad_set in ([], "bad", 123, {}, {"keys": None}, {"keys": "str"}, {"keys": 123}):
        assert signing._public_key_for(k_id, bad_set) is None

    poisoned_entries = [
        None,
        123,
        "str",
        [],
        {"kid": k_id, "kty": "OKP", "crv": "Ed25519"},
        {"kid": k_id, "kty": "OKP", "crv": "Ed25519", "x": None},
        {"kid": k_id, "kty": "OKP", "crv": "Ed25519", "x": 999},
        {"kid": k_id, "kty": "OKP", "crv": "Ed25519", "x": "bad$b64"},
        {"kid": k_id, "kty": "OKP", "crv": "Ed25519", "x": signing.b64url(b"x" * 16)},
        {"kid": k_id, "kty": "OKP", "crv": "Ed25519", "x": signing.b64url(b"x" * 64)},
    ]
    assert signing._public_key_for(k_id, {"keys": poisoned_entries}) is None
    assert signing._public_key_for(k_id, {"keys": [*poisoned_entries, valid_jwk]}) is not None


def test_verify_quote_detailed_guards_malformed_fields_and_naive_aware_datetimes():
    signed = signing.sign_quote(_make_quote(now=NOW))
    ok_res = signing.verify_quote_detailed(signed, now=NOW)
    assert ok_res["ok"] is True and ok_res["expired"] is False

    dict_res = signing.verify_quote_detailed(signed.model_dump(mode="json"), now=NOW)
    assert dict_res["ok"] is True and dict_res["expired"] is False

    for non_quote in (None, 123, True, False, "quote", b"quote", [], (1, 2), {1, 2}):
        res = signing.verify_quote_detailed(non_quote, now=NOW)
        assert res["ok"] is False and res["hash_matches"] is False and res["signature_valid"] is False
        assert signing.verify_quote(non_quote) is False

    for bad_total in (float("nan"), float("inf"), float("-inf")):
        q_nan = signed.model_copy(update={"total_net": bad_total})
        res = signing.verify_quote_detailed(q_nan, now=NOW)
        assert res["ok"] is False and res["hash_matches"] is False and res["quote_hash"] is None
        assert signing.verify_quote(q_nan) is False

    unhashable_dict = {**signed.model_dump(mode="json"), "provenance": {"bad": object()}}
    res_unhash = signing.verify_quote_detailed(unhashable_dict, now=NOW)
    assert res_unhash["ok"] is False and res_unhash["quote_hash"] is None

    for bad_sig in (None, "", 123, True, False, ["sig"], {"sig": 1}):
        q_sig = signed.model_copy(update={"signature": bad_sig})
        res = signing.verify_quote_detailed(q_sig, now=NOW)
        assert res["ok"] is False and res["signature_valid"] is False

    for bad_qhash in (None, "", 123, True, False, ["h"], {"h": 1}, "0" * 64):
        q_h = signed.model_copy(update={"quote_hash": bad_qhash})
        res = signing.verify_quote_detailed(q_h, now=NOW)
        assert res["ok"] is False and res["hash_matches"] is False

    for bad_kid in (123, True, False, 0, ["k"], {"k": 1}, "wrong-kid"):
        q_kid = signed.model_copy(update={"kid": bad_kid})
        res = signing.verify_quote_detailed(q_kid, now=NOW)
        assert res["ok"] is False and res["signature_valid"] is False

    for bad_qid in (None, "", 123, "Q-other"):
        q_id = signed.model_copy(update={"quote_id": bad_qid})
        res = signing.verify_quote_detailed(q_id, now=NOW)
        assert res["ok"] is False and res["signature_valid"] is False

    naive_created = datetime(2026, 11, 14, 12, 0)
    naive_expires = datetime(2026, 11, 14, 12, 15)
    naive_signed = signing.sign_quote(_make_quote(now=naive_created).model_copy(update={"expires_at": naive_expires}))

    r1 = signing.verify_quote_detailed(naive_signed, now=datetime(2026, 11, 14, 12, 5, tzinfo=timezone.utc))
    assert r1["ok"] is True and r1["expired"] is False

    r2 = signing.verify_quote_detailed(signed, now=datetime(2026, 11, 14, 12, 5))
    assert r2["ok"] is True and r2["expired"] is False

    r3 = signing.verify_quote_detailed(naive_signed, now=datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc))
    assert r3["ok"] is True and r3["expired"] is True

    broken_dt = datetime(2026, 11, 14, 12, 5, tzinfo=_BrokenTz())
    r_broken_tz = signing.verify_quote_detailed(signed, now=broken_dt)
    assert r_broken_tz["ok"] is True and r_broken_tz["expired"] is False

    for bad_exp in (None, "", "not-a-date", 123, [], {}):
        q_exp = signed.model_copy(update={"expires_at": bad_exp})
        res = signing.verify_quote_detailed(q_exp, now=NOW)
        assert res["ok"] is False and res["expired"] is True

    for bad_now in ("not-a-date", 123, True, []):
        res = signing.verify_quote_detailed(signed, now=bad_now)
        assert res["expired"] is True


@pytest.mark.parametrize(
    "bad_id",
    [
        True,
        False,
        None,
        123,
        3.14,
        [],
        {},
        "",
        "   ",
        "\t\n",
        "id\x00evil",
        "\x00",
        "x" * 129,
    ],
)
def test_learned_remember_swap_rejects_bad_ids_and_guards_read_paths(monkeypatch, bad_id):
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {})
    with pytest.raises(ValueError):
        learned.remember_swap(bad_id, "463038", "469363")
    with pytest.raises(ValueError):
        learned.remember_swap("hotel-aurora", "463038", bad_id)
    if bad_id is not None:
        with pytest.raises(ValueError):
            learned.remember_swap("hotel-aurora", bad_id, "469363")

    assert learned.forget_swap(bad_id, "469363") == 0
    assert learned.forget_swap("hotel-aurora", bad_id) == 0
    if bad_id is not None:
        assert learned.forget_swap("hotel-aurora", "469363", from_product_id=bad_id) == 0
    assert learned.list_learned(bad_id) == []
    assert learned.match(bad_id, "469363") is None
    assert learned.match("hotel-aurora", bad_id) is None
    if bad_id is not None:
        assert learned.match("hotel-aurora", "469363", reference_id=bad_id) is None


def test_learned_scopes_metadata_corrupted_db_kind_and_match_guards(monkeypatch):
    monkeypatch.setattr(
        textsearch,
        "get_rows",
        lambda ids: {
            "469363": {
                "id": "469363",
                "name": "Arla Semi skimmed milk drink 1 L lactose free",
                "name_en": "Arla Semi skimmed milk drink 1 L lactose free",
                "category": "10400",
                "claims": ["FREE_FROM_LACTOSE"],
                "content_unit": "L",
            }
        },
    )

    max_cid = "c" * 128
    max_tid = "p" * 128
    saved_none_ref = learned.remember_swap(max_cid, None, max_tid, "product")
    assert saved_none_ref["customer_id"] == max_cid
    assert saved_none_ref["from_product_id"] is None
    assert saved_none_ref["to_product_id"] == max_tid

    for bad_scope in (True, False, None, 123, "", "global", ["product"]):
        with pytest.raises(ValueError):
            learned.remember_swap("hotel-aurora", None, "469363", scope=bad_scope)
        if bad_scope is not None:
            assert learned.forget_swap("hotel-aurora", "469363", scope=bad_scope) == 0

    for kw in (
        {"approver": 123},
        {"approver": True},
        {"approver": "a\x00b"},
        {"approver_role": ["role"]},
        {"quote_id": 999},
        {"order_id": {"x": 1}},
        {"note": 123},
        {"note": "n" * 1025},
    ):
        with pytest.raises(ValueError):
            learned.remember_swap("hotel-aurora", "463038", "469363", **kw)

    learned.remember_swap("hotel-aurora", "463038", "469363", scope="product_type")

    valid_facts = {
        "category": "10400",
        "types": learned.list_learned("hotel-aurora")[0]["kind"]["types"],
        "claims": ["FREE_FROM_LACTOSE"],
        "content_unit": "L",
    }
    assert learned.match("hotel-aurora", "900101", reference_id="463038", product=valid_facts) is not None

    for bad_product in (
        "not-a-dict",
        123,
        True,
        ["10400"],
        {"category": "10400", "types": "milk", "claims": ["FREE_FROM_LACTOSE"], "content_unit": "L"},
        {"category": "10400", "types": 123, "claims": ["FREE_FROM_LACTOSE"], "content_unit": "L"},
        {"category": "10400", "types": {"milk": 1}, "claims": ["FREE_FROM_LACTOSE"], "content_unit": "L"},
        {"category": "10400", "types": [123, {}], "claims": ["FREE_FROM_LACTOSE"], "content_unit": "L"},
        {"category": "10400", "types": valid_facts["types"], "claims": "FREE_FROM_LACTOSE", "content_unit": "L"},
        {"category": "10400", "types": valid_facts["types"], "claims": 123, "content_unit": "L"},
        {"category": "10400", "types": valid_facts["types"], "claims": [True, {}], "content_unit": "L"},
    ):
        assert learned.match("hotel-aurora", "900101", reference_id="463038", product=bad_product) is None

    for idx, raw_kind in enumerate(["[]", '"x"', "123", "true", "null", "{bad-json", ""]):
        pid = f"corrupt-{idx}"
        learned.remember_swap("school-tampere", None, pid, scope="product_type")
        with closing(sqlite3.connect(str(config.STATE_DB))) as con, con:
            con.execute(
                "UPDATE learned_swaps SET kind = ? WHERE customer_id = ? AND to_product_id = ?",
                (raw_kind, "school-tampere", pid),
            )
    rows = learned.list_learned("school-tampere")
    assert len(rows) == 7
    for r in rows:
        assert r["kind"] == {} and isinstance(r["kind"], dict)
    assert learned.match("school-tampere", "other-prod", product=valid_facts) is None


def test_learned_concurrent_sqlite_thread_safety(monkeypatch):
    monkeypatch.setattr(textsearch, "get_rows", lambda ids: {})
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            cid = f"hotel-aurora-{idx % 4}"
            pid = f"prod-{idx}"
            for _ in range(8):
                learned.remember_swap(cid, "463038", pid, scope="product")
                _ = learned.list_learned(cid)
                _ = learned.match(cid, pid, reference_id="463038")
                learned.forget_swap(cid, pid)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_check_approval_guards_non_str_fields_and_invalid_decision_kinds(audit_log):
    now = datetime.now(timezone.utc)
    q = signing.sign_quote(_make_quote(qty=600, now=now))
    d = evaluate(
        q,
        get_mandate("hotel-aurora", "buyer-1"),
        products={"464306": _make_product()},
        audit_log=audit_log,
    )
    assert d.outcome == "needs_approval"

    ok = approvals.check_approval(
        q,
        d,
        "anna@hotel-aurora.example",
        "procurement_manager",
        q.quote_hash,
        note=None,
        audit_log=audit_log,
    )
    assert ok["ok"] is True

    for bad_hash in (None, "", 123, True, False, [], {}, object()):
        r = approvals.check_approval(
            q,
            d,
            "anna@hotel-aurora.example",
            "procurement_manager",
            bad_hash,
            audit_log=audit_log,
        )
        assert r["ok"] is False and "hash mismatch" in r["reason"]

    for bad_approver in (None, "", "   ", "human:   ", 123, True, False, [], {}, object()):
        r = approvals.check_approval(
            q,
            d,
            bad_approver,
            "procurement_manager",
            q.quote_hash,
            audit_log=audit_log,
        )
        assert r["ok"] is False

    for bad_role in (123, True, False, ["procurement_manager"], {"r": 1}, object()):
        r = approvals.check_approval(
            q,
            d,
            "anna@hotel-aurora.example",
            bad_role,
            q.quote_hash,
            audit_log=audit_log,
        )
        assert r["ok"] is False and "approver_role must be a string" in r["reason"]

    for bad_note in (123, True, False, ["note"], {"n": 1}, object()):
        r = approvals.check_approval(
            q,
            d,
            "anna@hotel-aurora.example",
            "procurement_manager",
            q.quote_hash,
            note=bad_note,
            audit_log=audit_log,
        )
        assert r["ok"] is False and "note must be a string" in r["reason"]

    rejected_dec = PolicyDecision(
        outcome="reject",
        approver_role="procurement_manager",
        hits=[RuleHit(rule_id="spend", outcome="reject", message="over cap")],
        policy_id="hotel-aurora",
        policy_version="1.2.0",
    )
    expired_now = now + timedelta(hours=2)
    for bad_kind in (None, "", "bypass", "auto_approve", 123, True, False, [], {}, object()):
        r_kind = approvals.check_approval(
            q,
            d,
            "anna@hotel-aurora.example",
            "procurement_manager",
            q.quote_hash,
            decision_kind=bad_kind,
            audit_log=audit_log,
        )
        assert r_kind["ok"] is False and "decision_kind" in r_kind["reason"]

        r_rej = approvals.check_approval(
            q,
            rejected_dec,
            "anna@hotel-aurora.example",
            "procurement_manager",
            q.quote_hash,
            decision_kind=bad_kind,
            now=expired_now,
            audit_log=audit_log,
        )
        assert r_rej["ok"] is False
        assert "hard rule" in r_rej["reason"] and "expired" in r_rej["reason"]

    assert audit_log.verify()["ok"] is True


def test_log_decision_and_log_order_handle_ab_and_unknown_codes_cleanly(audit_log):
    now = datetime.now(timezone.utc)
    q = signing.sign_quote(_make_quote(qty=100, now=now))

    for outcome, expected in (
        ("auto_approve", "AUTO-APPROVED"),
        ("needs_approval", "NEEDS APPROVAL"),
        ("reject", "REJECTED"),
        ("escalated", "ESCALATED"),
        (None, "UNKNOWN"),
        (123, "UNKNOWN"),
    ):
        dec = PolicyDecision.model_construct(
            outcome=outcome,
            approver_role=123 if outcome == "needs_approval" else "procurement_manager",
            hits=[RuleHit(rule_id="custom:1", outcome="needs_approval", message="custom check")],
            policy_id="hotel-aurora",
            policy_version="1.2.0",
        )
        ev = approvals.log_decision(q, dec, order_id="O-dec", audit_log=audit_log)
        assert expected in ev.summary

    base_dec = PolicyDecision(
        outcome="auto_approve",
        policy_id="hotel-aurora",
        policy_version="1.2.0",
    )
    for code, expected_snippet in (
        ("AP", "supplier response AP (accepted)"),
        ("CA", "supplier response CA (accepted with changes)"),
        ("RE", "supplier response RE (rejected)"),
        ("AB", "supplier response AB (acknowledged)"),
        ("IP", "supplier response IP"),
        ("CUSTOM_99", "supplier response CUSTOM_99"),
        (404, "supplier response 404"),
        (None, ""),
        ("", ""),
    ):
        o = Order.model_construct(
            order_id=f"O-{code}",
            quote_id=q.quote_id,
            quote_hash=q.quote_hash,
            customer_id="hotel-aurora",
            agent_id="hotel-aurora/buyer-1",
            status="placed",
            decision=base_dec,
            response_code=code,
            response_lines=[],
            created_at=now,
            updated_at=now,
        )
        ev = approvals.log_order(o, q, note=None, audit_log=audit_log)
        if expected_snippet:
            assert expected_snippet in ev.summary
        else:
            assert "supplier response" not in ev.summary

    weird_order = Order.model_construct(
        order_id="O-weird",
        quote_id=q.quote_id,
        quote_hash=None,
        customer_id="hotel-aurora",
        agent_id="hotel-aurora/buyer-1",
        status="custom_status",
        decision=None,
        approved_by=123,
        response_code="AB",
        response_lines=[],
        created_at=now,
        updated_at=now,
    )
    ev_weird = approvals.log_order(weird_order, None, actor="", note=123, audit_log=audit_log)
    assert "custom_status" in ev_weird.summary and "AB (acknowledged)" in ev_weird.summary
    assert audit_log.verify()["ok"] is True
