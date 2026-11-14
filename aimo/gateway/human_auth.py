"""Human-approver authentication (demo grade).

Agents must never be able to approve. In production, humans sign in with the customer's SSO (OIDC) and agents use
OAuth client credentials, so the two can never be confused. In this demo:

- every approval decision (and the demo tamper routes) needs the header `X-Approver-Pin`;
- the credential comes from env `AIMO_APPROVER_PIN`, or is generated once into `<data>/keys/approver_pin` (mode
  0600) as a random 12-character passphrase;
- no API route or MCP tool ever returns it, so a remote agent cannot obtain it;
- brute force is capped: after MAX_FAILURES wrong credentials within WINDOW_S, every credentialed route is locked
  for LOCK_S seconds (HTTP 429), and only the first refusals and the lock itself are audited;
- any decision request that carries agent identity (`X-Agent-Id`) is refused outright;
- on top of this, aimo.trust.check_approval requires the approver to be a human in the order customer's directory
  with an allowed role, and binds the approval to the quote hash.
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time

from aimo.config import KEY_DIR

PIN_FILE = KEY_DIR / "approver_pin"
MAX_FAILURES = int(os.environ.get("AIMO_APPROVER_MAX_FAILURES", "5"))
WINDOW_S = 600.0
LOCK_S = float(os.environ.get("AIMO_APPROVER_LOCK_S", "300"))

_lock = threading.Lock()
_failures: list[float] = []
_locked_until = 0.0


def expected_pin() -> str:
    env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if env:
        return env
    if PIN_FILE.exists():
        return PIN_FILE.read_text().strip()
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    pin = secrets.token_urlsafe(9)  # 12 chars, ~72 bits
    PIN_FILE.write_text(pin)
    os.chmod(PIN_FILE, 0o600)
    return pin


def locked_for() -> float:
    """Seconds until the credential lock lifts (0 = not locked)."""
    return max(0.0, _locked_until - time.time())


def check_pin(pin: str | None) -> bool:
    """Constant-time check. Wrong credentials count toward the lockout; while locked, nothing is accepted."""
    global _locked_until
    if locked_for() > 0:
        return False
    if pin and hmac.compare_digest(str(pin).strip(), expected_pin()):
        return True
    if pin:  # an attempt with a wrong credential (absent credential is not a guess)
        now = time.time()
        with _lock:
            _failures[:] = [t for t in _failures if now - t < WINDOW_S] + [now]
            if len(_failures) >= MAX_FAILURES:
                _locked_until = now + LOCK_S
                _failures.clear()
    return False


def failures_in_window() -> int:
    now = time.time()
    return sum(1 for t in _failures if now - t < WINDOW_S)


def reset_for_tests() -> None:
    global _locked_until
    with _lock:
        _failures.clear()
        _locked_until = 0.0


def local_pin() -> str:
    """For trusted local tooling on the same machine as the gateway (demo scripts, tests): the operator's PIN."""
    return expected_pin()
