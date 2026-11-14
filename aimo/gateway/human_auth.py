"""Human-approver authentication (demo grade).

Agents must never be able to approve. In production, humans sign in with the customer's SSO (OIDC) and agents use
OAuth client credentials, so the two can never be confused. In this demo:

- every approval decision (and the demo tamper routes) needs the header `X-Approver-Pin`;
- the PIN comes from env `AIMO_APPROVER_PIN`, or is generated once into `<data>/keys/approver_pin` (mode 0600);
- no API route or MCP tool ever returns the PIN, so a remote agent cannot obtain it;
- any decision request that carries agent identity (`X-Agent-Id`) is refused outright;
- on top of this, aimo.trust.check_approval requires the approver to be a human in the order customer's directory
  with an allowed role, and binds the approval to the quote hash.
"""
from __future__ import annotations

import hmac
import os
import secrets

from aimo.config import KEY_DIR

PIN_FILE = KEY_DIR / "approver_pin"


def expected_pin() -> str:
    env = os.environ.get("AIMO_APPROVER_PIN", "").strip()
    if env:
        return env
    if PIN_FILE.exists():
        return PIN_FILE.read_text().strip()
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    pin = f"{secrets.randbelow(10**6):06d}"
    PIN_FILE.write_text(pin)
    os.chmod(PIN_FILE, 0o600)
    return pin


def check_pin(pin: str | None) -> bool:
    return bool(pin) and hmac.compare_digest(str(pin).strip(), expected_pin())


def local_pin() -> str:
    """For trusted local tooling on the same machine as the gateway (demo scripts, tests): the operator's PIN."""
    return expected_pin()
