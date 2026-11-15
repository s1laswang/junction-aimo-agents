"""Independent verification primitives for the Trusted Trade profile (no aimo imports, `cryptography` only).

A conformance suite must not trust the implementation it tests, so this re-implements, from docs/TRUST.md:
  - canonical JSON (sorted keys, no whitespace, UTF-8, integral numbers without ".0"; RFC 8785-style),
  - JWK thumbprints (RFC 7638) for Ed25519 OKP keys,
  - compact JWS (RFC 7515) with alg EdDSA (RFC 8037) verified against a JWKS,
  - quote hash binding and audit event hashes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

QUOTE_EXCLUDE = ("quote_hash", "signature", "kid")
EVENT_EXCLUDE = ("hash", "signature", "kid")
GENESIS = "0" * 64
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")


def _reject_json_constant(val: str) -> Any:
    raise ValueError(f"invalid JSON constant: {val}")


def b64d(s: str) -> bytes:
    if not isinstance(s, str):
        raise ValueError("expected base64url string")
    if s == "":
        return b""
    stripped = s.rstrip("=")
    if "=" in stripped or len(s) - len(stripped) > 2 or len(stripped) % 4 == 1 or not _B64URL_RE.fullmatch(stripped):
        raise ValueError("malformed base64url string")
    return base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))


def b64e(b: bytes) -> str:
    if not isinstance(b, (bytes, bytearray, memoryview)):
        raise ValueError("expected bytes")
    return base64.urlsafe_b64encode(bytes(b)).rstrip(b"=").decode("ascii")


def _norm(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _norm(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_norm(v) for v in o]
    if isinstance(o, float):
        if not math.isfinite(o):
            raise ValueError("non-finite float in canonical JSON")
        if o.is_integer() and abs(o) < 2**53:
            return int(o)
    return o


def canonical_json(o: Any) -> str:
    try:
        return json.dumps(_norm(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except TypeError as e:
        raise ValueError(f"value is not JSON serializable: {e}") from e


def sha256_hex(s: str) -> str:
    if not isinstance(s, str):
        raise ValueError("expected string")
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def thumbprint(jwk: dict) -> str:
    """RFC 7638 for OKP: members crv, kty, x in lexicographic order."""
    if not isinstance(jwk, dict):
        raise ValueError("invalid OKP JWK: expected dict")
    kty = jwk.get("kty")
    crv = jwk.get("crv")
    x = jwk.get("x")
    if kty != "OKP" or crv != "Ed25519" or not isinstance(x, str) or not x:
        raise ValueError("invalid OKP Ed25519 JWK")
    raw = b64d(x)
    if len(raw) != 32:
        raise ValueError("invalid Ed25519 public key length: expected 32 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except Exception as e:
        raise ValueError(f"invalid Ed25519 public key: {e}") from e
    return b64e(hashlib.sha256(canonical_json({"crv": crv, "kty": kty, "x": x}).encode("utf-8")).digest())


def key_for(jwks: dict, kid: str) -> Ed25519PublicKey | None:
    if not isinstance(jwks, dict) or not isinstance(kid, str) or not kid:
        return None
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return None
    for k in keys:
        if not isinstance(k, dict):
            continue
        if k.get("kid") == kid and k.get("kty") == "OKP" and k.get("crv") == "Ed25519":
            x = k.get("x")
            if not isinstance(x, str) or not x:
                continue
            try:
                raw = b64d(x)
                if len(raw) != 32:
                    continue
                return Ed25519PublicKey.from_public_bytes(raw)
            except Exception:
                continue
    return None


def verify_jws(token: str, jwks: dict) -> tuple[dict, dict]:
    """Returns (header, payload); raises ValueError. Refuses alg other than EdDSA (incl. "none")."""
    if not isinstance(token, str) or not token:
        raise ValueError("malformed JWS: token must be a non-empty string")
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(b64d(h64).decode("utf-8"), parse_constant=_reject_json_constant)
        payload = json.loads(b64d(p64).decode("utf-8"), parse_constant=_reject_json_constant)
    except Exception as e:
        raise ValueError(f"malformed JWS: {e}") from e
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("malformed JWS: header and payload must be JSON objects")
    if header.get("alg") != "EdDSA":
        raise ValueError(f"alg {header.get('alg')!r} refused")
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise ValueError("invalid JWKS")
    kid_val = header.get("kid", "")
    if not isinstance(kid_val, str) or not kid_val:
        raise ValueError(f"unknown kid {kid_val!r}")
    pub = key_for(jwks, kid_val)
    if pub is None:
        raise ValueError(f"unknown kid {kid_val!r}")
    try:
        sig_bytes = b64d(s64)
        if len(sig_bytes) != 64:
            raise ValueError("Ed25519 signature must be 64 bytes")
        signing_input = f"{h64}.{p64}".encode("ascii")
    except Exception as e:
        raise ValueError(f"malformed JWS signature: {e}") from e
    try:
        pub.verify(sig_bytes, signing_input)
    except InvalidSignature as e:
        raise ValueError("signature does not verify") from e
    return header, payload


def quote_hash(quote: dict) -> str:
    if not isinstance(quote, dict):
        raise ValueError("quote must be a JSON object")
    return sha256_hex(canonical_json({k: v for k, v in quote.items() if k not in QUOTE_EXCLUDE}))


def verify_quote(quote: dict, jwks: dict) -> dict:
    """{ok, hash_matches, signature_valid, reasons}: hash binding + EdDSA signature over the hash."""
    if not isinstance(quote, dict):
        return {"ok": False, "hash_matches": False, "signature_valid": False, "quote_hash": None,
                "reasons": ["quote must be a JSON object"]}
    reasons: list[str] = []
    sig_ok = False
    try:
        h: str | None = quote_hash(quote)
    except (ValueError, TypeError) as e:
        h = None
        hash_ok = False
        reasons.append(f"quote cannot be canonicalized: {e}")
    else:
        claimed_hash = quote.get("quote_hash")
        if not isinstance(claimed_hash, str) or not claimed_hash:
            hash_ok = False
            reasons.append("missing or non-string quote_hash")
        else:
            hash_ok = h == claimed_hash
            if not hash_ok:
                reasons.append("quote content does not match quote_hash")
    raw_sig = quote.get("signature")
    if not isinstance(raw_sig, str) or not raw_sig:
        reasons.append("missing or non-string signature")
    else:
        try:
            header, payload = verify_jws(raw_sig, jwks)
            qid = quote.get("quote_id")
            sig_ok = (
                h is not None
                and isinstance(qid, str)
                and bool(qid)
                and payload.get("quote_hash") == h
                and payload.get("quote_id") == qid
            )
            if not sig_ok:
                reasons.append("JWS payload is for a different quote_hash / quote_id")
            if "kid" in quote and quote["kid"] is not None:
                if not isinstance(quote["kid"], str) or header.get("kid") != quote["kid"]:
                    sig_ok = False
                    reasons.append("kid does not match the JWS header")
        except ValueError as e:
            reasons.append(str(e))
    return {"ok": bool(hash_ok and sig_ok), "hash_matches": hash_ok, "signature_valid": sig_ok, "quote_hash": h,
            "reasons": reasons}


def event_hash(ev: dict) -> str:
    if not isinstance(ev, dict):
        raise ValueError("event must be a JSON object")
    prev_h = ev.get("prev_hash")
    if not isinstance(prev_h, str) or not prev_h:
        raise ValueError("missing or non-string prev_hash")
    return sha256_hex(prev_h + canonical_json({k: v for k, v in ev.items() if k not in EVENT_EXCLUDE}))


def _is_seq(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def verify_events(events: list[dict], jwks: dict, contiguous: bool = False) -> dict:
    """Each event: hash recomputes and its JWS (payload {typ, seq, hash}) verifies. With contiguous=True
    (the full chain), prev_hash must link to the previous event and the first to GENESIS."""
    if not isinstance(events, list):
        return {"ok": False, "checked": 0, "problems": ["events must be a list"]}
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        return {"ok": False, "checked": len(events), "problems": ["invalid JWKS"]}
    bad: list[str] = []
    valid_seq_events: list[tuple[int, dict]] = []
    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            bad.append(f"index {idx}: event is not a JSON object")
            continue
        seq = ev.get("seq")
        if not _is_seq(seq):
            bad.append(f"index {idx}: missing or invalid seq")
            continue
        valid_seq_events.append((seq, ev))
    prev: dict | None = None
    prev_seq: int | None = None
    for seq, ev in sorted(valid_seq_events, key=lambda pair: pair[0]):
        claimed_hash = ev.get("hash")
        if not isinstance(claimed_hash, str) or not claimed_hash:
            bad.append(f"seq {seq}: missing or non-string hash")
        else:
            try:
                recomputed = event_hash(ev)
            except (ValueError, TypeError) as e:
                bad.append(f"seq {seq}: {e}")
            else:
                if recomputed != claimed_hash:
                    bad.append(f"seq {seq}: hash does not recompute")
        raw_sig = ev.get("signature")
        if not isinstance(raw_sig, str) or not raw_sig:
            bad.append(f"seq {seq}: missing or non-string signature")
        else:
            try:
                header, p = verify_jws(raw_sig, jwks)
                if not isinstance(claimed_hash, str) or not claimed_hash or p.get("hash") != claimed_hash or p.get("seq") != seq:
                    bad.append(f"seq {seq}: signature is for another event")
                if "kid" in ev and ev["kid"] is not None:
                    if not isinstance(ev["kid"], str) or header.get("kid") != ev["kid"]:
                        bad.append(f"seq {seq}: kid does not match the JWS header")
            except ValueError as e:
                bad.append(f"seq {seq}: {e}")
        if contiguous:
            if prev_seq is None:
                if seq != 1:
                    bad.append(f"seq {seq}: contiguous chain must start at seq 1")
                if ev.get("prev_hash") != GENESIS:
                    bad.append(f"seq {seq}: prev_hash does not link")
            else:
                if seq != prev_seq + 1:
                    bad.append(f"seq {seq}: non-contiguous seq after {prev_seq}")
                prev_claimed = prev.get("hash") if prev is not None else None
                if not isinstance(prev_claimed, str) or not prev_claimed or ev.get("prev_hash") != prev_claimed:
                    bad.append(f"seq {seq}: prev_hash does not link")
        prev = ev
        prev_seq = seq
    return {"ok": not bad, "checked": len(events), "problems": bad[:10]}
