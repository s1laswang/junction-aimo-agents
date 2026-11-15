"""The committed live Gemini run (docs/evidence/live-run) verifies offline and is one coherent run.
No keys, no network, no catalog needed."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "docs" / "evidence" / "live-run"
VERIFY = ROOT / "scripts" / "verify_quote.py"


def _verify(doc: Path, jwks: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(VERIFY), str(doc), str(jwks)], capture_output=True, text=True, timeout=60)


def _load(name: str):
    return json.loads((PACK / name).read_text())


def test_quote_and_audit_chain_verify_offline():
    for name in ("quote.json", "audit-events.json"):
        r = _verify(PACK / name, PACK / "jwks.json")
        assert r.returncode == 0 and "VERIFIED" in r.stdout, (name, r.stdout[-400:], r.stderr[-400:])


def test_tampered_quote_or_event_fails(tmp_path):
    quote = _load("quote.json")
    quote["total_net"] = 10.10
    (tmp_path / "quote.json").write_text(json.dumps(quote))
    r = _verify(tmp_path / "quote.json", PACK / "jwks.json")
    assert r.returncode != 0 or "VERIFIED" not in r.stdout
    events = _load("audit-events.json")
    events[3]["summary"] = events[3]["summary"].replace("approved", "rejected") + " (edited)"
    (tmp_path / "events.json").write_text(json.dumps(events))
    r = _verify(tmp_path / "events.json", PACK / "jwks.json")
    assert r.returncode != 0 or "VERIFIED" not in r.stdout


def test_pack_is_one_run():
    run, quote, order, events = _load("run.json"), _load("quote.json"), _load("order.json"), _load("audit-events.json")
    trace = [json.loads(line) for line in (PACK / "gemini-trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {t["run_id"] for t in trace} == {run["run_id"]}
    assert all(t["ok"] and t["request"] and t["response"] and t["usage"] for t in trace if t["ok"])
    assert {t["model"] for t in trace} <= {"gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash"}
    assert run["quote"]["quote_id"] == quote["quote_id"] and run["quote"]["quote_hash"] == quote["quote_hash"]
    assert order["quote_id"] == quote["quote_id"] and order["quote_hash"] == quote["quote_hash"]
    assert order["status"] == "placed" and order["response_code"] == "AP" and quote["total_net"] == 1010.0
    assert order["approved_by"] == "anna@hotel-aurora.example"
    actions = [e["action"] for e in events]
    assert actions == ["quote.created", "policy.evaluated", "order.pending_approval", "approval.verified",
                       "order.approved", "order.placed"]
    assert any(quote["quote_id"] in json.dumps(e) for e in events) and any(order["order_id"] in json.dumps(e) for e in events)
    calls = [p["function_call"]["name"] for t in trace for c in (t["response"] or {}).get("candidates", [])
             for p in (c.get("content") or {}).get("parts", []) if "function_call" in p]
    assert calls[-2:] == ["create_quote", "place_order"] and "plan_need" in calls


def test_summarize_script_reproduces_transcript_offline():
    res = subprocess.run(
        [sys.executable, str(PACK / "summarize.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert res.stdout == (PACK / "transcript.md").read_text(encoding="utf-8")
    for md in PACK.glob("*.md"):
        assert "\u2014" not in md.read_text(encoding="utf-8")
    for py in (PACK / "summarize.py", Path(__file__).resolve()):
        src = py.read_text(encoding="utf-8")
        assert "\x23" not in src
        assert "\u2014" not in src
