"""Offline verifier for a Trusted Trade quote, an audit evidence pack or a list of audit events (GET /v1/audit).
Needs only `cryptography`. Keys come ONLY from the JWKS you pass (never from the document being checked).
Usage: python scripts/verify_quote.py <quote.json | evidence.json | events.json> <jwks.json | https://host/.well-known/jwks.json>"""
import base64, hashlib, json, sys, urllib.request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

b64d = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
norm = lambda o: {k: norm(v) for k, v in o.items()} if isinstance(o, dict) else [norm(v) for v in o] if isinstance(o, list) \
    else int(o) if isinstance(o, float) and o.is_integer() else o
canon = lambda o: json.dumps(norm(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()

def jws(token, keys):  # -> payload, or {} for a bad signature, unknown kid or alg other than EdDSA
    try:
        h, p, s = token.split("."); hd = json.loads(b64d(h)); assert hd["alg"] == "EdDSA"
        Ed25519PublicKey.from_public_bytes(b64d(keys[hd["kid"]]["x"])).verify(b64d(s), f"{h}.{p}".encode()); return json.loads(b64d(p))
    except Exception: return {}

doc, src = json.load(open(sys.argv[1])), sys.argv[2]
doc = {"events": doc} if isinstance(doc, list) else doc
jwks = json.load(urllib.request.urlopen(src) if src.startswith("http") else open(src))
keys = {k["kid"]: k for k in jwks["keys"] if k.get("kty") == "OKP" and k.get("crv") == "Ed25519"}
ok = True
quote = doc.get("quote") if "events" in doc else doc
if quote:
    h = sha(canon({k: v for k, v in quote.items() if k not in ("quote_hash", "signature", "kid")}))
    good = h == quote["quote_hash"] and jws(quote["signature"], keys).get("quote_hash") == h
    print(f"quote {quote['quote_id']}: hash {'OK' if h == quote['quote_hash'] else 'MISMATCH'}, signature {'OK' if good else 'BAD'}"); ok &= good
for e in doc.get("events", []):
    h = sha(e["prev_hash"] + canon({k: v for k, v in e.items() if k not in ("hash", "signature", "kid")}))
    good = h == e["hash"] and jws(e["signature"], keys).get("hash") == h; ok &= good
    print(f"event {e['seq']:>4} {e['action']:<24} {'OK' if good else 'BAD'}")
print("VERIFIED" if ok else "FAILED"); sys.exit(0 if ok else 1)
