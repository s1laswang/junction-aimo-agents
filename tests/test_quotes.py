import base64
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from joserfc import jws
from joserfc.jwk import KeySet

from aimo import keys, quotes
from aimo.models import QuoteLine


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))


def make(hours=2, customer="hotel-aurora", agent="a1"):
    line = QuoteLine(product_id="p1", quantity=10, sales_unit="L", unit_price_net=1.2, line_total_net=12.0)
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    return quotes.issue(customer, agent, [line], date(2026, 11, 16), until, 14.88)


def test_issue_redeem_single_use():
    q = make()
    assert q.quote_id.startswith("q_") and q.total_net == 12.0
    assert quotes.get(q.quote_id).jws == q.jws
    r = quotes.redeem(q.quote_id, q.jws, "hotel-aurora", "a1")
    assert r.total_gross == 14.88 and r.lines[0].product_id == "p1"
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem(q.quote_id, q.jws, "hotel-aurora", "a1")
    assert e.value.code == "QUOTE_USED"


def test_tampered_payload():
    q = make()
    h, p, s = q.jws.split(".")
    d = json.loads(base64.urlsafe_b64decode(p + "=="))
    d["total_gross"] = 1.0
    p2 = base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem(q.quote_id, f"{h}.{p2}.{s}", "hotel-aurora", "a1")
    assert e.value.code == "BAD_SIGNATURE"


def test_expired_wrong_agent_mismatch_unknown():
    q = make(hours=-1)
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem(q.quote_id, q.jws, "hotel-aurora", "a1")
    assert e.value.code == "QUOTE_EXPIRED"
    q = make()
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem(q.quote_id, q.jws, "hotel-aurora", "other")
    assert e.value.code == "WRONG_AGENT"
    q2 = make()
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem(q2.quote_id, q.jws, "hotel-aurora", "a1")
    assert e.value.code == "QUOTE_MISMATCH"
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem("q_nope", q.jws, "hotel-aurora", "a1")
    assert e.value.code == "UNKNOWN_QUOTE"


def test_verify_with_public_jwks_only():
    q = make()
    pub = keys.jwks()
    assert "d" not in pub["keys"][0] and pub["keys"][0]["kid"] == keys.kid()
    assert quotes.verify(q.jws, pub).quote_id == q.quote_id
    assert jws.deserialize_compact(q.jws, KeySet.import_key_set(pub), algorithms=["EdDSA"]).headers()["kid"] == keys.kid()


def test_key_persists(tmp_path):
    k = keys.kid()
    keys._cache.clear()
    assert keys.kid() == k


def test_naive_valid_until_and_audit_and_cli(tmp_path, capsys):
    from aimo import audit

    line = QuoteLine(product_id="p1", quantity=1, unit_price_net=1.0, line_total_net=1.0)
    q = quotes.issue("hotel-aurora", "a1", [line], date(2026, 11, 16), datetime.utcnow() + timedelta(hours=1), 1.24)
    quotes.redeem(q.quote_id, q.jws, "hotel-aurora", "a1")
    ev = audit.events(order_id=q.quote_id)
    assert [e.action for e in ev] == ["quote.issued", "quote.redeemed"]
    assert "1.00 EUR net (1.24 EUR with VAT)" in ev[0].why and "Helsinki time" in ev[0].why
    assert ev[0].detail["total_net"] == 1.0
    f = tmp_path / "jwks.json"
    f.write_text(json.dumps(keys.jwks()))
    assert quotes.main(["x", "verify", q.jws, str(f)]) == 0
    assert "OK " + q.quote_id in capsys.readouterr().out
    assert quotes.main(["x", "verify", q.jws[:-4] + "AAAA", str(f)]) == 1


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=").decode()


def parts(q):
    h, p, s = q.jws.split(".")
    return json.loads(base64.urlsafe_b64decode(h + "==")), json.loads(base64.urlsafe_b64decode(p + "==")), s


def code(q, token):
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem(q.quote_id, token, q.customer, q.agent_id)
    return e.value.code


def test_alg_none_and_hs256_rejected():
    import hashlib
    import hmac

    q = make()
    h, p, _ = parts(q)
    assert code(q, f"{b64({'alg': 'none', 'kid': h['kid']})}.{b64(p)}.") == "BAD_SIGNATURE"
    assert code(q, f"{b64({'alg': 'none'})}.{b64(p)}.AAAA") == "BAD_SIGNATURE"
    for secret in (base64.urlsafe_b64decode(keys.public_jwk()["x"] + "=="), json.dumps(keys.jwks()["keys"][0]).encode(), keys.kid().encode()):
        head = f"{b64({'alg': 'HS256', 'kid': h['kid']})}.{b64(p)}"
        sig = base64.urlsafe_b64encode(hmac.new(secret, head.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        assert code(q, f"{head}.{sig}") == "BAD_SIGNATURE"
    assert quotes.get(q.quote_id) is not None
    quotes.redeem(q.quote_id, q.jws, q.customer, q.agent_id)


def test_kid_swap_and_foreign_key_rejected():
    from joserfc.jwk import OKPKey

    q = make()
    h, p, s = parts(q)
    assert code(q, f"{b64({**h, 'kid': 'evil'})}.{b64(p)}.{s}") == "BAD_SIGNATURE"
    evil = OKPKey.generate_key("Ed25519")
    forged = jws.serialize_compact({"alg": "EdDSA", "kid": keys.kid()}, json.dumps(p).encode(), evil, algorithms=["EdDSA"])
    assert code(q, forged) == "BAD_SIGNATURE"
    forged = jws.serialize_compact({"alg": "EdDSA", "kid": evil.thumbprint()}, json.dumps(p).encode(), evil, algorithms=["EdDSA"])
    assert code(q, forged) == "BAD_SIGNATURE"
    with pytest.raises(quotes.QuoteError):
        quotes.verify(forged, keys.jwks())


@pytest.mark.parametrize("edit", [
    lambda p: p.update(total_gross=0.01),
    lambda p: p["lines"][0].update(quantity=1000),
    lambda p: p["lines"][0].update(unit_price_net=0.01),
    lambda p: p.update(customer="school-hervanta"),
    lambda p: p.update(agent_id="other"),
    lambda p: p.update(valid_until="2099-01-01T00:00:00+00:00"),
])
def test_payload_edits_rejected(edit):
    q = make()
    h, p, s = parts(q)
    edit(p)
    assert code(q, f"{b64(h)}.{b64(p)}.{s}") == "BAD_SIGNATURE"


def test_customer_swap_is_wrong_agent_and_stays_redeemable():
    q = make()
    with pytest.raises(quotes.QuoteError) as e:
        quotes.redeem(q.quote_id, q.jws, "school-hervanta", "a1")
    assert e.value.code == "WRONG_AGENT"
    assert quotes.redeem(q.quote_id, q.jws, "hotel-aurora", "a1").quote_id == q.quote_id


@pytest.mark.parametrize("qty,total,gross", [(-5, -34.3, -38.95), (0, 0.0, 0.0), (5, 6.0, 0.0), (5, 6.0, -1.0)])
def test_invalid_quote_not_issued(qty, total, gross):
    line = QuoteLine(product_id="p1", quantity=qty, unit_price_net=1.2, line_total_net=total)
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    with pytest.raises(quotes.QuoteError) as e:
        quotes.issue("hotel-aurora", "a1", [line], date(2026, 11, 16), until, gross)
    assert e.value.code == "INVALID_QUOTE"
    with pytest.raises(quotes.QuoteError):
        quotes.issue("hotel-aurora", "a1", [], date(2026, 11, 16), until, 5.0)
