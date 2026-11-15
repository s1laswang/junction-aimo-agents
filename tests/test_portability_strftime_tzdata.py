from __future__ import annotations

import re
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

from aimo.trust import policy
from aimo.trust.audit import AuditLog

REPO_ROOT = Path(__file__).resolve().parent.parent
NON_PORTABLE_STRFTIME = re.compile(r"%[-#][a-zA-Z]")


def test_no_non_portable_strftime_directives_in_repo():
    offenders: list[str] = []
    skip_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules"}
    this_file = Path(__file__).resolve()
    for py_file in sorted(REPO_ROOT.rglob("*.py")):
        if any(part in skip_dirs for part in py_file.parts):
            continue
        if py_file.resolve() == this_file:
            continue
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if NON_PORTABLE_STRFTIME.search(line):
                offenders.append(f"{py_file.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == []


def test_policy_day_text_formats_single_and_double_digit_days():
    assert policy.day_text(date(2026, 11, 7)) == "Sat 7 Nov"
    assert policy.day_text(date(2026, 11, 16)) == "Mon 16 Nov"


def test_audit_render_formats_helsinki_date_portably(tmp_path, monkeypatch):
    from aimo import config
    from aimo.trust.audit import _local_date

    assert _local_date(datetime(2026, 11, 7, 10, 5, 0, tzinfo=timezone.utc)) == "Sat 7 Nov 2026"
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    for k in ("AIMO_NOW", "AIMO_NOW_FROZEN", "AIMO_REPLAY", "AIMO_REPLAY_CLOCK"):
        monkeypatch.delenv(k, raising=False)
    log = AuditLog(tmp_path / "audit.sqlite")
    ev = log.append("agent:hotel-aurora/buyer-1", "quote.created", "Q-test-7", "Created quote", {"total_net": 10.0})
    conn = log._conn()
    try:
        conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        conn.execute("UPDATE audit_events SET ts = ? WHERE seq = ?", (datetime(2026, 11, 7, 10, 5, 0, tzinfo=timezone.utc).isoformat(), ev.seq))
    finally:
        conn.close()
    md = log.render("Q-test-7")
    assert "_Sat 7 Nov 2026, times in Helsinki (EET), real clock._" in md


def test_pyproject_declares_tzdata_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert any(re.match(r"^tzdata(\b|[><=~!])", d.strip()) for d in deps), deps
