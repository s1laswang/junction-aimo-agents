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


GLOBAL_MAX = int(os.environ.get("AIMO_APPROVER_GLOBAL_MAX", "30"))   # wrong guesses / window, all sources together
MAX_SOURCES = 2048                                                    # cap on tracked client addresses (memory bound)
_global_failures: list[float] = []
_global_locked_until = 0.0


def credential_warning() -> str | None:
    pin = expected_pin()
    if len(pin) < 12:
        return (f"AIMO_APPROVER_PIN is only {len(pin)} characters; use a 12+ character passphrase "
                "(demo only; production uses SSO)")
    return None


def locked_for(source: str = "?") -> float:
    """Seconds until PIN checks from this source are accepted again (per-source lock or global backstop)."""
    now = time.time()
    return max(0.0, _locked_until.get(source, 0.0) - now, _global_locked_until - now)


def check_pin(pin: str | None, source: str = "?") -> bool:
    """Constant-time check. Wrong credentials count toward this source's lockout and a global backstop (which
    only blocks PIN checks: signed-in approver sessions are never affected); while locked nothing is accepted."""
    global _global_locked_until
    if locked_for(source) > 0:
        return False
    if pin and hmac.compare_digest(str(pin).strip(), expected_pin()):
        return True
    if pin:  # a wrong guess (an absent credential is not a guess)
        now = time.time()
        with _lock:
            if source not in _failures and len(_failures) >= MAX_SOURCES:  # bounded memory: drop the stalest source
                stale = min(_failures, key=lambda s: (_failures[s][-1] if _failures[s] else 0.0))
                _failures.pop(stale, None)
                _locked_until.pop(stale, None)
            f = [t for t in _failures.get(source, []) if now - t < WINDOW_S] + [now]
            if len(f) >= MAX_FAILURES:
                _locked_until[source] = now + LOCK_S
                f = []
            _failures[source] = f
            _global_failures[:] = [t for t in _global_failures if now - t < WINDOW_S] + [now]
            if len(_global_failures) >= GLOBAL_MAX:
                _global_locked_until = now + LOCK_S
                _global_failures.clear()
    return False


def issue_session(approver: str) -> dict:
    """Signed 8-hour session bound to ONE named human approver (directory id)."""
    exp = int(time.time() + SESSION_S)
    payload = f"v2.{exp}.{approver}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return {"session": f"{payload}.{sig}", "approver": approver, "expires_at": exp}


def session_approver(token: str | None) -> str | None:
    """The approver id a valid session is bound to, else None."""
    if not token or token.count(".") < 3:
        return None
    payload, sig = token.rsplit(".", 1)
    parts = payload.split(".", 2)
    if len(parts) != 3 or parts[0] != "v2":
        return None
    try:
        exp = int(parts[1])
    except ValueError:
        return None
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good) or exp <= time.time() or not parts[2]:
        return None
    return parts[2]


def check_session(token: str | None) -> bool:
    return session_approver(token) is not None


def reset_for_tests() -> None:
    global _global_locked_until
    with _lock:
        _failures.clear()
        _locked_until.clear()
        _global_failures.clear()
        _global_locked_until = 0.0


def local_pin() -> str:
    """For trusted local tooling on the same machine as the gateway (demo scripts, tests): the operator's PIN."""
    return expected_pin()
