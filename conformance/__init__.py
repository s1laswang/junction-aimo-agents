"""Trusted Trade profile conformance suite: `python -m conformance --base <url>` checks any wholesaler gateway
that implements the profile (conformance/profile.py, docs/OPENNESS.md) and prints a PASS/FAIL table + JSON."""
from conformance.profile import PROFILE

__all__ = ["PROFILE"]
