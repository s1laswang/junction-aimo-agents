"""Simulated clock for demo resilience. Owner: gateway.

The demo depends on wall-clock facts: Stock products close for Monday delivery at Sun 15 Nov 17:00,
Terminal products closed Thu 12 Nov 15:00, and quotes expire after 15 minutes. To demo at any time:

  AIMO_NOW=2026-11-14T14:00:00+02:00   anchor; the clock then runs forward in real time from server start
  AIMO_NOW_FROZEN=1                    keep the clock fixed at AIMO_NOW
  X-Aimo-Now: <ISO datetime>           per-request fixed "now" (demo mode only, ignored when AIMO_ENV=production)
  AIMO_REPLAY=<dir>                    replay mode anchors the clock at the snapshot's recorded timeNow
                                       unless AIMO_NOW is set (AIMO_REPLAY_CLOCK=real keeps the real clock)

Everything time-sensitive in the gateway (cut-off evaluation, quote created/expires, approval expiry,
policy freshness via trust's `now=`) reads `now()`. Other components can use `aimo.backend.now()`
(e.g. the buyer resolving "by Monday").
"""
from __future__ import annotations

import contextvars
import os
import time
from datetime import datetime, timedelta, timezone

_START = time.time()
_ctx: contextvars.ContextVar[datetime | None] = contextvars.ContextVar("aimo_now", default=None)


def demo_enabled() -> bool:
    return os.environ.get("AIMO_ENV", "dev").strip().lower() not in ("prod", "production")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    from aimo.backend.client import parse_dt

    dt = parse_dt(value.strip())
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def set_request_now(value: str | None) -> contextvars.Token:
    """Per-request fixed now (header X-Aimo-Now). Ignored outside demo mode or when unparsable."""
    dt = parse(value) if (value and demo_enabled()) else None
    return _ctx.set(dt)


def reset_request_now(token: contextvars.Token) -> None:
    _ctx.reset(token)


def _anchor() -> tuple[datetime | None, str]:
    a = parse(os.environ.get("AIMO_NOW"))
    if a is not None:
        return a, "env"
    if os.environ.get("AIMO_REPLAY_CLOCK", "snapshot") != "real":
        from aimo.backend import replay

        d = replay.replay_dir()
        if d is not None:
            t = replay.snapshot_time_now(d)
            if t is not None:
                return t, "replay"
    return None, "real"


def now() -> datetime:
    """Timezone-aware UTC."""
    return _now_any().astimezone(timezone.utc)


def _now_any() -> datetime:
    fixed = _ctx.get()
    if fixed is not None:
        return fixed
    a, _ = _anchor()
    if a is None:
        return datetime.now(timezone.utc)
    if os.environ.get("AIMO_NOW_FROZEN", "0") == "1":
        return a
    from datetime import timedelta

    return a + timedelta(seconds=time.time() - _START)


def simulated() -> bool:
    return _ctx.get() is not None or _anchor()[0] is not None


def describe() -> dict:
    mode = "header" if _ctx.get() is not None else _anchor()[1]
    return {"mode": mode, "now": now().isoformat(), "frozen": os.environ.get("AIMO_NOW_FROZEN", "0") == "1"}


# ---------------------------------------------------------------- delivery dates (S1 replay)
# Delivery dates derive from the (simulated) clock, never from a hard-coded date: scenarios, demo and docs
# scripts call next_delivery_day() (or ask the server's /healthz clock), so the demo still works after the
# Sun 15 Nov 17:00 Stock cut-off (live) and on any real day in replay (clock anchored at the snapshot).
HELSINKI_TZ = timezone(timedelta(hours=2))  # November: EET, no DST
STOCK_CUTOFF_HOUR = 17  # Stock products close at 17:00 Helsinki the day before delivery (observed 14 Nov)


def today_local(at: datetime | None = None):
    """Today's date in Helsinki on the simulated clock."""
    return (at or now()).astimezone(HELSINKI_TZ).date()


def next_delivery_day(at: datetime | None = None, min_days: int = 1, weekday: int | None = None):
    """The next feasible delivery date: a weekday (optionally a given weekday, 0=Mon) at least `min_days`
    after today whose Stock cut-off (the day before at 17:00 Helsinki) is still ahead of `at` (default now())."""
    from datetime import time as _time

    n = (at or now()).astimezone(HELSINKI_TZ)
    d = n.date() + timedelta(days=max(1, int(min_days)))
    for _ in range(21):
        cutoff = datetime.combine(d - timedelta(days=1), _time(STOCK_CUTOFF_HOUR), HELSINKI_TZ)
        if d.weekday() < 5 and (weekday is None or d.weekday() == weekday) and n < cutoff:
            return d
        d += timedelta(days=1)
    return d
