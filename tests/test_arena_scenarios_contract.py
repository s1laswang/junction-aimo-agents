from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tokenize
from types import ModuleType

from aimo.backend.peppol import check_t116_rules
from aimo.gateway.mcp_server import build_mcp
from conformance.verify import verify_events, verify_jws, verify_quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCENARIOS_JS = ROOT / "docs" / "arena" / "scenarios.js"
RECORDER_PY = ROOT / "scripts" / "record_arena_fixtures.py"
VERIFY_QUOTE_PY = ROOT / "scripts" / "verify_quote.py"

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
HASH_CHAR = chr(35)


def _load_recorder_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_arena_fixtures", RECORDER_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_node_binary() -> str | None:
    direct = shutil.which("node")
    if direct:
        return direct
    exe_root = pathlib.Path(sys.executable).resolve().parents[1]
    for candidate in exe_root.glob("lib*/python*/site-packages/playwright/driver/node"):
        if candidate.is_file():
            return str(candidate)
    return None


def test_scenarios_js_bundle_exists_size_budget_and_redaction_hygiene() -> None:
    mod = _load_recorder_module()
    assert SCENARIOS_JS.is_file(), f"Missing {SCENARIOS_JS}"
    raw_bytes = SCENARIOS_JS.read_bytes()
    assert 0 < len(raw_bytes) < mod.MAX_JS_BYTES
    raw_text = raw_bytes.decode("utf-8")

    assert EM_DASH not in raw_text
    assert EN_DASH not in raw_text
    for forbidden_tok in ("<!--", "-->", "/*", "*/", "//", "/tmp/", "fleet-root"):
        assert forbidden_tok not in raw_text, f"Forbidden token {forbidden_tok!r} in scenarios.js"
    assert mod.JS_PREFIX in raw_text
    assert mod.JS_SCENARIOS_LINE in raw_text
    assert 'module.exports = window.AIMO_ARENA_DATA' in raw_text
    assert mod.DEMO_PIN not in raw_text
    assert "PRIVATE KEY" not in raw_text

    v2_tokens = re.findall(r"\bv2\.[^\s\"']+", raw_text)
    assert v2_tokens, "Expected redacted v2 approver session tokens in scenarios.js"
    for tok in v2_tokens:
        assert tok.endswith(".<redacted>"), f"Unredacted v2 session token in scenarios.js: {tok}"


def test_parsed_envelope_mcp_tools_and_six_scenario_order() -> None:
    mod = _load_recorder_module()
    raw_text = SCENARIOS_JS.read_text(encoding="utf-8")
    data = mod.parse_scenarios_js(raw_text)

    assert data["schema_version"] == "1.0"
    assert data["gemini_calls"] == 0
    assert data["scenario_order"] == [
        "happy_path",
        "price_mid_approval",
        "slip_allergen",
        "fake_approver",
        "prompt_injection",
        "unknown_customer",
    ]

    live_tools = [t.name for t in asyncio.run(build_mcp().list_tools())]
    assert len(live_tools) == 13
    assert data["mcp_tool_count"] == 13
    assert data["mcp_tools"] == live_tools
    assert [n for n in data["mcp_tools"] if "approv" in n.lower() or "decid" in n.lower()] == []

    scenarios = data["scenarios"]
    assert len(scenarios) >= 6
    assert [sc["id"] for sc in scenarios] == data["scenario_order"]
    assert set(data["scenarios_by_id"]) == set(data["scenario_order"])

    by_id = data["scenarios_by_id"]
    assert by_id["happy_path"]["category"] == "normal"
    assert by_id["happy_path"]["customer_id"] == "hotel-aurora"
    assert by_id["happy_path"]["blocked"] is False
    assert by_id["happy_path"]["http_status"] == 200

    expected_attack_customers = {
        "price_mid_approval": "hotel-aurora",
        "slip_allergen": "school-tampere",
        "fake_approver": "hotel-aurora",
        "prompt_injection": "hotel-aurora",
        "unknown_customer": "shadow-bistro-99",
    }
    for atk_id, cust_id in expected_attack_customers.items():
        sc = by_id[atk_id]
        assert sc["category"] == "attack"
        assert sc["customer_id"] == cust_id
        assert sc["blocked"] is True


def test_all_recorded_quotes_audit_chain_and_peppol_xml_verify_cryptographically() -> None:
    mod = _load_recorder_module()
    raw_bytes = SCENARIOS_JS.read_bytes()
    data = mod.parse_scenarios_js(raw_bytes.decode("utf-8"))
    counts = mod.validate_arena_payload(data, raw_size=len(raw_bytes))

    assert counts["scenarios"] >= 6
    assert counts["steps"] >= 25
    assert counts["quotes"] >= 5
    assert counts["audit_events"] >= 27

    jwks = data["jwks"]
    for sc in data["scenarios"]:
        for q in sc["quotes"]:
            vq = verify_quote(q, jwks)
            assert vq["ok"] is True, vq
            verify_jws(q["signature"], jwks)

    chain = data["audit_chain"]
    assert [ev["seq"] for ev in chain] == list(range(1, len(chain) + 1))
    vc = verify_events(chain, jwks, contiguous=True)
    assert vc["ok"] is True, vc
    assert data["audit_verify"]["ok"] is True
    assert data["audit_verify"]["checked"] == len(chain)

    peppol_xml = data["scenarios_by_id"]["happy_path"]["peppol_xml"]
    assert isinstance(peppol_xml, str) and "OrderResponse" in peppol_xml
    assert "<cbc:OrderResponseCode>AP</cbc:OrderResponseCode>" in peppol_xml
    assert check_t116_rules(peppol_xml) == []


def test_standalone_verify_quote_script_verifies_arena_quotes_and_rejects_tampering(
    tmp_path: pathlib.Path,
) -> None:
    mod = _load_recorder_module()
    data = mod.parse_scenarios_js(SCENARIOS_JS.read_text(encoding="utf-8"))
    jwks_path = tmp_path / "jwks.json"
    quote_path = tmp_path / "quote.json"
    events_path = tmp_path / "audit-events.json"

    hero_quote = data["scenarios_by_id"]["happy_path"]["quotes"][0]
    jwks_path.write_text(json.dumps(data["jwks"]), encoding="utf-8")
    quote_path.write_text(json.dumps(hero_quote), encoding="utf-8")
    events_path.write_text(json.dumps(data["audit_chain"]), encoding="utf-8")

    for doc_path in (quote_path, events_path):
        proc = subprocess.run(
            [sys.executable, "-I", str(VERIFY_QUOTE_PY), str(doc_path), str(jwks_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (doc_path.name, proc.stdout, proc.stderr)
        assert "VERIFIED" in proc.stdout

    tampered = dict(hero_quote)
    tampered["total_net"] = float(hero_quote["total_net"]) + 10.0
    tampered_path = tmp_path / "tampered-quote.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    bad_proc = subprocess.run(
        [sys.executable, "-I", str(VERIFY_QUOTE_PY), str(tampered_path), str(jwks_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bad_proc.returncode != 0
    assert "VERIFIED" not in bad_proc.stdout


def test_happy_path_and_five_scam_scenarios_guardrail_assertions() -> None:
    mod = _load_recorder_module()
    data = mod.parse_scenarios_js(SCENARIOS_JS.read_text(encoding="utf-8"))
    by_id = data["scenarios_by_id"]

    happy_steps = {st["id"]: st for st in by_id["happy_path"]["steps"]}
    plan1 = happy_steps["plan_milk"]["response_body"]
    assert plan1["lines"][0]["product_id"] == "464306"
    assert plan1["lines"][0]["qty_sales_units"] == 1000.0
    assert plan1["lines"][0]["injection_screen"]["suspicious"] is False
    assert happy_steps["submit_order"]["response_body"]["status"] == "pending_approval"
    assert happy_steps["approve_order"]["response_body"]["status"] == "placed"
    assert happy_steps["approve_order"]["response_body"]["response_code"] == "AP"
    assert happy_steps["verify_audit"]["response_body"]["ok"] is True

    price_steps = {st["id"]: st for st in by_id["price_mid_approval"]["steps"]}
    assert price_steps["forged_hash_attempt"]["http_status"] == 409
    assert "quote_hash_mismatch" in json.dumps(price_steps["forged_hash_attempt"]["response_body"])
    hike = price_steps["price_hike_after_expiry"]
    assert hike["http_status"] == 409
    assert hike["response_body"]["error"] == "requote_needed"
    hike_detail = hike["response_body"]["detail"]
    assert hike_detail["reason"] == "quote_expired"
    assert hike_detail["new_quote"]["total_net"] > 1010.0

    allergen_steps = {st["id"]: st for st in by_id["slip_allergen"]["steps"]}
    school_plan = allergen_steps["school_plan_dairy_sneak"]["response_body"]
    planned_ids = [ln["product_id"] for ln in school_plan["lines"]]
    assert "463038" not in planned_ids and "464306" not in planned_ids
    assert "470889" in planned_ids
    forced_quote = allergen_steps["force_dairy_quote"]["response_body"]
    assert forced_quote["lines"][0]["tier"] == "rejected"
    assert any("milk" in fl for fl in forced_quote["lines"][0]["flags"])
    forced_order = allergen_steps["force_dairy_order"]["response_body"]
    assert forced_order["status"] == "rejected"

    approver_steps = {st["id"]: st for st in by_id["fake_approver"]["steps"]}
    assert approver_steps["agent_self_approve_impersonation"]["http_status"] == 403
    assert approver_steps["wrong_pin_attempt_1"]["http_status"] == 401
    assert approver_steps["wrong_pin_lockout_5"]["http_status"] == 429
    refused_actions = [ev["action"] for ev in by_id["fake_approver"]["audit_events"]]
    assert "approval.refused" in refused_actions

    inj_steps = {st["id"]: st for st in by_id["prompt_injection"]["steps"]}
    inj_plan = inj_steps["plan_with_injected_note_and_catalog"]["response_body"]
    assert inj_plan["lines"][0]["qty_sales_units"] == 1000.0
    assert inj_plan["lines"][0]["injection_screen"]["suspicious"] is True
    assert any(
        (cand.get("injection_screen") or {}).get("suspicious") is True
        for cand in inj_plan["candidates"]
    )
    inj_order = inj_steps["order_escalated_by_injection_rule"]["response_body"]
    assert inj_order["status"] == "pending_approval"

    unk_steps = {st["id"]: st for st in by_id["unknown_customer"]["steps"]}
    assert unk_steps["search_missing_identity"]["http_status"] == 401
    assert unk_steps["search_unknown_customer"]["http_status"] == 403
    assert unk_steps["plan_unknown_customer"]["http_status"] == 403
    assert unk_steps["quote_missing_headers"]["http_status"] == 401
    assert unk_steps["quote_unknown_customer"]["http_status"] == 403


def test_live_keyless_gateway_parity_check_cli(tmp_path: pathlib.Path) -> None:
    mod = _load_recorder_module()
    live_data = mod.record_arena_data(scratch_root=tmp_path)
    rendered = mod.render_scenarios_js(live_data)
    out_file = tmp_path / "scenarios.js"
    out_file.write_text(rendered, encoding="utf-8")

    rc_existing = mod.main(["--check", "--output", str(SCENARIOS_JS)])
    assert rc_existing == 0
    rc_fresh = mod.main(["--check", "--output", str(out_file)])
    assert rc_fresh == 0


def test_v8_or_structure_contract_for_window_arena_scenarios_and_aimo_arena_data() -> None:
    raw_js = SCENARIOS_JS.read_text(encoding="utf-8")
    assert "window.AIMO_ARENA_DATA = " in raw_js
    assert "window.ARENA_SCENARIOS = window.AIMO_ARENA_DATA.scenarios;" in raw_js

    node_bin = _find_node_binary()
    if node_bin is not None:
        script = (
            "const fs = require('fs'); const vm = require('vm');"
            f"const code = fs.readFileSync({json.dumps(str(SCENARIOS_JS))}, 'utf8');"
            "const browserWin = {}; vm.runInNewContext(code, { window: browserWin });"
            "if (!browserWin.AIMO_ARENA_DATA || !Array.isArray(browserWin.ARENA_SCENARIOS)) process.exit(2);"
            "if (browserWin.ARENA_SCENARIOS.length < 6) process.exit(3);"
            "if (browserWin.ARENA_SCENARIOS.happy_path.id !== 'happy_path') process.exit(4);"
            "if (browserWin.ARENA_SCENARIOS.scenarios !== browserWin.AIMO_ARENA_DATA.scenarios) process.exit(5);"
            "if (Object.keys(browserWin.ARENA_SCENARIOS).length < 6) process.exit(6);"
            f"const cjs = require({json.dumps(str(SCENARIOS_JS))});"
            "if (cjs !== globalThis.AIMO_ARENA_DATA || globalThis.ARENA_SCENARIOS.length < 6) process.exit(7);"
            "const repUp = JSON.stringify(browserWin.AIMO_ARENA_DATA);"
            "if (repUp.indexOf('replay://tests/fixtures/snapshot/replay') === -1) process.exit(8);"
        )
        proc = subprocess.run([node_bin, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


def test_arena_contract_files_have_zero_comments_and_zero_em_dashes() -> None:
    for py_path in (pathlib.Path(__file__).resolve(), RECORDER_PY):
        raw = py_path.read_text(encoding="utf-8")
        assert EM_DASH not in raw, f"Em dash found in {py_path.name}"
        assert EN_DASH not in raw, f"En dash found in {py_path.name}"
        assert HASH_CHAR not in raw, f"Hash char found in {py_path.name}"
        comments = [
            tok.string
            for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
            if tok.type == tokenize.COMMENT
        ]
        assert comments == [], f"Python comments found in {py_path.name}: {comments}"
