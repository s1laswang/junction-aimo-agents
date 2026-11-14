"""Human-approver authentication (demo grade).

Agents must never be able to approve. In production, humans sign in with the customer's SSO (OIDC) and agents use
OAuth client credentials, so the two can never be confused. In this demo:

- a human signs in once with the approver credential (`X-Approver-Pin`) at `POST /v1/auth/approver-session` and
  gets a short-lived signed session token (`X-Approver-Session`, 8 h); credentialed routes accept either;
- the credential comes from env `AIMO_APPROVER_PIN`, or is generated once into `<data>/keys/approver_pin` (mode
  0600) as a random 12-character passphrase; no API route or MCP tool ever returns it;
- brute force is capped PER SOURCE: after MAX_FAILURES wrong credentials from one client address within WINDOW_S,
  that address is locked for LOCK_S (HTTP 429). A valid session token is never locked, so an attacker cannot lock
  the signed-in human approver out (no denial of service on approvals);
- any decision request that carries agent identity (`X-Agent-Id`) is refused outright;
- on top of this, aimo.trust.check_approval requires the approver to be a human in the order customer's directory
  with an allowed role, and binds the approval to the quote hash.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

from aimo.config import KEY_DIR

PIN_FILE = KEY_DIR / "approver_pin"
SECRET_FILE = KEY_DIR / "approver_session_secret"
MAX_FAILURES = int(os.environ.get("AIMO_APPROVER_MAX_FAILURES", "5"))
WINDOW_S = 600.0
LOCK_S = float(os.environ.get("AIMO_APPROVER_LOCK_S", "300"))
SESSION_S = 8 * 3600

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def _read_or_create(path, make) -> str:
    if path.exists():
        return path.read_text().strip()
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    val = make()
    path.write_text(val)
    os.chmod(path, 0o600)
    return val


def expected_pin() -> str:
    env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if env:
        return env
    return _read_or_create(PIN_FILE, lambda: secrets.token_urlsafe(9))  # 12 chars, ~72 bits


def _secret() -> bytes:
    # bound to the current credential, so changing the PIN invalidates every session
    base = _read_or_create(SECRET_FILE, lambda: secrets.token_hex(32))
    return hashlib.sha256((base + "|" + expected_pin()).encode()).digest()


def locked_for(source: str = "?") -> float:
    """Seconds until this source's credential lock lifts (0 = not locked)."""
    return max(0.0, _locked_until.get(source, 0.0) - time.time())


def check_pin(pin: str | None, source: str = "?") -> bool:
    """Constant-time check. Wrong credentials count toward this source's lockout; while locked, nothing is accepted."""
    if locked_for(source) > 0:
        return False
    if pin and hmac.compare_digest(str(pin).strip(), expected_pin()):
        return True
    if pin:  # a wrong guess (an absent credential is not a guess)
        now = time.time()
        with _lock:
            f = [t for t in _failures.get(source, []) if now - t < WINDOW_S] + [now]
            if len(f) >= MAX_FAILURES:
                _locked_until[source] = now + LOCK_S
                f = []
            _failures[source] = f
    return False


def issue_session(approver: str = "") -> dict:
    exp = int(time.time() + SESSION_S)
    payload = f"v1.{exp}.{approver}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return {"session": f"{payload}.{sig}", "expires_at": exp}


def check_session(token: str | None) -> bool:
    if not token or token.count(".") < 3:
        return False
    payload, sig = token.rsplit(".", 1)
    try:
        exp = int(payload.split(".")[1])
    except ValueError:
        return False
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, good) and exp > time.time()


def reset_for_tests() -> None:
    with _lock:
        _failures.clear()
        _locked_until.clear()


def local_pin() -> str:
    """For trusted local tooling on the same machine as the gateway (demo scripts, tests): the operator's PIN."""
    return expected_pin()
