import json
import os
import sys
import warnings
from pathlib import Path

from joserfc.jwk import OKPKey

warnings.filterwarnings("ignore", message="EdDSA is deprecated")

_cache = {}


def _path():
    return Path(os.environ.get("AIMO_KEY_DIR", "var/keys")) / "quote-ed25519.json"


def signing_key():
    p = _path()
    if p not in _cache:
        if p.exists():
            key = OKPKey.import_key(json.loads(p.read_text()))
        else:
            key = OKPKey.generate_key("Ed25519")
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(key.as_dict(private=True), f)
        _cache[p] = key
    return _cache[p]


def kid():
    return signing_key().thumbprint()


def public_jwk():
    return {**signing_key().as_dict(private=False), "kid": kid(), "alg": "EdDSA", "use": "sig"}


def _retired():
    return [json.loads(f.read_text()) for f in sorted(_path().parent.glob("retired-*.json"))]


def jwks():
    return {"keys": [public_jwk()] + _retired()}


def rotate():
    old = public_jwk()
    p = _path()
    (p.parent / f"retired-{old['kid']}.json").write_text(json.dumps(old))
    key = OKPKey.generate_key("Ed25519")
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(key.as_dict(private=True), f)
    os.replace(tmp, p)
    _cache[p] = key
    return kid()


if __name__ == "__main__":
    if sys.argv[1:] == ["rotate"]:
        print("new kid", rotate())
    else:
        print("usage: python -m aimo.keys rotate")
