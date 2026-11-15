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
import math
import os
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ISSUER = "valio-aimo-trusted-trade"
QUOTE_HASH_EXCLUDE = {"quote_hash", "signature", "kid"}
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")

_lock = threading.Lock()
_keys: dict[str, Ed25519PrivateKey] = {}


class JWSError(ValueError):
    pass


def _reject_json_constant(val: str) -> Any:
    raise ValueError(f"invalid JSON constant: {val}")


def b64url(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("expected bytes")
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    if not isinstance(s, str):
        raise ValueError("expected base64url string")
    if s == "":
        return b""
    stripped = s.rstrip("=")
    if "=" in stripped or len(s) - len(stripped) > 2 or len(stripped) % 4 == 1 or not _B64URL_RE.fullmatch(stripped):
        raise ValueError("malformed base64url string")
    return base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))


def _normalize(obj: Any) -> Any:
    """Make values JSON-stable: integral floats become ints (as in JavaScript / RFC 8785),
    datetimes and dates become ISO strings, pydantic models become their JSON dump."""
    if hasattr(obj, "model_dump"):
        try:
            obj = obj.model_dump(mode="json", warnings=False)
        except TypeError:
            obj = obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("NaN/Infinity are not allowed in canonical JSON")
        return int(obj) if obj.is_integer() and abs(obj) < 2**53 else obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def canonical_json(obj: Any) -> str:
    """RFC 8785-ish canonical JSON: sorted keys, no whitespace, UTF-8, integral floats as ints.
    (Differs from full JCS only in exotic float formatting, which our data does not use.)"""
    try:
        return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except TypeError as e:
        raise ValueError(f"value is not JSON serializable: {e}") from e


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
    else:
        raise ValueError("expected str or bytes")
    return hashlib.sha256(raw).hexdigest()


def _key_path() -> Path:
    from aimo import config

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
            pem = new.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(pem)
            except FileExistsError:
                pass
        k = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(k, Ed25519PrivateKey):
            raise JWSError("configured signing key is not an Ed25519 private key")
        _keys[str(path)] = k
        return k


def _public_x(pub: Ed25519PublicKey) -> str:
    return b64url(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK thumbprint (SHA-256) for an OKP key: required members crv, kty, x only."""
    if not isinstance(jwk, dict):
        raise JWSError("invalid OKP JWK: expected dict")
    kty = jwk.get("kty")
    crv = jwk.get("crv")
    x = jwk.get("x")
    if kty != "OKP" or crv != "Ed25519" or not isinstance(x, str) or not x:
        raise JWSError("invalid OKP Ed25519 JWK")
    try:
        raw = b64url_decode(x)
    except ValueError as e:
        raise JWSError(f"invalid OKP Ed25519 JWK x: {e}") from e
    if len(raw) != 32:
        raise JWSError("invalid Ed25519 public key length: expected 32 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except Exception as e:
        raise JWSError(f"invalid Ed25519 public key: {e}") from e
    req = {"crv": crv, "kty": kty, "x": x}
    return b64url(hashlib.sha256(json.dumps(req, sort_keys=True, separators=(",", ":")).encode("utf-8")).digest())


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
    if not isinstance(kid_, str) or not kid_.strip():
        return None
    source = jwks() if jwks_ is None else jwks_
    if not isinstance(source, dict):
        return None
    keys = source.get("keys")
    if not isinstance(keys, list):
        return None
    for k in keys:
        if not isinstance(k, dict):
            continue
        if k.get("kid") == kid_ and k.get("kty") == "OKP" and k.get("crv") == "Ed25519":
            x = k.get("x")
            if not isinstance(x, str) or not x:
                continue
            try:
                raw = b64url_decode(x)
                if len(raw) != 32:
                    continue
                return Ed25519PublicKey.from_public_bytes(raw)
            except Exception:
                continue
    return None


def sign_jws(payload: dict, typ: str = "JOSE") -> str:
    """Compact JWS, protected header {"alg":"EdDSA","kid":...,"typ":...}."""
    header = {"alg": "EdDSA", "kid": kid(), "typ": typ}
    signing_input = f"{b64url(canonical_json(header).encode())}.{b64url(canonical_json(payload).encode())}"
    sig = _private_key().sign(signing_input.encode("ascii"))
    return f"{signing_input}.{b64url(sig)}"


def verify_jws(token: str, jwks_: dict | None = None) -> tuple[dict, dict]:
    """Verify a compact JWS against the JWKS. Returns (header, payload) or raises JWSError."""
    if not isinstance(token, str) or not token:
        raise JWSError("malformed JWS: token must be a non-empty string")
    try:
        h64, p64, s64 = token.split(".")
        if not h64 or not p64:
            raise ValueError("empty header or payload segment")
        header = json.loads(b64url_decode(h64).decode("utf-8"), parse_constant=_reject_json_constant)
        payload = json.loads(b64url_decode(p64).decode("utf-8"), parse_constant=_reject_json_constant)
        sig = b64url_decode(s64)
    except Exception as e:
        raise JWSError(f"malformed JWS: {e}") from e
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise JWSError("malformed JWS: header and payload must be JSON objects")
    if header.get("alg") != "EdDSA":
        raise JWSError(f"unsupported alg {header.get('alg')!r}")
    kid_val = header.get("kid")
    if not isinstance(kid_val, str) or not kid_val.strip():
        raise JWSError(f"invalid or missing kid {kid_val!r}")
    if jwks_ is not None and (not isinstance(jwks_, dict) or not isinstance(jwks_.get("keys"), list)):
        raise JWSError("invalid JWKS")
    if len(sig) != 64:
        raise JWSError("malformed JWS: Ed25519 signature must be 64 bytes")
    pub = _public_key_for(kid_val, jwks_)
    if pub is None:
        raise JWSError(f"unknown kid {kid_val!r}")
    try:
        signing_input = f"{h64}.{p64}".encode("ascii")
        pub.verify(sig, signing_input)
    except (InvalidSignature, UnicodeEncodeError, ValueError, TypeError) as e:
        raise JWSError("signature does not verify") from e
    return header, payload


def quote_hash(quote: Any) -> str:
    """sha256 hex of the canonical JSON of the quote without quote_hash/signature/kid."""
    if hasattr(quote, "model_dump"):
        try:
            d = quote.model_dump(mode="json", warnings=False)
        except TypeError:
            d = quote.model_dump(mode="json")
    elif isinstance(quote, dict):
        d = dict(quote)
    else:
        raise ValueError("quote must be a Quote model or dict")
    if not isinstance(d, dict):
        raise ValueError("quote dump must be a dict")
    for k in QUOTE_HASH_EXCLUDE:
        d.pop(k, None)
    return sha256_hex(canonical_json(d))


def _to_aware_utc(val: Any) -> datetime:
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        s = val.strip()
        if not s:
            raise ValueError("empty datetime string")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    else:
        raise TypeError(f"expected datetime or ISO string, got {type(val).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _epoch(dt: datetime) -> int:
    return int(_to_aware_utc(dt).timestamp())


def sign_quote(quote):
    """Return a copy of the quote with quote_hash, signature (compact JWS) and kid filled.
    JWS payload binds the hash to quote id, customer, agent and expiry."""
    h = quote_hash(quote)
    payload = {
        "iss": ISSUER,
        "typ": "quote",
        "quote_id": quote.quote_id,
        "quote_hash": h,
        "customer_id": quote.customer_id,
        "agent_id": quote.agent_id,
        "total_net": quote.total_net,
        "currency": quote.currency,
        "iat": _epoch(quote.created_at),
        "exp": _epoch(quote.expires_at),
    }
    return quote.model_copy(
        update={
            "quote_hash": h,
            "signature": sign_jws(payload, typ="quote+jws"),
            "kid": kid(),
        }
    )


def _qfield(quote: Any, name: str) -> Any:
    if isinstance(quote, dict):
        return quote.get(name)
    return getattr(quote, name, None)


def verify_quote_detailed(quote, jwks_: dict | None = None, now: datetime | None = None) -> dict:
    """Everything an agent should check before relying on a quote.
    Returns {ok, signature_valid, hash_matches, expired, reasons[]}. ok excludes expiry; use
    `ok and not expired` before ordering."""
    if quote is None or isinstance(quote, (bool, int, float, str, bytes, bytearray, list, tuple, set, frozenset)):
        return {
            "ok": False,
            "signature_valid": False,
            "hash_matches": False,
            "expired": True,
            "quote_hash": None,
            "reasons": ["invalid quote object"],
        }
    reasons: list[str] = []
    try:
        recomputed: str | None = quote_hash(quote)
    except (ValueError, TypeError, AttributeError) as e:
        recomputed = None
        hash_matches = False
        reasons.append(f"quote cannot be hashed: {e}")
    else:
        claimed_hash = _qfield(quote, "quote_hash")
        if not isinstance(claimed_hash, str) or not claimed_hash:
            hash_matches = False
            reasons.append("quote content does not match quote_hash (modified after signing)")
        else:
            hash_matches = claimed_hash == recomputed
            if not hash_matches:
                reasons.append("quote content does not match quote_hash (modified after signing)")
    sig_ok = False
    raw_sig = _qfield(quote, "signature")
    if raw_sig is None or raw_sig == "":
        reasons.append("quote is not signed")
    elif not isinstance(raw_sig, str):
        reasons.append("quote signature must be a string")
    else:
        try:
            header, payload = verify_jws(raw_sig, jwks_)
            sig_ok = True
            if recomputed is None or payload.get("quote_hash") != recomputed:
                sig_ok = False
                reasons.append("signature was made for a different quote_hash")
            qid = _qfield(quote, "quote_id")
            if not isinstance(qid, str) or not qid or payload.get("quote_id") != qid:
                sig_ok = False
                reasons.append("signature was made for a different quote_id")
            raw_kid = _qfield(quote, "kid")
            if raw_kid is not None and raw_kid != "":
                if not isinstance(raw_kid, str) or header.get("kid") != raw_kid:
                    sig_ok = False
                    reasons.append("kid field does not match the JWS header")
        except JWSError as e:
            sig_ok = False
            reasons.append(str(e))
    expires_valid = True
    raw_expires = _qfield(quote, "expires_at")
    try:
        exp_dt = _to_aware_utc(raw_expires)
    except (ValueError, TypeError, OverflowError):
        expires_valid = False
        expired = True
        reasons.append("invalid expires_at")
    else:
        if now is None:
            now_dt = datetime.now(timezone.utc)
            expired = _epoch(exp_dt) < _epoch(now_dt)
            if expired:
                reasons.append("quote has expired")
        else:
            try:
                now_dt = _to_aware_utc(now)
            except (ValueError, TypeError, OverflowError):
                expired = True
                reasons.append("invalid now timestamp")
            else:
                expired = _epoch(exp_dt) < _epoch(now_dt)
                if expired:
                    reasons.append("quote has expired")
    return {
        "ok": bool(hash_matches and sig_ok and expires_valid),
        "signature_valid": sig_ok,
        "hash_matches": hash_matches,
        "expired": expired,
        "quote_hash": recomputed,
        "reasons": reasons,
    }


def verify_quote(quote, jwks_: dict | None = None) -> bool:
    """True if the signature verifies and the quote has not been modified. Expiry is checked
    separately (policy rule quote_freshness, check_approval, verify_quote_detailed)."""
    return verify_quote_detailed(quote, jwks_)["ok"]
