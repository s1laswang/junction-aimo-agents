"""Ed25519 signing: compact JWS (RFC 7515) with alg "EdDSA" (RFC 8037), JWK thumbprint kid (RFC 7638).

Demo key management: one Ed25519 key generated on first use into aimo.config.KEY_DIR (gitignored).
Production: the private key lives in a KMS/HSM (e.g. Cloud KMS Ed25519) and only the JWKS is public.

JWS is implemented by hand (base64url(header).base64url(payload).base64url(sig)) so that any agent can
verify with any JOSE library: the public key is published at /.well-known/jwks.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ISSUER = "valio-aimo-trusted-trade"
QUOTE_HASH_EXCLUDE = {"quote_hash", "signature", "kid"}

_lock = threading.Lock()
_keys: dict[str, Ed25519PrivateKey] = {}   # key path -> private key


# ------------------------------------------------------------------ encoding helpers
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _normalize(obj: Any) -> Any:
    """Make values JSON-stable: integral floats become ints (as in JavaScript / RFC 8785),
    datetimes and dates become ISO strings, pydantic models become their JSON dump."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError("NaN/Infinity are not allowed in canonical JSON")
        return int(obj) if obj.is_integer() and abs(obj) < 2**53 else obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def canonical_json(obj: Any) -> str:
    """RFC 8785-ish canonical JSON: sorted keys, no whitespace, UTF-8, integral floats as ints.
    (Differs from full JCS only in exotic float formatting, which our data does not use.)"""
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ keys
def _key_path() -> Path:
    from aimo import config  # read at call time so tests can point KEY_DIR elsewhere
    return Path(config.KEY_DIR) / "ed25519-signing.pem"


def _private_key() -> Ed25519PrivateKey:
    path = _key_path()
    k = _keys.get(str(path))
    if k is not None:
        return k
    with _lock:
        k = _keys.get(str(path))
        if k is not None:
            return k
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            new = Ed25519PrivateKey.generate()
            pem = new.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            try:  # O_EXCL: if another process won the race, use its key
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(pem)
            except FileExistsError:
                pass
        k = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(k, Ed25519PrivateKey)
        _keys[str(path)] = k
        return k


def _public_x(pub: Ed25519PublicKey) -> str:
    return b64url(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK thumbprint (SHA-256) for an OKP key: required members crv, kty, x only."""
    req = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}
    return b64url(hashlib.sha256(json.dumps(req, sort_keys=True, separators=(",", ":")).encode()).digest())


def public_jwk() -> dict:
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": _public_x(_private_key().public_key())}
    jwk["kid"] = thumbprint(jwk)
    jwk["alg"] = "EdDSA"
    jwk["use"] = "sig"
    return jwk


def kid() -> str:
    return public_jwk()["kid"]


def jwks() -> dict:
    """JWK Set for /.well-known/jwks.json. Verifiers select the key by kid."""
    return {"keys": [public_jwk()]}


def _public_key_for(kid_: str, jwks_: dict | None = None) -> Ed25519PublicKey | None:
    for k in (jwks_ or jwks()).get("keys", []):
        if k.get("kid") == kid_ and k.get("kty") == "OKP" and k.get("crv") == "Ed25519":
            return Ed25519PublicKey.from_public_bytes(b64url_decode(k["x"]))
    return None


# ------------------------------------------------------------------ JWS
def sign_jws(payload: dict, typ: str = "JOSE") -> str:
    """Compact JWS, protected header {"alg":"EdDSA","kid":...,"typ":...}."""
    header = {"alg": "EdDSA", "kid": kid(), "typ": typ}
    signing_input = f"{b64url(canonical_json(header).encode())}.{b64url(canonical_json(payload).encode())}"
    sig = _private_key().sign(signing_input.encode("ascii"))
    return f"{signing_input}.{b64url(sig)}"


class JWSError(ValueError):
    pass


def verify_jws(token: str, jwks_: dict | None = None) -> tuple[dict, dict]:
    """Verify a compact JWS against the JWKS. Returns (header, payload) or raises JWSError."""
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(b64url_decode(h64))
        payload = json.loads(b64url_decode(p64))
        sig = b64url_decode(s64)
    except Exception as e:  # noqa: BLE001
        raise JWSError(f"malformed JWS: {e}") from e
    if header.get("alg") != "EdDSA":
        raise JWSError(f"unsupported alg {header.get('alg')!r}")  # never accept "none"
    pub = _public_key_for(header.get("kid", ""), jwks_)
    if pub is None:
        raise JWSError(f"unknown kid {header.get('kid')!r}")
    try:
        pub.verify(sig, f"{h64}.{p64}".encode("ascii"))
    except InvalidSignature as e:
        raise JWSError("signature does not verify") from e
    return header, payload


# ------------------------------------------------------------------ quotes
def quote_hash(quote: Any) -> str:
    """sha256 hex of the canonical JSON of the quote without quote_hash/signature/kid."""
    d = quote.model_dump(mode="json") if hasattr(quote, "model_dump") else dict(quote)
    for k in QUOTE_HASH_EXCLUDE:
        d.pop(k, None)
    return sha256_hex(canonical_json(d))


def _epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def sign_quote(quote):
    """Return a copy of the quote with quote_hash, signature (compact JWS) and kid filled.
    JWS payload binds the hash to quote id, customer, agent and expiry."""
    h = quote_hash(quote)
    payload = {"iss": ISSUER, "typ": "quote", "quote_id": quote.quote_id, "quote_hash": h,
               "customer_id": quote.customer_id, "agent_id": quote.agent_id,
               "total_net": quote.total_net, "currency": quote.currency,
               "iat": _epoch(quote.created_at), "exp": _epoch(quote.expires_at)}
    return quote.model_copy(update={"quote_hash": h, "signature": sign_jws(payload, typ="quote+jws"),
                                    "kid": kid()})


def verify_quote_detailed(quote, jwks_: dict | None = None, now: datetime | None = None) -> dict:
    """Everything an agent should check before relying on a quote.
    Returns {ok, signature_valid, hash_matches, expired, reasons[]}. ok excludes expiry; use
    `ok and not expired` before ordering."""
    reasons: list[str] = []
    recomputed = quote_hash(quote)
    hash_matches = quote.quote_hash == recomputed
    if not hash_matches:
        reasons.append("quote content does not match quote_hash (modified after signing)")
    sig_ok = False
    if not quote.signature:
        reasons.append("quote is not signed")
    else:
        try:
            header, payload = verify_jws(quote.signature, jwks_)
            sig_ok = True
            if payload.get("quote_hash") != recomputed:
                sig_ok = False
                reasons.append("signature was made for a different quote_hash")
            if payload.get("quote_id") != quote.quote_id:
                sig_ok = False
                reasons.append("signature was made for a different quote_id")
            if quote.kid and header.get("kid") != quote.kid:
                sig_ok = False
                reasons.append("kid field does not match the JWS header")
        except JWSError as e:
            reasons.append(str(e))
    now = now or datetime.now(timezone.utc)
    expired = _epoch(quote.expires_at) < _epoch(now)
    if expired:
        reasons.append("quote has expired")
    return {"ok": bool(hash_matches and sig_ok), "signature_valid": sig_ok, "hash_matches": hash_matches,
            "expired": expired, "quote_hash": recomputed, "reasons": reasons}


def verify_quote(quote, jwks_: dict | None = None) -> bool:
    """True if the signature verifies and the quote has not been modified. Expiry is checked
    separately (policy rule quote_freshness, check_approval, verify_quote_detailed)."""
    return verify_quote_detailed(quote, jwks_)["ok"]
