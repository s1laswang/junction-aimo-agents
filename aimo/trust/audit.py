"""Append-only, hash-chained, signed audit log (SQLite table audit_events in aimo.config.STATE_DB).

Each event: hash = sha256(prev_hash + canonical_json(event without hash/signature/kid)),
signature = compact JWS (EdDSA) over {"typ":"audit-event","seq","hash"}. Genesis prev_hash = 64 zeros.
SQLite triggers refuse UPDATE/DELETE, so the table is append-only for normal code paths; verify()
detects any change made behind its back (e.g. tamper_for_demo, or editing the file directly).

Production path: write the same events to an append-only store (e.g. object storage with retention
lock / WORM, or a transparency log) and anchor the chain head periodically.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from aimo.models import AuditEvent
from aimo.trust.signing import JWSError, canonical_json, jwks, kid, sha256_hex, sign_jws, verify_jws

GENESIS = "0" * 64
HASH_EXCLUDE = {"hash", "signature", "kid"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
  seq INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  data TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE,
  signature TEXT NOT NULL,
  kid TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_subject ON audit_events(subject_id);
CREATE INDEX IF NOT EXISTS audit_events_action ON audit_events(action);
CREATE TABLE IF NOT EXISTS audit_links (seq INTEGER NOT NULL, subject_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_links_subject ON audit_links(subject_id);
CREATE TABLE IF NOT EXISTS audit_tamper_backup (seq INTEGER PRIMARY KEY, summary TEXT, data TEXT);
"""
_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""


def _event_hash(prev_hash: str, core: dict) -> str:
    return sha256_hex(prev_hash + canonical_json(core))


def event_core(ev: AuditEvent | dict) -> dict:
    """The hashed part of an event: its JSON form without hash/signature/kid."""
    d = ev.model_dump(mode="json") if isinstance(ev, AuditEvent) else dict(ev)
    return {k: v for k, v in d.items() if k not in HASH_EXCLUDE}


def _clock_now() -> tuple[datetime, dict | None]:
    real = datetime.now(timezone.utc)
    try:
        from aimo.backend import clock
        if clock.simulated():
            return clock.now(), {"mode": clock.describe().get("mode"), "wall_ts": real.isoformat()}
    except Exception:
        pass
    return real, None


def _event_clock_mode(ev: Any) -> str | None:
    d = getattr(ev, "data", None)
    if not isinstance(d, dict):
        return None
    clk = d.get("clock")
    if not isinstance(clk, dict):
        return None
    mode = clk.get("mode")
    return str(mode) if isinstance(mode, str) and mode else None


def clock_label(evs: list) -> str:
    modes = {_event_clock_mode(e) for e in evs}
    if not modes or modes == {None}:
        return "real clock"
    sim = sorted(m for m in modes if m)
    if not sim:
        return "real clock"
    src = {"env": "AIMO_NOW", "header": "X-Aimo-Now", "replay": "replay snapshot"}
    what = ", ".join(src.get(m, m) for m in sim)
    if None in modes:
        return f"mixed clocks: events marked (sim) use the simulated clock ({what}), the rest real time"
    return f"simulated clock ({what}); real wall time is in each event's data.clock.wall_ts"


def _local(ts: datetime, fmt: str = "%H:%M:%S") -> str:
    try:
        from zoneinfo import ZoneInfo
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo("Europe/Helsinki")).strftime(fmt)
    except Exception:
        return ts.strftime(fmt)


def _local_date(ts: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        dt = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo("Europe/Helsinki"))
    except Exception:
        dt = ts
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b %Y')}"


def actor_text(actor: str) -> str:
    kind, _, who = actor.partition(":")
    return {"agent": f"Buyer agent {who}", "human": who, "system": f"System ({who})"}.get(kind, actor) if who else actor


class AuditLog:
    def __init__(self, db_path: str | Path | None = None):
        self._db_path = Path(db_path) if db_path else None
        self._lock = threading.Lock()
        self._ready: set[str] = set()

    @property
    def db_path(self) -> Path:
        if self._db_path:
            return self._db_path
        from aimo import config
        return Path(config.STATE_DB)

    def _conn(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        con.row_factory = sqlite3.Row
        if str(path) not in self._ready:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA + _TRIGGERS)
            self._ready.add(str(path))
        return con

    # ---------------------------------------------------------------- write
    def append(self, actor: str, action: str, subject_id: str, summary: str, data: dict | None = None,
               related_ids: Iterable[str] = ()) -> AuditEvent:
        """Append one event. Thread-safe and process-safe (BEGIN IMMEDIATE). `related_ids` (or
        data["related_ids"]) link this event to other subjects (quote <-> order <-> run)."""
        data = dict(data or {})
        rel = list(dict.fromkeys([*data.get("related_ids", []), *related_ids]))
        rel = [r for r in rel if r and r != subject_id]
        if rel:
            data["related_ids"] = rel
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
                seq, prev = (row["seq"] + 1, row["hash"]) if row else (1, GENESIS)
                ts, clk = _clock_now()
                if clk:
                    data = {**data, "clock": clk}
                draft = AuditEvent(seq=seq, ts=ts, actor=actor, action=action,
                                   subject_id=subject_id, summary=summary, data=data, prev_hash=prev,
                                   hash="", signature="", kid="")
                core = event_core(draft)
                h = _event_hash(prev, core)
                sig = sign_jws({"typ": "audit-event", "seq": seq, "hash": h}, typ="audit+jws")
                ev = AuditEvent(**{**core, "hash": h, "signature": sig, "kid": kid()})
                con.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (seq, core["ts"], actor, action, subject_id, summary,
                             json.dumps(core["data"], ensure_ascii=False, sort_keys=True), prev, h, sig, ev.kid))
                for r in rel:
                    con.execute("INSERT INTO audit_links VALUES (?,?)", (seq, r))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            finally:
                con.close()
        return ev

    # ---------------------------------------------------------------- read
    @staticmethod
    def _row(r: sqlite3.Row) -> AuditEvent:
        return AuditEvent(seq=r["seq"], ts=r["ts"], actor=r["actor"], action=r["action"], subject_id=r["subject_id"],
                          summary=r["summary"], data=json.loads(r["data"]), prev_hash=r["prev_hash"], hash=r["hash"],
                          signature=r["signature"], kid=r["kid"])

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        con = self._conn()
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def related_subjects(self, subject_id: str, max_hops: int = 4) -> set[str]:
        """Subject ids connected to subject_id through related_ids links (both directions)."""
        if not isinstance(subject_id, str) or not subject_id.strip():
            return set()
        hops = 0 if isinstance(max_hops, bool) or not isinstance(max_hops, int) else max(0, min(max_hops, 32))
        ids, frontier = {subject_id}, {subject_id}
        for _ in range(hops):
            if not frontier:
                break
            ph = ",".join("?" * len(frontier))
            fr = tuple(frontier)
            found = set()
            for r in self._rows(f"SELECT subject_id FROM audit_links WHERE seq IN (SELECT seq FROM audit_events WHERE subject_id IN ({ph}))", fr):
                found.add(r["subject_id"])
            for r in self._rows(f"SELECT e.subject_id FROM audit_events e JOIN audit_links l ON l.seq = e.seq WHERE l.subject_id IN ({ph})", fr):
                found.add(r["subject_id"])
            frontier = found - ids
            ids |= found
        return ids

    def events(self, subject_id: Optional[str] = None) -> list[AuditEvent]:
        """All events, or the events of subject_id plus those of linked subjects (quote <-> order <-> run)."""
        if subject_id is None:
            return [self._row(r) for r in self._rows("SELECT * FROM audit_events ORDER BY seq")]
        ids = tuple(self.related_subjects(subject_id))
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = self._rows(f"SELECT * FROM audit_events WHERE subject_id IN ({ph}) OR seq IN "
                          f"(SELECT seq FROM audit_links WHERE subject_id IN ({ph})) ORDER BY seq", ids + ids)
        return [self._row(r) for r in rows]

    def events_by_action(self, action: str) -> list[AuditEvent]:
        return [self._row(r) for r in self._rows("SELECT * FROM audit_events WHERE action = ? ORDER BY seq", (action,))]

    def head(self) -> dict:
        rows = self._rows("SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        return {"seq": rows[0]["seq"], "hash": rows[0]["hash"]} if rows else {"seq": 0, "hash": GENESIS}

    # ---------------------------------------------------------------- verify
    def verify(self, jwks_: dict | None = None) -> dict:
        """Recompute the whole chain and every signature.
        Returns {ok, checked, broken_at, reason, head, kids}."""
        keys = jwks_ or jwks()
        prev, expected_seq, checked, kids = GENESIS, 1, 0, set()
        for r in self._rows("SELECT * FROM audit_events ORDER BY seq"):
            ev = self._row(r)
            seq = ev.seq

            def broken(reason: str) -> dict:
                return {"ok": False, "checked": checked, "broken_at": seq, "reason": reason, "head": None,
                        "kids": sorted(kids)}

            if seq != expected_seq:
                return broken(f"event #{expected_seq} is missing (next is #{seq})")
            if ev.prev_hash != prev:
                return broken(f"event #{seq} does not link to the previous event's hash")
            h = _event_hash(prev, event_core(ev))
            if h != ev.hash:
                return broken(f"event #{seq} was modified after it was recorded (hash mismatch)")
            try:
                header, payload = verify_jws(ev.signature, keys)
            except JWSError as e:
                return broken(f"event #{seq} signature invalid: {e}")
            if payload.get("hash") != ev.hash or payload.get("seq") != seq or header.get("kid") != ev.kid:
                return broken(f"event #{seq} signature does not cover this event")
            kids.add(ev.kid)
            prev, expected_seq, checked = ev.hash, seq + 1, checked + 1
        return {"ok": True, "checked": checked, "broken_at": None, "reason": None,
                "head": prev if checked else GENESIS, "kids": sorted(kids)}

    # ---------------------------------------------------------------- human-readable
    def render(self, subject_id: str) -> str:
        """Markdown that reads like a story, with rule hits, approver and quote hash, ending with the
        chain verification result."""
        evs = self.events(subject_id)
        lines = [f"# Audit trail for {subject_id}", ""]
        if not evs:
            lines.append("_No events recorded for this subject._")
        else:
            lbl = clock_label(evs)
            lines.append(f"_{_local_date(evs[0].ts)}, times in Helsinki ({_local(evs[0].ts, '%Z')}), {lbl}._")
            lines.append("")
            mixed = lbl.startswith("mixed")
            for e in evs:
                sim = " (sim)" if mixed and _event_clock_mode(e) is not None else ""
                lines.append(f"- **{_local(e.ts)}**{sim}, {e.summary}  `#{e.seq} {e.action}`")
        decisions = [e for e in evs if isinstance(e.data.get("decision"), dict)]
        if decisions:
            d = decisions[-1].data["decision"]
            lines += ["", f"## Policy decision: {d.get('outcome', '?').replace('_', ' ')}"
                          + (f" (approver: {d['approver_role'].replace('_', ' ')})" if d.get("approver_role") else ""),
                      "", f"Policy `{d.get('policy_id')}` version {d.get('policy_version')}.", "",
                      "| Rule | Outcome | Why |", "|---|---|---|"]
            for h in d.get("hits", []):
                mark = {"allow": "pass", "needs_approval": "**needs approval**", "reject": "**REJECT**"}.get(h["outcome"], h["outcome"])
                lines.append(f"| `{h['rule_id']}` | {mark} | {h['message'].replace('|', '/')} |")
        facts = []
        quotes = [e.data["quote"] for e in evs if isinstance(e.data.get("quote"), dict) and e.data["quote"].get("quote_hash")]
        if quotes:
            q = quotes[-1]
            facts.append(f"- Quote `{q.get('quote_id')}` hash (sha256): `{q['quote_hash']}`, signed with key `{q.get('kid')}`")
        for e in evs:
            if not e.data.get("approver"):
                continue
            role = f" ({str(e.data.get('approver_role')).replace('_', ' ')})" if e.data.get("approver_role") else ""
            note = f": \"{e.data['note']}\"" if e.data.get("note") else ""
            if e.action == "approval.verified":
                verb = "Approved" if e.data.get("decision") == "approve" else "Rejected"
                facts.append(f"- {verb} by **{e.data['approver']}**{role} at {_local(e.ts)}, bound to quote hash "
                             f"`{str(e.data.get('quote_hash'))[:16]}…`{note}")
            elif e.action == "approval.refused":
                facts.append(f"- Refused attempt by {e.data['approver']}{role} at {_local(e.ts)}: {e.data.get('reason')}")
            elif e.action in ("order.approved", "order.rejected"):
                facts.append(f"- Order {e.action.split('.')[1]} by **{e.data['approver']}** at {_local(e.ts)}{note}")
        if facts:
            lines += ["", "## Key facts", "", *facts]
        v = self.verify()
        lines.append("")
        if v["ok"]:
            k = ", ".join(x[:16] + "…" for x in v["kids"]) or kid()[:16] + "…"
            lines.append(f"**Chain verified: {v['checked']} events, signatures valid (key kid {k}).** "
                         f"This trail shows {len(evs)} of them.")
        else:
            lines.append(f"**CHAIN BROKEN at event #{v['broken_at']}: {v['reason']}.** Do not rely on this trail.")
        return "\n".join(lines) + "\n"

    def evidence_pack(self, subject_id: str) -> dict:
        """Self-contained JSON for download: events + signed quote + decision + JWKS + how to verify."""
        evs = self.events(subject_id)
        quote = next((e.data["quote"] for e in reversed(evs) if isinstance(e.data.get("quote"), dict)), None)
        decision = next((e.data["decision"] for e in reversed(evs) if isinstance(e.data.get("decision"), dict)), None)
        return {
            "subject_id": subject_id,
            "generated_at": _clock_now()[0].isoformat(),
            "events": [e.model_dump(mode="json") for e in evs],
            "quote": quote,
            "decision": decision,
            "chain": self.verify(),
            "jwks": jwks(),
            "how_to_verify": [
                "Quote: recompute sha256(canonical_json(quote minus quote_hash/signature/kid)) and compare with "
                "quote_hash; verify the compact JWS in quote.signature (alg EdDSA) with the JWKS key whose kid matches; "
                "its payload.quote_hash must equal the recomputed hash; check expires_at.",
                "Events: for each event hash = sha256(prev_hash + canonical_json(event minus hash/signature/kid)); "
                "first prev_hash is 64 zeros; verify each signature (JWS payload {typ, seq, hash}) with the JWKS. "
                "A subject's events are a subset of the global chain, so prev_hash links are only contiguous in the "
                "full chain (GET /v1/audit).",
                "canonical_json = JSON with sorted keys, no whitespace, UTF-8, integral numbers without '.0' (RFC 8785-style).",
            ],
        }

    # ---------------------------------------------------------------- demo only
    def tamper_for_demo(self, seq: int, summary: str | None = None, data: dict | None = None) -> dict:
        """DEV ONLY: silently rewrite one event (bypassing the append-only trigger) so verify() can be
        shown catching it. Keeps a backup; call untamper_for_demo(seq) to restore. Refused when
        AIMO_ENV=production."""
        if os.environ.get("AIMO_ENV", "dev").lower() in ("prod", "production"):
            raise PermissionError("tamper_for_demo is disabled in production")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1 or seq > (1 << 62):
            raise KeyError(f"no audit event #{seq}")
        with self._lock:
            con = self._conn()
            try:
                row = con.execute("SELECT * FROM audit_events WHERE seq = ?", (seq,)).fetchone()
                if row is None:
                    raise KeyError(f"no audit event #{seq}")
                new_summary = summary if summary is not None else row["summary"].replace("approved", "rejected") \
                    if "approved" in row["summary"] else row["summary"] + " (edited)"
                new_data = json.dumps(data, sort_keys=True) if data is not None else row["data"]
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT OR IGNORE INTO audit_tamper_backup VALUES (?,?,?)", (seq, row["summary"], row["data"]))
                con.execute("DROP TRIGGER IF EXISTS audit_no_update")
                con.execute("UPDATE audit_events SET summary = ?, data = ? WHERE seq = ?", (new_summary, new_data, seq))
                con.execute("COMMIT")
                con.executescript(_TRIGGERS)
            finally:
                con.close()
        return {"seq": seq, "old_summary": row["summary"], "new_summary": new_summary}

    def untamper_for_demo(self, seq: int) -> bool:
        """DEV ONLY: restore an event changed by tamper_for_demo."""
        if os.environ.get("AIMO_ENV", "dev").lower() in ("prod", "production"):
            raise PermissionError("untamper_for_demo is disabled in production")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1 or seq > (1 << 62):
            raise KeyError(f"no audit event #{seq}")
        with self._lock:
            con = self._conn()
            try:
                b = con.execute("SELECT * FROM audit_tamper_backup WHERE seq = ?", (seq,)).fetchone()
                if b is None:
                    return False
                con.execute("BEGIN IMMEDIATE")
                con.execute("DROP TRIGGER IF EXISTS audit_no_update")
                con.execute("UPDATE audit_events SET summary = ?, data = ? WHERE seq = ?", (b["summary"], b["data"], seq))
                con.execute("DELETE FROM audit_tamper_backup WHERE seq = ?", (seq,))
                con.execute("COMMIT")
                con.executescript(_TRIGGERS)
                return True
            finally:
                con.close()


audit = AuditLog()
