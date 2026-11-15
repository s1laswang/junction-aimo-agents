"""Human-approver authentication with per-approver credentials and per-identity lockout.

Agents must never be able to approve. In production, humans sign in with the customer's SSO (OIDC) and agents use
OAuth client credentials, so the two can never be confused. In this demo:

- each canonical human approver in the customer policies gets a distinct 12-character passphrase stored in
  `<data>/keys/approver_pins.json` (mode 0600), overridable via `AIMO_APPROVER_PINS` (JSON object or `id=pin` CSV),
  with `AIMO_APPROVER_PIN` kept as an operator fallback for single-credential test and conformance harnesses;
- a human signs in with their credential (`X-Approver-Pin`) at `POST /v1/auth/approver-session` and receives an
  8-hour signed session token (`X-Approver-Session`) bound to that approver and their credential;
- brute force is capped per `(approver_id, source)` (MAX_FAILURES within WINDOW_S -> HTTP 429 for LOCK_S) plus a
  per-approver backstop (`GLOBAL_MAX` across all sources for that approver only), so attacking one approver never
  locks out other approvers or active `X-Approver-Session` tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from aimo import config

_ORIG_KEY_DIR = config.KEY_DIR
PIN_FILE = config.KEY_DIR / "approver_pin"
PINS_FILE = config.KEY_DIR / "approver_pins.json"
SECRET_FILE = config.KEY_DIR / "approver_session_secret"
MAX_FAILURES = int(os.environ.get("AIMO_APPROVER_MAX_FAILURES", "5"))
WINDOW_S = 600.0
LOCK_S = float(os.environ.get("AIMO_APPROVER_LOCK_S", "300"))
SESSION_S = 8 * 3600
GLOBAL_MAX = int(os.environ.get("AIMO_APPROVER_GLOBAL_MAX", "30"))
MAX_SOURCES = 2048

_lock = threading.Lock()
_failures: dict[tuple[str, str], list[float]] = {}
_locked_until: dict[tuple[str, str], float] = {}
_identity_failures: dict[str, list[float]] = {}
_identity_locked_until: dict[str, float] = {}


def _pin_path() -> Path:
    return config.KEY_DIR / "approver_pin" if config.KEY_DIR != _ORIG_KEY_DIR else PIN_FILE


def _pins_path() -> Path:
    return config.KEY_DIR / "approver_pins.json" if config.KEY_DIR != _ORIG_KEY_DIR else PINS_FILE


def _secret_path() -> Path:
    return config.KEY_DIR / "approver_session_secret" if config.KEY_DIR != _ORIG_KEY_DIR else SECRET_FILE


def _utf8(value: str) -> bytes:
    return value.encode("utf-8", errors="surrogatepass")


def _read_or_create(path: Path, make) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    val = make()
    path.write_text(val, encoding="utf-8")
    os.chmod(path, 0o600)
    return val


def _directory_approver_ids() -> list[str]:
    from aimo.gateway import service

    out: list[str] = []
    for c in service.list_customers():
        for a in c.get("approvers") or []:
            aid = str(a.get("id") or "").strip()
            if aid and aid not in out:
                out.append(aid)
    return out


def _canonical_approver_id(approver_id: str | None) -> str | None:
    if approver_id is None:
        return None
    raw = str(approver_id).strip()
    if not raw or len(raw) > 256 or any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in raw):
        return None
    from aimo.gateway import service

    resolved = service.directory_approver_id(raw)
    return resolved if resolved else raw.lower()


def _configured_approver_pins() -> dict[str, str]:
    raw = os.environ.get("AIMO_APPROVER_PINS", "").strip()
    if not raw:
        return {}
    entries: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                ks = _canonical_approver_id(str(k))
                vs = str(v).strip() if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
                if ks and vs:
                    entries[ks] = vs
            return entries
    for part in raw.split(","):
        item = part.strip()
        if ":" in item and "=" in item:
            sep = ":" if item.index(":") < item.index("=") else "="
        elif "=" in item:
            sep = "="
        elif ":" in item:
            sep = ":"
        else:
            continue
        k, v = item.split(sep, 1)
        ks = _canonical_approver_id(k)
        vs = v.strip()
        if ks and vs:
            entries[ks] = vs
    return entries


def _ensure_pins_file() -> dict[str, str]:
    path = _pins_path()
    pin_file = _pin_path()
    dir_ids = _directory_approver_ids()
    primary_id = (
        "anna@hotel-aurora.example"
        if ("anna@hotel-aurora.example" in dir_ids or not dir_ids)
        else dir_ids[0]
    )
    with _lock:
        data: dict[str, str] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = {
                        str(k).strip(): str(v).strip()
                        for k, v in loaded.items()
                        if str(k).strip() and v is not None and str(v).strip()
                    }
            except (OSError, ValueError):
                data = {}
        legacy_pin = ""
        if pin_file.exists():
            try:
                legacy_pin = pin_file.read_text(encoding="utf-8").strip()
            except OSError:
                legacy_pin = ""
        changed = not path.exists()
        if legacy_pin and not data.get(primary_id) and legacy_pin not in data.values():
            data[primary_id] = legacy_pin
            changed = True
        existing_vals = set(data.values())
        for aid in dir_ids:
            if not data.get(aid):
                cand = secrets.token_urlsafe(9)
                while cand in existing_vals:
                    cand = secrets.token_urlsafe(9)
                data[aid] = cand
                existing_vals.add(cand)
                changed = True
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        primary_val = data.get(primary_id)
        if primary_val and legacy_pin != primary_val:
            pin_file.parent.mkdir(parents=True, exist_ok=True)
            pin_file.write_text(primary_val, encoding="utf-8")
            os.chmod(pin_file, 0o600)
        return data


def approver_pins() -> dict[str, str]:
    pins = dict(_ensure_pins_file())
    pins.update(_configured_approver_pins())
    return pins


def expected_pin() -> str:
    env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if env:
        return env
    pins = _ensure_pins_file()
    if "anna@hotel-aurora.example" in pins:
        return pins["anna@hotel-aurora.example"]
    if pins:
        return next(iter(pins.values()))
    return _read_or_create(_pin_path(), lambda: secrets.token_urlsafe(9))


def expected_pin_for(approver_id: str) -> str | None:
    cid = _canonical_approver_id(approver_id)
    if not cid:
        return None
    overrides = _configured_approver_pins()
    if cid in overrides:
        return overrides[cid]
    single_env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if single_env:
        return single_env
    pins = _ensure_pins_file()
    return pins.get(cid)


def _secret_for(approver_id: str) -> bytes:
    cid = _canonical_approver_id(approver_id) or str(approver_id or "").strip()
    base = _read_or_create(_secret_path(), lambda: secrets.token_hex(32))
    pin = expected_pin_for(cid) or ""
    return hashlib.sha256(_utf8(f"{base}|{cid}|{pin}")).digest()


def credential_warning() -> str | None:
    single_env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if single_env and len(single_env) < 12:
        return (
            f"AIMO_APPROVER_PIN is only {len(single_env)} characters; use a 12+ character passphrase "
            "(demo only; production uses SSO)"
        )
    for aid, apin in _configured_approver_pins().items():
        if len(apin) < 12:
            return (
                f"AIMO_APPROVER_PINS entry for {aid} is only {len(apin)} characters; use a 12+ character passphrase "
                "(demo only; production uses SSO)"
            )
    pins = _ensure_pins_file()
    for aid, apin in pins.items():
        if len(apin) < 12:
            return (
                f"approver passphrase for {aid} is only {len(apin)} characters; use a 12+ character passphrase "
                "(demo only; production uses SSO)"
            )
    return None


def _lock_key_id(approver_id: str | None) -> str:
    cid = _canonical_approver_id(approver_id)
    return cid if cid else "*"


def locked_for(source: str = "?", approver_id: str | None = None) -> float:
    now = time.time()
    id_key = _lock_key_id(approver_id)
    return max(
        0.0,
        _locked_until.get((id_key, source), 0.0) - now,
        _identity_locked_until.get(id_key, 0.0) - now,
    )


def _record_failure(id_key: str, source: str) -> None:
    now = time.time()
    pair = (id_key, source)
    with _lock:
        if pair not in _failures and len(_failures) >= MAX_SOURCES:
            stale = min(_failures, key=lambda k: (_failures[k][-1] if _failures[k] else 0.0))
            _failures.pop(stale, None)
            _locked_until.pop(stale, None)
        f = [t for t in _failures.get(pair, []) if now - t < WINDOW_S] + [now]
        if len(f) >= MAX_FAILURES:
            _locked_until[pair] = now + LOCK_S
            f = []
        _failures[pair] = f
        if id_key not in _identity_failures and len(_identity_failures) >= MAX_SOURCES:
            stale_id = min(
                _identity_failures,
                key=lambda k: (_identity_failures[k][-1] if _identity_failures[k] else 0.0),
            )
            _identity_failures.pop(stale_id, None)
            _identity_locked_until.pop(stale_id, None)
        gf = [t for t in _identity_failures.get(id_key, []) if now - t < WINDOW_S] + [now]
        if len(gf) >= GLOBAL_MAX:
            _identity_locked_until[id_key] = now + LOCK_S
            gf = []
        _identity_failures[id_key] = gf


def check_pin_for(pin: str | None, approver_id: str | None, source: str = "?") -> bool:
    if approver_id is None or not str(approver_id).strip():
        return check_pin(pin, source)
    if locked_for(source, approver_id) > 0:
        return False
    provided = str(pin).strip(" \t\r\n") if pin is not None else ""
    if not provided:
        return False
    if len(provided) > 1024 or any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in provided):
        hmac.compare_digest(_utf8(provided[:256]), b"invalid-approver-credential")
        _record_failure(_lock_key_id(approver_id), source)
        return False
    provided_b = _utf8(provided)
    exp = expected_pin_for(approver_id)
    if exp is not None and hmac.compare_digest(provided_b, _utf8(exp)):
        return True
    hmac.compare_digest(provided_b, b"invalid-approver-credential")
    _record_failure(_lock_key_id(approver_id), source)
    return False


def check_pin(pin: str | None, source: str = "?") -> bool:
    if locked_for(source, None) > 0:
        return False
    provided = str(pin).strip(" \t\r\n") if pin is not None else ""
    if not provided:
        return False
    if len(provided) > 1024 or any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in provided):
        hmac.compare_digest(_utf8(provided[:256]), b"invalid-approver-credential")
        _record_failure("*", source)
        return False
    provided_b = _utf8(provided)
    matched = False
    single_env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if single_env:
        if hmac.compare_digest(provided_b, _utf8(single_env)):
            matched = True
    elif _pin_path().exists():
        legacy = _pin_path().read_text(encoding="utf-8", errors="replace").strip()
        if legacy and hmac.compare_digest(provided_b, _utf8(legacy)):
            matched = True
    for aid, apin in approver_pins().items():
        if locked_for(source, aid) <= 0 and hmac.compare_digest(provided_b, _utf8(apin)):
            matched = True
    if matched:
        return True
    _record_failure("*", source)
    return False


def issue_session(approver: str) -> dict:
    canonical = _canonical_approver_id(approver) or str(approver or "").strip()
    exp = int(time.time() + SESSION_S)
    payload = f"v2.{exp}.{canonical}"
    sig = hmac.new(_secret_for(canonical), _utf8(payload), hashlib.sha256).hexdigest()[:32]
    return {"session": f"{payload}.{sig}", "approver": canonical, "expires_at": exp}


def session_approver(token: str | None) -> str | None:
    if not isinstance(token, str):
        return None
    cleaned = token.strip(" \t\r\n")
    if not cleaned or len(cleaned) > 1024 or cleaned.count(".") < 3:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in cleaned):
        return None
    payload, sig = cleaned.rsplit(".", 1)
    if not sig:
        return None
    parts = payload.split(".", 2)
    if len(parts) != 3 or parts[0] != "v2":
        return None
    if not (parts[1].isascii() and parts[1].isdigit() and len(parts[1]) <= 20):
        return None
    exp = int(parts[1])
    who = parts[2].strip()
    if not who or exp <= time.time():
        return None
    good = hmac.new(_secret_for(who), _utf8(payload), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(_utf8(sig), _utf8(good)):
        return None
    return who


def check_session(token: str | None) -> bool:
    return session_approver(token) is not None


def reset_for_tests() -> None:
    with _lock:
        _failures.clear()
        _locked_until.clear()
        _identity_failures.clear()
        _identity_locked_until.clear()


def local_pin(approver_id: str | None = None) -> str:
    if approver_id:
        p = expected_pin_for(approver_id)
        if p:
            return p
    single_env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if single_env:
        return single_env
    default_approver = expected_pin_for("anna@hotel-aurora.example")
    if default_approver:
        return default_approver
    return expected_pin()
