import json
import os
import sqlite3
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

from joserfc import jws
from joserfc.errors import JoseError

from aimo import keys
from aimo.models import Quote

warnings.filterwarnings("ignore", message="EdDSA is deprecated")

ALGS = ["EdDSA"]


class QuoteError(Exception):
    def __init__(self, code, text):
        super().__init__(f"{code}: {text}")
        self.code = code
        self.text = text


def _db():
    path = os.environ.get("AIMO_DB", "var/aimo.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS quotes (quote_id TEXT PRIMARY KEY, customer TEXT, agent_id TEXT,"
        " valid_until TEXT, payload TEXT, jws TEXT, used_at TEXT)"
    )
    return con


def _canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def _audit(customer, agent_id, quote):
    try:
        from aimo import audit
    except ImportError:
        return
    audit.record(
        customer,
        f"agent:{agent_id}",
        "quote.issued",
        quote.quote_id,
        f"Quote {quote.quote_id} for {quote.total_gross:.2f} {quote.currency} issued, valid until {quote.valid_until.isoformat()}.",
        {"quote_id": quote.quote_id, "total_gross": quote.total_gross, "lines": len(quote.lines)},
    )


def issue(customer, agent_id, lines, delivery_date, valid_until, total_gross, currency="EUR"):
    quote = Quote(
        quote_id="q_" + uuid.uuid4().hex[:12],
        customer=customer,
        agent_id=agent_id,
        delivery_date=delivery_date,
        lines=lines,
        total_net=round(sum(l["line_total_net"] if isinstance(l, dict) else l.line_total_net for l in lines), 2),
        total_gross=total_gross,
        currency=currency,
        issued_at=datetime.now(timezone.utc),
        valid_until=valid_until,
    )
    payload = _canon(quote.model_dump(mode="json", exclude={"jws"}))
    token = jws.serialize_compact({"alg": "EdDSA", "kid": keys.kid()}, payload.encode(), keys.signing_key(), algorithms=ALGS)
    quote.jws = token
    with _db() as con:
        con.execute(
            "INSERT INTO quotes (quote_id, customer, agent_id, valid_until, payload, jws) VALUES (?,?,?,?,?,?)",
            (quote.quote_id, customer, agent_id, quote.valid_until.isoformat(), payload, token),
        )
    _audit(customer, agent_id, quote)
    return quote


def get(quote_id):
    with _db() as con:
        row = con.execute("SELECT payload, jws FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    return Quote(**json.loads(row[0]), jws=row[1]) if row else None


def verify(token, jwks=None):
    from joserfc.jwk import KeySet

    ks = KeySet.import_key_set(jwks) if jwks else None
    key = ks if ks else keys.signing_key()
    try:
        obj = jws.deserialize_compact(token, key, algorithms=ALGS)
    except (JoseError, ValueError, KeyError):
        raise QuoteError("BAD_SIGNATURE", "Quote signature is invalid.")
    h = obj.headers()
    if h.get("alg") != "EdDSA" or (not jwks and h.get("kid") != keys.kid()):
        raise QuoteError("BAD_SIGNATURE", "Quote was not signed with an accepted key.")
    try:
        return Quote(**json.loads(obj.payload), jws=token)
    except ValueError:
        raise QuoteError("BAD_SIGNATURE", "Quote payload is malformed.")


def redeem(quote_id, token, customer, agent_id):
    with _db() as con:
        if not con.execute("SELECT 1 FROM quotes WHERE quote_id=?", (quote_id,)).fetchone():
            raise QuoteError("UNKNOWN_QUOTE", f"No quote {quote_id}.")
    quote = verify(token)
    if quote.quote_id != quote_id:
        raise QuoteError("QUOTE_MISMATCH", "Signed quote does not match the quote id.")
    if quote.customer != customer or quote.agent_id != agent_id:
        raise QuoteError("WRONG_AGENT", "Quote was issued to a different agent.")
    now = datetime.now(timezone.utc)
    if now >= quote.valid_until:
        raise QuoteError("QUOTE_EXPIRED", f"Quote expired at {quote.valid_until.isoformat()}.")
    with _db() as con:
        n = con.execute(
            "UPDATE quotes SET used_at=? WHERE quote_id=? AND used_at IS NULL", (now.isoformat(), quote_id)
        ).rowcount
    if not n:
        raise QuoteError("QUOTE_USED", "Quote was already used.")
    return quote
