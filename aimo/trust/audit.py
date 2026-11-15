"""Append-only, hash-chained, signed audit log (SQLite table audit_events in aimo.config.STATE_DB).

Each event: hash = sha256(prev_hash + canonical_json(event without hash/signature/kid)),
signature = compact JWS (EdDSA) over {"typ":"audit-event","seq","hash"}. Genesis prev_hash = 64 zeros.
SQLite triggers refuse UPDATE/DELETE, so the table is append-only for normal code paths; verify()
detects any change made behind its back (e.g. tamper_for_demo, or editing the file directly).

Production path: write the same events to an append-only store (e.g. object storage with retention
lock / WORM, or a transparency log) and anchor the chain head periodically.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Optional

from aimo.models import AuditEvent
from aimo.trust.signing import JWSError, _reject_json_constant, canonical_json, jwks, kid, sha256_hex, sign_jws, verify_jws

GENESIS = "0" * 64
HASH_EXCLUDE = {"hash", "signature", "kid"}
MAX_ID_LEN = 256

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
_CREATE_NO_UPDATE_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
)
_TRIGGERS = f"""
{_CREATE_NO_UPDATE_TRIGGER};
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""


def _safe_str(val: Any) -> str:
    try:
        s = str(val)
    except Exception:
        s = f"<{type(val).__name__}>"
    if "\x00" in s:
        s = s.replace("\x00", "")
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        s = s.encode("utf-8", errors="replace").decode("utf-8")
    return s


def _require_id_field(val: Any, name: str) -> str:
    if isinstance(val, bool) or not isinstance(val, str):
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in val:
        raise ValueError(f"{name} must not contain null bytes")
    cleaned = _safe_str(val).strip()
    if not cleaned:
        raise ValueError(f"{name} must be a non-empty string")
    if len(cleaned) > MAX_ID_LEN:
        raise ValueError(f"{name} exceeds maximum length ({MAX_ID_LEN})")
    return cleaned


def _require_summary(val: Any) -> str:
    if isinstance(val, bool) or not isinstance(val, str):
        raise ValueError("summary must be a non-empty string")
    cleaned = _safe_str(val)
    if not cleaned.strip():
        raise ValueError("summary must be a non-empty string")
    return cleaned


def _valid_query_id(val: Any) -> str | None:
    if isinstance(val, bool) or not isinstance(val, str):
        return None
    if "\x00" in val or len(val) > MAX_ID_LEN:
        return None
    cleaned = _safe_str(val).strip()
    return cleaned if cleaned else None


def _sanitize_json_value(val: Any, _seen: set[int] | None = None, _depth: int = 0) -> Any:
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return int(val)
    if isinstance(val, float):
        f = float(val)
        return f if math.isfinite(f) else None
    if isinstance(val, Decimal):
        try:
            f = float(val)
        except (ValueError, OverflowError):
            return None
        return f if math.isfinite(f) else None
    if isinstance(val, str):
        return _safe_str(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, (bytes, bytearray, memoryview)):
        return _safe_str(bytes(val).decode("utf-8", errors="replace"))
    if _depth > 32:
        return _safe_str(val)
    seen = _seen if _seen is not None else set()
    if hasattr(val, "model_dump"):
        obj_id = id(val)
        if obj_id in seen:
            return "[circular]"
        seen.add(obj_id)
        try:
            try:
                dumped = val.model_dump(mode="json", warnings=False)
            except TypeError:
                dumped = val.model_dump(mode="json")
            return _sanitize_json_value(dumped, seen, _depth + 1)
        except Exception:
            return _safe_str(val)
        finally:
            seen.discard(obj_id)
    if isinstance(val, Mapping):
        obj_id = id(val)
        if obj_id in seen:
            return "[circular]"
        seen.add(obj_id)
        try:
            return {_safe_str(k): _sanitize_json_value(v, seen, _depth + 1) for k, v in val.items()}
        finally:
            seen.discard(obj_id)
    if isinstance(val, (list, tuple)):
        obj_id = id(val)
        if obj_id in seen:
            return "[circular]"
        seen.add(obj_id)
        try:
            return [_sanitize_json_value(v, seen, _depth + 1) for v in val]
        finally:
            seen.discard(obj_id)
    if isinstance(val, (set, frozenset)):
        obj_id = id(val)
        if obj_id in seen:
            return "[circular]"
        seen.add(obj_id)
        try:
            items = [_sanitize_json_value(v, seen, _depth + 1) for v in val]
            return sorted(items, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
        finally:
            seen.discard(obj_id)
    return _safe_str(val)


def _coerce_data_dict(data: Any) -> dict[str, Any]:
    if data is None:
        return {}
    if isinstance(data, Mapping) or hasattr(data, "model_dump"):
        sanitized = _sanitize_json_value(data)
        return sanitized if isinstance(sanitized, dict) else {"value": sanitized}
    return {"value": _sanitize_json_value(data)}


def _iter_related_raw(raw: Any) -> list[Any]:
    if raw is None or isinstance(raw, (bool, bytes, bytearray, memoryview, Mapping)):
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Iterable):
        try:
            return list(raw)
        except Exception:
            return []
    return []


def _collect_related_ids(raw_a: Any, raw_b: Any, subject_id: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in (*_iter_related_raw(raw_a), *_iter_related_raw(raw_b)):
        if isinstance(item, bool) or not isinstance(item, str):
            continue
        if "\x00" in item:
            continue
        cleaned = _safe_str(item).strip()
        if not cleaned or len(cleaned) > MAX_ID_LEN or cleaned == subject_id:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


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
    if isinstance(actor, bool) or not isinstance(actor, str):
        return ""
    kind, _, who = actor.partition(":")
    return {"agent": f"Buyer agent {who}", "human": who, "system": f"System ({who})"}.get(kind, actor) if who else actor


class AuditLog:
    def __init__(self, db_path: str | Path | None = None):
        self._db_path = Path(db_path) if db_path else None
        self._lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._ready: set[tuple[str, int]] = set()

    @property
    def db_path(self) -> Path:
        if self._db_path:
            return self._db_path
        from aimo import config
        return Path(config.STATE_DB)

    def _conn(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existed_nonempty = path.stat().st_size > 0
        except OSError:
            existed_nonempty = False
        con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 30000")
        try:
            ino = path.stat().st_ino
        except OSError:
            ino = 0
        ready_key = (str(path), ino)
        if not existed_nonempty or ready_key not in self._ready:
            with self._init_lock:
                existing = con.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name IN "
                    "('audit_events','audit_links','audit_tamper_backup','audit_no_update','audit_no_delete')"
                ).fetchone()[0]
                if existing < 5:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.executescript(_SCHEMA + _TRIGGERS)
                try:
                    ino = path.stat().st_ino
                except OSError:
                    ino = 0
                self._ready.add((str(path), ino))
        return con

    # ---------------------------------------------------------------- write
    def append(self, actor: str, action: str, subject_id: str, summary: str, data: dict | None = None,
               related_ids: Iterable[str] = ()) -> AuditEvent:
        """Append one event. Thread-safe and process-safe (BEGIN IMMEDIATE). `related_ids` (or
        data["related_ids"]) link this event to other subjects (quote <-> order <-> run)."""
        clean_actor = _require_id_field(actor, "actor")
        clean_action = _require_id_field(action, "action")
        clean_subject = _require_id_field(subject_id, "subject_id")
        clean_summary = _require_summary(summary)
        raw_data_rel = data.get("related_ids") if isinstance(data, Mapping) else None
        rel = _collect_related_ids(raw_data_rel, related_ids, clean_subject)
        clean_data = _coerce_data_dict(data)
        if rel:
            clean_data["related_ids"] = rel
        else:
            clean_data.pop("related_ids", None)
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
                seq, prev = (row["seq"] + 1, row["hash"]) if row else (1, GENESIS)
                ts, clk = _clock_now()
                row_data = {**clean_data, "clock": _sanitize_json_value(clk)} if clk else clean_data
                draft = AuditEvent(
                    seq=seq,
                    ts=ts,
                    actor=clean_actor,
                    action=clean_action,
                    subject_id=clean_subject,
                    summary=clean_summary,
                    data=row_data,
                    prev_hash=prev,
                    hash="",
                    signature="",
                    kid="",
                )
                core = event_core(draft)
                h = _event_hash(prev, core)
                sig = sign_jws({"typ": "audit-event", "seq": seq, "hash": h}, typ="audit+jws")
                ev = AuditEvent(**{**core, "hash": h, "signature": sig, "kid": kid()})
                con.execute(
                    "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        seq,
                        core["ts"],
                        clean_actor,
                        clean_action,
                        clean_subject,
                        clean_summary,
                        json.dumps(core["data"], ensure_ascii=False, sort_keys=True, allow_nan=False),
                        prev,
                        h,
                        sig,
                        ev.kid,
                    ),
                )
                for r in rel:
                    con.execute("INSERT INTO audit_links VALUES (?,?)", (seq, r))
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    try:
                        con.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                con.close()
        return ev

    # ---------------------------------------------------------------- read
    @staticmethod
    def _parse_row_strict(r: sqlite3.Row | Mapping[str, Any]) -> AuditEvent:
        raw_seq = r["seq"]
        if isinstance(raw_seq, bool) or not isinstance(raw_seq, int):
            raise ValueError("seq is not an integer")
        for field in ("ts", "actor", "action", "subject_id", "summary", "data", "prev_hash", "hash", "signature", "kid"):
            if isinstance(r[field], bool) or not isinstance(r[field], str):
                raise ValueError(f"{field} is not a string")
        parsed_data = json.loads(r["data"], parse_constant=_reject_json_constant)
        if not isinstance(parsed_data, dict):
            raise ValueError(f"data is not a JSON object (got {type(parsed_data).__name__})")
        return AuditEvent(
            seq=raw_seq,
            ts=r["ts"],
            actor=r["actor"],
            action=r["action"],
            subject_id=r["subject_id"],
            summary=r["summary"],
            data=parsed_data,
            prev_hash=r["prev_hash"],
            hash=r["hash"],
            signature=r["signature"],
            kid=r["kid"],
        )

    @staticmethod
    def _row(r: sqlite3.Row | Mapping[str, Any]) -> AuditEvent:
        raw_data = r["data"]
        if isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
            except Exception:
                data_dict: dict[str, Any] = {"_corrupt_json": _safe_str(raw_data)}
            else:
                data_dict = _coerce_data_dict(parsed) if parsed is not None else {"value": None}
        elif isinstance(raw_data, Mapping):
            data_dict = _coerce_data_dict(raw_data)
        else:
            data_dict = {"_corrupt_json": _safe_str(raw_data)}
        try:
            return AuditEvent(
                seq=r["seq"],
                ts=r["ts"],
                actor=r["actor"],
                action=r["action"],
                subject_id=r["subject_id"],
                summary=r["summary"],
                data=data_dict,
                prev_hash=r["prev_hash"],
                hash=r["hash"],
                signature=r["signature"],
                kid=r["kid"],
            )
        except Exception:
            raw_seq = r["seq"]
            seq_val = raw_seq if not isinstance(raw_seq, bool) and isinstance(raw_seq, int) else 0
            raw_ts = r["ts"]
            ts_val = datetime.fromtimestamp(0, tz=timezone.utc)
            if isinstance(raw_ts, datetime):
                ts_val = raw_ts
            elif isinstance(raw_ts, str):
                try:
                    s_ts = raw_ts[:-1] + "+00:00" if raw_ts.endswith("Z") else raw_ts
                    ts_val = datetime.fromisoformat(s_ts)
                except ValueError:
                    pass
            return AuditEvent.model_construct(
                seq=seq_val,
                ts=ts_val,
                actor=_safe_str(r["actor"]) if r["actor"] is not None else "",
                action=_safe_str(r["action"]) if r["action"] is not None else "",
                subject_id=_safe_str(r["subject_id"]) if r["subject_id"] is not None else "",
                summary=_safe_str(r["summary"]) if r["summary"] is not None else "",
                data=data_dict,
                prev_hash=_safe_str(r["prev_hash"]) if r["prev_hash"] is not None else "",
                hash=_safe_str(r["hash"]) if r["hash"] is not None else "",
                signature=_safe_str(r["signature"]) if r["signature"] is not None else "",
                kid=_safe_str(r["kid"]) if r["kid"] is not None else "",
            )

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        con = self._conn()
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def related_subjects(self, subject_id: str, max_hops: int = 4) -> set[str]:
        """Subject ids connected to subject_id through related_ids links (both directions)."""
        sid = _valid_query_id(subject_id)
        if sid is None:
            return set()
        hops = 0 if isinstance(max_hops, bool) or not isinstance(max_hops, int) else max(0, min(max_hops, 32))
        ids, frontier = {sid}, {sid}
        for _ in range(hops):
            if not frontier:
                break
            fr = tuple(frontier)[:500]
            ph = ",".join("?" * len(fr))
            found: set[str] = set()
            for r in self._rows(f"SELECT subject_id FROM audit_links WHERE seq IN (SELECT seq FROM audit_events WHERE subject_id IN ({ph}))", fr):
                cand = _valid_query_id(r["subject_id"])
                if cand is not None:
                    found.add(cand)
            for r in self._rows(f"SELECT e.subject_id FROM audit_events e JOIN audit_links l ON l.seq = e.seq WHERE l.subject_id IN ({ph})", fr):
                cand = _valid_query_id(r["subject_id"])
                if cand is not None:
                    found.add(cand)
            frontier = found - ids
            ids |= found
        return ids

    def events(self, subject_id: Optional[str] = None) -> list[AuditEvent]:
        """All events, or the events of subject_id plus those of linked subjects (quote <-> order <-> run)."""
        if subject_id is None:
            return [self._row(r) for r in self._rows("SELECT * FROM audit_events ORDER BY seq")]
        if _valid_query_id(subject_id) is None:
            return []
        ids = tuple(self.related_subjects(subject_id))[:450]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = self._rows(f"SELECT * FROM audit_events WHERE subject_id IN ({ph}) OR seq IN "
                          f"(SELECT seq FROM audit_links WHERE subject_id IN ({ph})) ORDER BY seq", ids + ids)
        return [self._row(r) for r in rows]

    def events_by_action(self, action: str) -> list[AuditEvent]:
        act = _valid_query_id(action)
        if act is None:
            return []
        return [self._row(r) for r in self._rows("SELECT * FROM audit_events WHERE action = ? ORDER BY seq", (act,))]

    def head(self) -> dict:
        rows = self._rows("SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        return {"seq": rows[0]["seq"], "hash": rows[0]["hash"]} if rows else {"seq": 0, "hash": GENESIS}

    # ---------------------------------------------------------------- verify
    def verify(self, jwks_: dict | None = None) -> dict:
        """Recompute the whole chain and every signature.
        Returns {ok, checked, broken_at, reason, head, kids}."""
        keys = jwks() if jwks_ is None else jwks_
        prev, expected_seq, checked, kids = GENESIS, 1, 0, set()
        for r in self._rows("SELECT * FROM audit_events ORDER BY seq"):
            raw_seq = r["seq"]
            seq = raw_seq if not isinstance(raw_seq, bool) and isinstance(raw_seq, int) else expected_seq

            def broken(reason: str) -> dict:
                return {"ok": False, "checked": checked, "broken_at": seq, "reason": reason, "head": None,
                        "kids": sorted(kids)}

            try:
                ev = self._parse_row_strict(r)
            except Exception as e:
                return broken(f"event \x23{seq} is corrupt: {e}")
            seq = ev.seq

            if seq != expected_seq:
                return broken(f"event \x23{expected_seq} is missing (next is \x23{seq})")
            if ev.prev_hash != prev:
                return broken(f"event \x23{seq} does not link to the previous event's hash")
            try:
                h = _event_hash(prev, event_core(ev))
            except Exception as e:
                return broken(f"event \x23{seq} cannot be hashed: {e}")
            if h != ev.hash:
                return broken(f"event \x23{seq} was modified after it was recorded (hash mismatch)")
            try:
                header, payload = verify_jws(ev.signature, keys)
            except JWSError as e:
                return broken(f"event \x23{seq} signature invalid: {e}")
            if payload.get("hash") != ev.hash or payload.get("seq") != seq or header.get("kid") != ev.kid:
                return broken(f"event \x23{seq} signature does not cover this event")
            kids.add(ev.kid)
            prev, expected_seq, checked = ev.hash, seq + 1, checked + 1
        return {"ok": True, "checked": checked, "broken_at": None, "reason": None,
                "head": prev if checked else GENESIS, "kids": sorted(kids)}

    # ---------------------------------------------------------------- human-readable
    def render(self, subject_id: str) -> str:
        """Markdown that reads like a story, with rule hits, approver and quote hash, ending with the
        chain verification result."""
        sid = _valid_query_id(subject_id)
        evs = self.events(sid) if sid is not None else []
        subject_label = (
            sid
            if sid is not None
            else (_safe_str(subject_id)[:MAX_ID_LEN] if isinstance(subject_id, str) and not isinstance(subject_id, bool) else "")
        )
        lines = [f"\x23 Audit trail for {subject_label}", ""]
        if not evs:
            lines.append("_No events recorded for this subject._")
        else:
            lbl = clock_label(evs)
            lines.append(f"_{_local_date(evs[0].ts)}, times in Helsinki ({_local(evs[0].ts, '%Z')}), {lbl}._")
            lines.append("")
            mixed = lbl.startswith("mixed")
            for e in evs:
                sim = " (sim)" if mixed and _event_clock_mode(e) is not None else ""
                lines.append(f"- **{_local(e.ts)}**{sim}, {e.summary}  `\x23{e.seq} {e.action}`")
        decisions = [e for e in evs if isinstance(e.data, dict) and isinstance(e.data.get("decision"), dict)]
        if decisions:
            d = decisions[-1].data["decision"]
            raw_outcome = d.get("outcome")
            outcome_txt = raw_outcome.replace("_", " ") if isinstance(raw_outcome, str) and raw_outcome.strip() else "?"
            raw_role = d.get("approver_role")
            role_txt = f" (approver: {raw_role.replace('_', ' ')})" if isinstance(raw_role, str) and raw_role.strip() else ""
            lines += ["", f"\x23\x23 Policy decision: {outcome_txt}" + role_txt,
                      "", f"Policy `{d.get('policy_id')}` version {d.get('policy_version')}.", "",
                      "| Rule | Outcome | Why |", "|---|---|---|"]
            raw_hits = d.get("hits")
            for h in (raw_hits if isinstance(raw_hits, list) else []):
                if not isinstance(h, dict):
                    continue
                raw_h_out = h.get("outcome")
                h_out = raw_h_out if isinstance(raw_h_out, str) and raw_h_out.strip() else "?"
                mark = {"allow": "pass", "needs_approval": "**needs approval**", "reject": "**REJECT**"}.get(h_out, h_out)
                raw_rid = h.get("rule_id")
                rid = raw_rid if isinstance(raw_rid, str) and raw_rid.strip() else "?"
                raw_msg = h.get("message")
                msg = raw_msg if isinstance(raw_msg, str) else ("" if raw_msg is None else _safe_str(raw_msg))
                lines.append(f"| `{rid}` | {mark} | {msg.replace('|', '/')} |")
        facts = []
        quotes = [
            e.data["quote"]
            for e in evs
            if isinstance(e.data, dict)
            and isinstance(e.data.get("quote"), dict)
            and isinstance(e.data["quote"].get("quote_hash"), str)
            and e.data["quote"].get("quote_hash")
        ]
        if quotes:
            q = quotes[-1]
            facts.append(f"- Quote `{q.get('quote_id')}` hash (sha256): `{q['quote_hash']}`, signed with key `{q.get('kid')}`")
        for e in evs:
            if not isinstance(e.data, dict):
                continue
            raw_approver = e.data.get("approver")
            if not isinstance(raw_approver, str) or not raw_approver.strip():
                continue
            raw_ev_role = e.data.get("approver_role")
            role = f" ({raw_ev_role.replace('_', ' ')})" if isinstance(raw_ev_role, str) and raw_ev_role.strip() else ""
            raw_note = e.data.get("note")
            note = f": \"{raw_note}\"" if isinstance(raw_note, str) and raw_note else ""
            if e.action == "approval.verified":
                verb = "Approved" if e.data.get("decision") == "approve" else "Rejected"
                facts.append(f"- {verb} by **{raw_approver}**{role} at {_local(e.ts)}, bound to quote hash "
                             f"`{_safe_str(e.data.get('quote_hash'))[:16]}…`{note}")
            elif e.action == "approval.refused":
                facts.append(f"- Refused attempt by {raw_approver}{role} at {_local(e.ts)}: {e.data.get('reason')}")
            elif e.action in ("order.approved", "order.rejected"):
                facts.append(f"- Order {e.action.split('.')[1]} by **{raw_approver}** at {_local(e.ts)}{note}")
        if facts:
            lines += ["", "\x23\x23 Key facts", "", *facts]
        v = self.verify()
        lines.append("")
        if v["ok"]:
            k = ", ".join(x[:16] + "…" for x in v["kids"] if isinstance(x, str)) or kid()[:16] + "…"
            lines.append(f"**Chain verified: {v['checked']} events, signatures valid (key kid {k}).** "
                         f"This trail shows {len(evs)} of them.")
        else:
            lines.append(f"**CHAIN BROKEN at event \x23{v['broken_at']}: {v['reason']}.** Do not rely on this trail.")
        return "\n".join(lines) + "\n"

    def evidence_pack(self, subject_id: str) -> dict:
        """Self-contained JSON for download: events + signed quote + decision + JWKS + how to verify."""
        sid = _valid_query_id(subject_id)
        evs = self.events(sid) if sid is not None else []
        clean_subject = (
            sid
            if sid is not None
            else (_safe_str(subject_id)[:MAX_ID_LEN] if isinstance(subject_id, str) and not isinstance(subject_id, bool) else "")
        )
        quote = next((e.data["quote"] for e in reversed(evs) if isinstance(e.data, dict) and isinstance(e.data.get("quote"), dict)), None)
        decision = next((e.data["decision"] for e in reversed(evs) if isinstance(e.data, dict) and isinstance(e.data.get("decision"), dict)), None)
        return {
            "subject_id": clean_subject,
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
            raise KeyError(f"no audit event \x23{seq}")
        if summary is not None:
            if isinstance(summary, bool) or not isinstance(summary, str):
                raise ValueError("summary must be a string")
            summary = _safe_str(summary)
        with self._lock:
            con = self._conn()
            try:
                row = con.execute("SELECT * FROM audit_events WHERE seq = ?", (seq,)).fetchone()
                if row is None:
                    raise KeyError(f"no audit event \x23{seq}")
                old_summary = _safe_str(row["summary"]) if isinstance(row["summary"], str) else ""
                new_summary = summary if summary is not None else old_summary.replace("approved", "rejected") \
                    if "approved" in old_summary else old_summary + " (edited)"
                new_data = (
                    json.dumps(_coerce_data_dict(data), ensure_ascii=False, sort_keys=True, allow_nan=False)
                    if data is not None
                    else row["data"]
                )
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT OR IGNORE INTO audit_tamper_backup VALUES (?,?,?)", (seq, row["summary"], row["data"]))
                con.execute("DROP TRIGGER IF EXISTS audit_no_update")
                con.execute("UPDATE audit_events SET summary = ?, data = ? WHERE seq = ?", (new_summary, new_data, seq))
                con.execute(_CREATE_NO_UPDATE_TRIGGER)
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    try:
                        con.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                con.close()
        return {"seq": seq, "old_summary": row["summary"], "new_summary": new_summary}

    def untamper_for_demo(self, seq: int) -> bool:
        """DEV ONLY: restore an event changed by tamper_for_demo."""
        if os.environ.get("AIMO_ENV", "dev").lower() in ("prod", "production"):
            raise PermissionError("untamper_for_demo is disabled in production")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1 or seq > (1 << 62):
            raise KeyError(f"no audit event \x23{seq}")
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
                con.execute(_CREATE_NO_UPDATE_TRIGGER)
                con.execute("COMMIT")
                return True
            except BaseException:
                if con.in_transaction:
                    try:
                        con.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                con.close()


audit = AuditLog()
