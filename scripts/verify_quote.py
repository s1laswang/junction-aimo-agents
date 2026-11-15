"""Offline verifier for a Trusted Trade quote, an audit evidence pack or a list of audit events (GET /v1/audit).
Needs only `cryptography`. Keys come ONLY from the JWKS you pass (never from the document being checked).
Usage: python scripts/verify_quote.py <quote.json | evidence.json | events.json> <jwks.json | https://host/.well-known/jwks.json>"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_HEX = frozenset("0123456789abcdef")


def _has_ctrl(s: str) -> bool:
    return any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in s)


def _reject_const(c: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {c}")


def _loads(raw: str | bytes) -> Any:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text, parse_constant=_reject_const)


def b64d(s: Any) -> bytes:
    if not isinstance(s, str) or not s or not all(c.isalnum() or c in "-_=" for c in s):
        raise ValueError("invalid base64url input")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def norm(o: Any) -> Any:
    if isinstance(o, dict):
        out: dict[str, Any] = {}
        for k, v in o.items():
            if not isinstance(k, str):
                raise ValueError("JSON object keys must be strings")
            out[k] = norm(v)
        return out
    if isinstance(o, list):
        return [norm(v) for v in o]
    if isinstance(o, bool):
        return o
    if isinstance(o, float):
        if not math.isfinite(o):
            raise ValueError("non-finite float")
        return int(o) if o.is_integer() else o
    return o


def canon(o: Any) -> str:
    return json.dumps(norm(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def jws(token: Any, keys: dict[str, dict[str, Any]]) -> dict[str, Any]:
    try:
        if not isinstance(token, str) or token.count(".") != 2:
            return {}
        h, p, s = token.split(".")
        hd = _loads(b64d(h))
        if not isinstance(hd, dict) or hd.get("alg") != "EdDSA":
            return {}
        kid = hd.get("kid")
        if not isinstance(kid, str) or kid not in keys:
            return {}
        pub = b64d(keys[kid].get("x"))
        sig = b64d(s)
        if len(pub) != 32 or len(sig) != 64:
            return {}
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, f"{h}.{p}".encode("ascii"))
        payload = _loads(b64d(p))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _load_source(src: str, *, allow_http: bool) -> Any:
    if not isinstance(src, str) or not src.strip() or _has_ctrl(src):
        raise ValueError("path or URL is empty or invalid")
    if allow_http and src.startswith(("http://", "https://")):
        if any(c.isspace() for c in src):
            raise ValueError("JWKS URL contains whitespace")
        parsed = urlparse(src)
        host = parsed.hostname
        if not parsed.netloc or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError("JWKS URL is invalid")
        if ".." in host or host.startswith("."):
            raise ValueError("JWKS URL host is invalid")
        port = parsed.port
        if port is not None and (port < 1 or port > 65535):
            raise ValueError("JWKS URL port out of range")
        with urllib.request.urlopen(src, timeout=15) as resp:
            return _loads(resp.read(1_048_576))
    p = Path(src)
    if not p.is_file():
        raise ValueError(f"file not found: {src}")
    return _loads(p.read_text(encoding="utf-8"))


def _extract_keys(jwks: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise ValueError("JWKS must be a JSON object with a 'keys' list")
    out: dict[str, dict[str, Any]] = {}
    for k in jwks["keys"]:
        if (
            isinstance(k, dict)
            and k.get("kty") == "OKP"
            and k.get("crv") == "Ed25519"
            and isinstance(k.get("kid"), str)
            and k["kid"].strip()
            and isinstance(k.get("x"), str)
            and k["x"].strip()
        ):
            out[k["kid"]] = k
    return out


def verify_document(doc: Any, jwks: Any) -> tuple[bool, list[str]]:
    lines: list[str] = []
    keys = _extract_keys(jwks)
    if isinstance(doc, list):
        doc = {"events": doc}
    if not isinstance(doc, dict):
        return False, lines
    has_events = "events" in doc
    if has_events:
        raw_events = doc.get("events")
        if not isinstance(raw_events, list):
            return False, lines
        has_quote_key = "quote" in doc and doc.get("quote") is not None
        quote = doc.get("quote")
        if has_quote_key and (not isinstance(quote, dict) or not quote):
            return False, lines
        if not has_quote_key and not raw_events:
            return False, lines
    else:
        raw_events = []
        has_quote_key = True
        quote = doc
        if not quote:
            return False, lines

    ok = True
    if has_quote_key and isinstance(quote, dict):
        qid = quote.get("quote_id")
        valid_qid = isinstance(qid, str) and bool(qid.strip()) and not _has_ctrl(qid)
        try:
            h = sha(canon({k: v for k, v in quote.items() if k not in ("quote_hash", "signature", "kid")}))
        except ValueError:
            h = ""
        qh = quote.get("quote_hash")
        hash_ok = bool(h) and isinstance(qh, str) and h == qh
        pl = jws(quote.get("signature"), keys)
        good = valid_qid and hash_ok and pl.get("quote_hash") == h
        disp_id = qid if valid_qid else "<invalid>"
        lines.append(f"quote {disp_id}: hash {'OK' if hash_ok else 'MISMATCH'}, signature {'OK' if good else 'BAD'}")
        ok &= good

    prev_seq: int | None = None
    prev_hash_out: str | None = None
    for e in raw_events:
        if not isinstance(e, dict):
            lines.append(f"event {'?':>4} {'<invalid>':<24} BAD")
            ok = False
            continue
        seq = e.get("seq")
        act = e.get("action")
        ph = e.get("prev_hash")
        eh = e.get("hash")
        valid_seq = isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1
        valid_act = isinstance(act, str) and bool(act.strip()) and not _has_ctrl(act)
        valid_ph = isinstance(ph, str) and len(ph) == 64 and all(c in _HEX for c in ph)
        valid_eh = isinstance(eh, str) and len(eh) == 64 and all(c in _HEX for c in eh)
        chain_ok = True
        if valid_seq and valid_ph:
            if seq == 1 and ph != "0" * 64:
                chain_ok = False
            if prev_seq is not None:
                if seq <= prev_seq:
                    chain_ok = False
                elif seq == prev_seq + 1 and prev_hash_out is not None and ph != prev_hash_out:
                    chain_ok = False
        try:
            h = sha(ph + canon({k: v for k, v in e.items() if k not in ("hash", "signature", "kid")})) if valid_ph else ""
        except ValueError:
            h = ""
        pl = jws(e.get("signature"), keys)
        pl_seq_ok = ("seq" not in pl) or (
            isinstance(pl.get("seq"), int) and not isinstance(pl.get("seq"), bool) and pl.get("seq") == seq
        )
        good = valid_seq and valid_act and valid_ph and valid_eh and chain_ok and bool(h) and h == eh and pl.get("hash") == h and pl_seq_ok
        if valid_seq:
            prev_seq = seq
            prev_hash_out = eh if valid_eh else None
        seq_txt = str(seq) if valid_seq else "?"
        act_txt = act if valid_act else "<invalid>"
        lines.append(f"event {seq_txt:>4} {act_txt:<24} {'OK' if good else 'BAD'}")
        ok &= good

    return ok, lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not isinstance(args, (list, tuple)) or isinstance(args, (str, bytes, bytearray, bool)) or len(args) != 2:
        print("usage: python scripts/verify_quote.py <doc.json> <jwks.json|https://...>", file=sys.stderr)
        print("FAILED")
        return 2
    doc_arg, jwks_arg = args[0], args[1]
    if not isinstance(doc_arg, str) or not isinstance(jwks_arg, str):
        print("error: arguments must be strings", file=sys.stderr)
        print("FAILED")
        return 2
    try:
        doc = _load_source(doc_arg, allow_http=False)
        jwks = _load_source(jwks_arg, allow_http=True)
        ok, lines = verify_document(doc, jwks)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("FAILED")
        return 1
    for line in lines:
        print(line)
    print("VERIFIED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
