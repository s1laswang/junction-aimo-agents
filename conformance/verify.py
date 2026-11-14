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
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

QUOTE_EXCLUDE = ("quote_hash", "signature", "kid")
EVENT_EXCLUDE = ("hash", "signature", "kid")
GENESIS = "0" * 64


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _norm(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _norm(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_norm(v) for v in o]
    if isinstance(o, float) and o.is_integer() and abs(o) < 2**53:
        return int(o)
    return o


def canonical_json(o: Any) -> str:
    return json.dumps(_norm(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def thumbprint(jwk: dict) -> str:
    """RFC 7638 for OKP: members crv, kty, x in lexicographic order."""
    return b64e(hashlib.sha256(canonical_json({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}).encode()).digest())


def key_for(jwks: dict, kid: str) -> Ed25519PublicKey | None:
    for k in jwks.get("keys", []):
        if k.get("kid") == kid and k.get("kty") == "OKP" and k.get("crv") == "Ed25519":
            return Ed25519PublicKey.from_public_bytes(b64d(k["x"]))
    return None


def verify_jws(token: str, jwks: dict) -> tuple[dict, dict]:
    """Returns (header, payload); raises ValueError. Refuses alg other than EdDSA (incl. "none")."""
    try:
        h64, p64, s64 = token.split(".")
        header, payload = json.loads(b64d(h64)), json.loads(b64d(p64))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"malformed JWS: {e}") from e
    if header.get("alg") != "EdDSA":
        raise ValueError(f"alg {header.get('alg')!r} refused")
    pub = key_for(jwks, header.get("kid", ""))
    if pub is None:
        raise ValueError(f"unknown kid {header.get('kid')!r}")
    try:
        pub.verify(b64d(s64), f"{h64}.{p64}".encode("ascii"))
    except InvalidSignature as e:
        raise ValueError("signature does not verify") from e
    return header, payload


def quote_hash(quote: dict) -> str:
    return sha256_hex(canonical_json({k: v for k, v in quote.items() if k not in QUOTE_EXCLUDE}))


def verify_quote(quote: dict, jwks: dict) -> dict:
    """{ok, hash_matches, signature_valid, reasons}: hash binding + EdDSA signature over the hash."""
    reasons, sig_ok = [], False
    h = quote_hash(quote)
    hash_ok = h == quote.get("quote_hash")
    if not hash_ok:
        reasons.append("quote content does not match quote_hash")
    try:
        header, payload = verify_jws(quote.get("signature") or "", jwks)
        sig_ok = payload.get("quote_hash") == h and payload.get("quote_id") == quote.get("quote_id")
        if not sig_ok:
            reasons.append("JWS payload is for a different quote_hash / quote_id")
        if quote.get("kid") and header.get("kid") != quote["kid"]:
            sig_ok = False
            reasons.append("kid does not match the JWS header")
    except ValueError as e:
        reasons.append(str(e))
    return {"ok": hash_ok and sig_ok, "hash_matches": hash_ok, "signature_valid": sig_ok, "quote_hash": h,
            "reasons": reasons}


def event_hash(ev: dict) -> str:
    return sha256_hex(ev["prev_hash"] + canonical_json({k: v for k, v in ev.items() if k not in EVENT_EXCLUDE}))


def verify_events(events: list[dict], jwks: dict, contiguous: bool = False) -> dict:
    """Each event: hash recomputes and its JWS (payload {typ, seq, hash}) verifies. With contiguous=True
    (the full chain), prev_hash must link to the previous event and the first to GENESIS."""
    bad: list[str] = []
    prev = None
    for ev in sorted(events, key=lambda e: e["seq"]):
        if event_hash(ev) != ev["hash"]:
            bad.append(f"seq {ev['seq']}: hash does not recompute")
        try:
            _, p = verify_jws(ev["signature"], jwks)
            if p.get("hash") != ev["hash"] or p.get("seq") != ev["seq"]:
                bad.append(f"seq {ev['seq']}: signature is for another event")
        except ValueError as e:
            bad.append(f"seq {ev['seq']}: {e}")
        if contiguous:
            want = GENESIS if prev is None else prev["hash"]
            if (prev is None and ev["seq"] == 1 or prev is not None) and ev["prev_hash"] != want:
                bad.append(f"seq {ev['seq']}: prev_hash does not link")
        prev = ev
    return {"ok": not bad, "checked": len(events), "problems": bad[:10]}
