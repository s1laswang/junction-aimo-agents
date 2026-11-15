from __future__ import annotations

import ast
import asyncio
import base64
import gzip
import hashlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import tokenize
import xml.etree.ElementTree as ET

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from aimo.buyer.costs import summarize
from aimo.gateway.mcp_server import build_mcp
from conformance import profile, verify as conf_verify

ROOT = pathlib.Path(__file__).resolve().parents[1]

EXTENSION_MCP_TOOLS = {
    "list_recipes",
    "plan_recipe",
    "check_standing_order_drift",
    "propose_offer",
}


def _read_text(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_snapshot_and_replay_meta_and_artifacts():
    snap_dir = ROOT / "tests" / "fixtures" / "snapshot"
    snap_meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    expected_snap_dates = ["2026-11-13", "2026-11-15", "2026-11-16", "2026-11-17"]
    assert snap_meta["n_ids"] == 92
    assert snap_meta["dates"] == expected_snap_dates
    for day in expected_snap_dates:
        price_blob = json.loads((snap_dir / f"price_{day}.json").read_text(encoding="utf-8"))
        avail_blob = json.loads((snap_dir / f"availability_{day}.json").read_text(encoding="utf-8"))
        assert len(price_blob["products"]) == 92
        assert len(avail_blob["products"]) == 92

    replay_dir = snap_dir / "replay"
    replay_meta = json.loads((replay_dir / "meta.json").read_text(encoding="utf-8"))
    expected_replay_dates = [
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
    ]
    assert replay_meta["n_ids"] == 669
    assert replay_meta["n_catalog_products"] == 669
    assert replay_meta["upstream_calls"] == 10
    assert replay_meta["dates"] == expected_replay_dates
    assert len(replay_meta["dates"]) * 2 == replay_meta["upstream_calls"]
    assert replay_meta["recorded_with"] == "scripts/record_replay.py"

    recorder = ROOT / replay_meta["recorded_with"]
    assert recorder.is_file()
    assert recorder.stat().st_size > 0

    for day in expected_replay_dates:
        p_gz = replay_dir / f"price_{day}.json.gz"
        a_gz = replay_dir / f"availability_{day}.json.gz"
        assert p_gz.is_file() and a_gz.is_file()
        p_data = json.loads(gzip.decompress(p_gz.read_bytes()))
        a_data = json.loads(gzip.decompress(a_gz.read_bytes()))
        assert len(p_data["products"]) == 669
        assert len(a_data["products"]) == 669

    catalog_dir = replay_dir / "catalog"
    prod_gz = catalog_dir / "valio_aimo_junction26_product_data.json.gz"
    co2_gz = catalog_dir / "valio_aimo_junction26_carbon_footprint.csv.gz"
    rel_gz = catalog_dir / "valio_aimo_junction26_delivery_reliability.csv.gz"
    assert prod_gz.is_file() and co2_gz.is_file() and rel_gz.is_file()
    assert co2_gz.stat().st_size > 0 and rel_gz.stat().st_size > 0
    cat_payload = json.loads(gzip.decompress(prod_gz.read_bytes()))
    cat_items = cat_payload["products"] if isinstance(cat_payload, dict) else cat_payload
    assert len(cat_items) == 669

    for doc_rel in ("README.md", "docs/NUMBERS.md"):
        doc_text = _read_text(doc_rel)
        assert "tests/fixtures/snapshot" in doc_text
        assert "92" in doc_text or "669" in doc_text


def test_mcp_vs_otap_profile_tool_contract():
    assert profile.PROFILE == "otap/0.1"
    expected_core = {
        "search_products",
        "get_product",
        "get_offers",
        "plan_need",
        "create_quote",
        "place_order",
        "get_order_status",
        "get_audit_trail",
        "list_policies",
    }
    assert len(profile.MCP_TOOLS) == 9
    assert set(profile.MCP_TOOLS) == expected_core

    tools = asyncio.run(build_mcp().list_tools())
    tool_map = {t.name: t for t in tools}
    tool_names = set(tool_map)
    assert len(tools) == 13
    assert len(tool_names) == 13
    assert expected_core.issubset(tool_names)
    assert tool_names - expected_core == EXTENSION_MCP_TOOLS
    assert tool_names.isdisjoint(set(profile.MCP_FORBIDDEN))
    approval_tools = [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()]
    assert approval_tools == []

    for core_name, req_args in profile.MCP_TOOLS.items():
        tool = tool_map[core_name]
        assert (tool.description or "").strip()
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        props = set(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        assert set(req_args).issubset(props)
        assert set(req_args).issubset(required)

    readme = _read_text("README.md")
    openness = _read_text("docs/OPENNESS.md")
    gemini = _read_text("docs/GEMINI.md")
    numbers = _read_text("docs/NUMBERS.md")

    for name in tool_names:
        assert name in readme
        assert name in openness
    assert "otap/0.1" in readme
    assert "otap/0.1" in openness
    assert "13" in openness and "no approval tool" in openness.lower()
    assert "9 core" in gemini and "13" in gemini
    assert "13 MCP tools" in numbers and "no approval tool" in numbers.lower()


def test_peppol_order_response_xml_samples():
    assert profile.PEPPOL_CODES == {"AP", "CA", "RE"}
    ns = {
        "ubl": "urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2",
        "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
        "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    }
    peppol_dir = ROOT / "docs" / "peppol"
    for code in ("AP", "CA", "RE"):
        xml_path = peppol_dir / f"sample-order-response-advanced-{code}.xml"
        assert xml_path.is_file()
        assert xml_path.stat().st_size > 0
        root = ET.parse(xml_path).getroot()
        assert root.tag == f"{{{ns['ubl']}}}OrderResponse"
        cust_id = root.find("cbc:CustomizationID", ns)
        prof_id = root.find("cbc:ProfileID", ns)
        resp_code = root.find("cbc:OrderResponseCode", ns)
        assert cust_id is not None and (cust_id.text or "").strip() == (
            "urn:fdc:peppol.eu:poacc:trns:order_response_advanced:3"
        )
        assert prof_id is not None and (prof_id.text or "").strip() == (
            "urn:fdc:peppol.eu:poacc:bis:advanced_ordering:3"
        )
        assert resp_code is not None and (resp_code.text or "").strip() == code
        order_lines = root.findall("cac:OrderLine", ns)
        if code in {"AP", "RE"}:
            assert order_lines == []
        else:
            assert len(order_lines) >= 1

    readme = _read_text("README.md")
    openness = _read_text("docs/OPENNESS.md")
    assert "docs/peppol/" in readme
    for code in ("AP", "CA", "RE"):
        assert code in readme
        assert code in openness


def test_gemini_section5_ledger_and_montage_arithmetic():
    assert 2586 + 831 + 493 + 345 == 4255
    assert 4187 + 68 == 4255
    assert round(57661 / 183196 * 100) == 31

    gemini = _read_text("docs/GEMINI.md")
    numbers = _read_text("docs/NUMBERS.md")
    sec5 = gemini.split("\x23\x23 5. Usage numbers", 1)[1].split("\x23\x23 6.", 1)[0]

    total_match = re.search(
        r"\*\*Total:\*\*\s*([\d,]+)\s+calls\s*\(\s*([\d,]+)\s+ok,\s*([\d,]+)\s+errors\s*\)",
        sec5,
    )
    assert total_match is not None
    total_calls = int(total_match.group(1).replace(",", ""))
    ok_calls = int(total_match.group(2).replace(",", ""))
    err_calls = int(total_match.group(3).replace(",", ""))
    assert (total_calls, ok_calls, err_calls) == (4255, 4187, 68)
    assert ok_calls + err_calls == total_calls

    model_match = re.search(
        r"By model:\s*gemini-3\.8-flash\s+([\d,]+)\s+calls;\s*"
        r"gemini-3\.1-flash-lite\s+([\d,]+);\s*"
        r"gemini-embedding-2\s+([\d,]+);\s*"
        r"gemini-4-hydrogen\s+([\d,]+)",
        sec5,
    )
    assert model_match is not None
    model_counts = tuple(int(g.replace(",", "")) for g in model_match.groups())
    assert model_counts == (2586, 831, 493, 345)
    assert sum(model_counts) == total_calls

    feature_calls: list[int] = []
    feature_rows = 0
    in_table = False
    for line in sec5.splitlines():
        if line.startswith("| Feature | Model | Calls |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(cells[0]) <= {"-"}:
                continue
            feature_rows += 1
            row_counts = [int(tok.replace(",", "")) for tok in re.findall(r"\d[\d,]*", cells[2])]
            assert row_counts
            feature_calls.extend(row_counts)

    assert feature_rows == 17
    assert sum(feature_calls) == 4255

    evidence_dir = ROOT / "docs" / "evidence"
    before_rows = [
        json.loads(ln)
        for ln in (evidence_dir / "montage-ledger-before-trim.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    after_rows = [
        json.loads(ln)
        for ln in (evidence_dir / "montage-ledger-after-trim.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    sun_rows = [
        json.loads(ln)
        for ln in (evidence_dir / "montage-ledger-sun.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(before_rows) == 55
    assert len(after_rows) == 50
    assert len(sun_rows) == 53

    before_summary = summarize(before_rows)
    assert round(before_summary["cost_usd"] / 5, 4) == 0.0822

    after_summary = summarize(after_rows)
    assert after_summary["cached"] == 57661
    assert after_summary["in"] == 183196
    assert round(after_summary["cached"] / after_summary["in"] * 100) == 31
    assert round(after_summary["cost_usd"] / 5, 4) == 0.0339

    sun_summary = summarize(sun_rows)
    assert sun_summary["calls"] == 53
    assert sun_summary["in"] == 225503
    assert sun_summary["cached"] == 86792
    assert round(sun_summary["cached"] / sun_summary["in"] * 100) == 38
    assert round(sun_summary["cost_usd"] / 5, 4) == 0.0352
    assert (sun_summary["escalated_runs"], sun_summary["runs"]) == (5, 6)
    assert (sun_summary["escalated_steps"], sun_summary["agent_steps"]) == (5, 34)
    assert round(sun_summary["escalated_steps"] / sun_summary["agent_steps"] * 100) == 15

    for doc_text in (gemini, numbers):
        assert "57,661" in doc_text
        assert "183,196" in doc_text
        assert "31%" in doc_text
        assert "4,255" in doc_text

    montage_md = _read_text("docs/demo-montage.md")
    evidence_readme = _read_text("docs/evidence/README.md")
    for doc_text in (montage_md, evidence_readme, numbers):
        for tok in ("$0.0352", "$0.0339", "225,503", "86,792", "38%", "15%", "53"):
            assert tok in doc_text


def test_openness_conformance_reports_valid_json():
    expected_tools = {t.name for t in asyncio.run(build_mcp().list_tools())}
    report_rels = (
        "docs/openness/conformance-valio-aimo.json",
        "docs/openness/conformance-nordic-bakery.json",
    )
    for rel in report_rels:
        path = ROOT / rel
        assert path.is_file()
        assert path.stat().st_size > 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["profile"] == "otap/0.1"
        assert set(payload["mcp_tools"]) == expected_tools
        assert payload["summary"] == {"pass": 44, "fail": 0, "skip": 0, "total": 44}
        assert len(payload["results"]) == 44
        assert all(item["status"] == "PASS" for item in payload["results"])

    openness = _read_text("docs/OPENNESS.md")
    numbers = _read_text("docs/NUMBERS.md")
    readme = _read_text("README.md")
    assert "conformance-valio-aimo.json" in openness
    assert "conformance-nordic-bakery.json" in openness
    for doc_text in (openness, numbers, readme):
        assert "44/44" in doc_text


def _pct1(val: float) -> str:
    return f"{val * 100:.1f}%"


def _by_sut(systems: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in systems:
        sut = (s.get("meta") or {}).get("sut")
        if not sut:
            sut = "baseline" if "naive" in (s.get("label") or "").lower() else "team"
        out[sut] = s
    return out


def _load_policy(customer_id: str) -> dict:
    path = ROOT / "aimo" / "trust" / "policies" / f"{customer_id}.yaml"
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["policy_id"] == customer_id
    return data


def _rules_by_type(policy: dict) -> dict[str, dict]:
    rules = policy["rules"]
    by_type = {r["type"]: r for r in rules}
    assert len(by_type) == len(rules)
    return by_type


def test_windtunnel_json_matches_documented_metrics():
    wt = json.loads(_read_text("docs/windtunnel-results.json"))

    wt640 = wt["main"]
    assert wt640["name"] == "wt640"
    assert wt640["scenarios"]["n"] == 640
    sys640 = _by_sut(wt640["systems"])
    gw640 = sys640["team"]
    nv640 = sys640["baseline"]

    assert gw640["n"] == 640
    assert nv640["n"] == 640
    assert gw640["policy_violations"] == 0
    assert gw640["allergen_violations"] == 0
    assert _pct1(gw640["escalation"]["recall"]) == "99.7%"
    assert _pct1(gw640["escalation"]["precision"]) == "69.5%"
    assert (gw640["escalation"]["tp"], gw640["escalation"]["fp"]) == (331, 145)
    assert _pct1(gw640["outcome_accuracy"]) == "74.8%"
    assert nv640["policy_violations"] == 372
    assert nv640["allergen_violations"] == 193
    assert _pct1(nv640["escalation"]["recall"]) == "17.8%"
    assert _pct1(nv640["escalation"]["precision"]) == "90.8%"
    assert gw640["injection"]["payloads"] == 80
    assert gw640["injection"]["blocked"] == 80
    assert _pct1(gw640["injection"]["block_rate"]) == "100.0%"
    assert (gw640["injection"]["false_positives"], gw640["injection"]["clean_requests"]) == (1, 560)

    pre640 = _by_sut(wt["main_before_s3"]["systems"])["team"]
    assert pre640["policy_violations"] == 1
    assert pre640["allergen_violations"] == 0
    assert _pct1(pre640["escalation"]["precision"]) == "54.0%"
    assert _pct1(pre640["escalation"]["recall"]) == "98.8%"
    assert _pct1(pre640["injection"]["block_rate"]) == "56.2%"

    wt_ev_dir = ROOT / "docs" / "evidence" / "windtunnel"
    for fname in (
        "README.md",
        "snapshot.tar.gz",
        "wt640_gem.jsonl.gz",
        "wt640_gem__pipeline__baseline.jsonl.gz",
        "wt640_gem__pipeline__team__final.jsonl.gz",
        "wt640_gem__pipeline__team__a99692f.jsonl.gz",
    ):
        fp = wt_ev_dir / fname
        assert fp.is_file() and fp.stat().st_size > 0, f"Missing windtunnel evidence {fname}"
    scen_lines = gzip.decompress((wt_ev_dir / "wt640_gem.jsonl.gz").read_bytes()).decode("utf-8").splitlines()
    assert len([ln for ln in scen_lines if ln.strip()]) == 640

    wt2560 = wt["scale"]
    assert wt2560["name"] == "wt2560"
    assert wt2560["scenarios"]["n"] == 2560
    sys2560 = _by_sut(wt2560["systems"])
    gw2560 = sys2560["team"]
    nv2560 = sys2560["baseline"]

    assert gw2560["n"] == 2560
    assert nv2560["n"] == 2560
    assert gw2560["policy_violations"] == 11
    assert nv2560["policy_violations"] == 1452
    assert gw2560["allergen_violations"] == 0
    assert nv2560["allergen_violations"] == 728
    assert _pct1(gw2560["escalation"]["recall"]) == "98.7%"
    assert _pct1(nv2560["escalation"]["recall"]) == "21.4%"
    assert _pct1(gw2560["escalation"]["precision"]) == "56.6%"
    assert _pct1(nv2560["escalation"]["precision"]) == "94.3%"

    se = wt["screen_eval"]
    assert _pct1(se["regex"]["recall"]) == "44.4%"
    assert _pct1(se["gemini"]["recall"]) == "94.4%"
    neg_per_kind = se["gemini"]["negatives"] // 2
    assert neg_per_kind == 60
    buyer_fp = se["gemini"]["fp_by_kind"]["request"]
    supplier_fp = se["gemini"]["fp_by_kind"]["marketing"]
    assert (buyer_fp, supplier_fp) == (42, 0)

    wt_md = _read_text("docs/windtunnel-results.md")
    numbers_md = _read_text("docs/NUMBERS.md")
    submission_md = _read_text("docs/SUBMISSION.md")

    for doc_name, doc_text in (
        ("docs/windtunnel-results.md", wt_md),
        ("docs/NUMBERS.md", numbers_md),
        ("docs/SUBMISSION.md", submission_md),
    ):
        for tok in ("wt640", "640", "372", "193", "17.8%", "90.8%", "56.2%", "wt2560", "728", "44.4%", "94.4%"):
            assert tok in doc_text, f"{doc_name} missing windtunnel metric {tok!r}"
        assert "2560" in doc_text or "2,560" in doc_text, f"{doc_name} missing wt2560 N"
        assert "1452" in doc_text or "1,452" in doc_text, f"{doc_name} missing wt2560 naive policy violations"
        assert re.search(r"\b11\b", doc_text), f"{doc_name} missing wt2560 gateway policy violations (11)"
        assert re.search(r"\b42(?:/|\s+of\s+)60\b", doc_text), f"{doc_name} missing 42/60 buyer FP"
        assert re.search(r"\b0(?:/|\s+of\s+)60\b", doc_text), f"{doc_name} missing 0/60 supplier FP"

    for doc_name, doc_text in (
        ("docs/windtunnel-results.md", wt_md),
        ("docs/NUMBERS.md", numbers_md),
    ):
        for tok in (
            "98.7%",
            "21.4%",
            "56.6%",
            "94.3%",
            "42/60",
            "0/60",
            "69.5%",
            "74.8%",
            "145",
            "68.2%",
            "73.4%",
            "99.7%",
            "100.0%",
            "80 of 80",
            "AIMO_SCREEN_GEMINI=1",
        ):
            assert tok in doc_text, f"{doc_name} missing windtunnel scale/screen metric {tok!r}"


def test_customer_policy_yamls_match_trust_doc_approval_matrix():
    hotel = _load_policy("hotel-aurora")
    school = _load_policy("school-tampere")
    hospital = _load_policy("hospital-meilahti")

    hotel_rules = _rules_by_type(hotel)
    school_rules = _rules_by_type(school)
    hospital_rules = _rules_by_type(hospital)

    assert (hotel_rules["spend_threshold"]["auto_max"], hotel_rules["spend_threshold"]["approval_max"]) == (500, 5000)
    assert (school_rules["spend_threshold"]["auto_max"], school_rules["spend_threshold"]["approval_max"]) == (300, 3000)
    assert (hospital_rules["spend_threshold"]["auto_max"], hospital_rules["spend_threshold"]["approval_max"]) == (1000, 10000)

    assert hotel_rules["allergen_block"]["excluded"] == []
    assert school_rules["allergen_block"]["excluded"] == ["milk", "nuts"]
    assert hospital_rules["allergen_block"]["excluded"] == ["gluten"]

    school_usual = [p["product_id"] for p in school["preferred_products"]]
    assert school_usual == ["470889"]
    assert hotel_rules["substitution"]["auto_approve_alternatives_below_eur"] == 50

    trust_md = _read_text("docs/TRUST.md")
    assumptions_md = _read_text("docs/ASSUMPTIONS.md")

    assert "auto_approve_alternatives_below_eur" in trust_md
    assert "€50" in trust_md

    matrix_rows: dict[str, str] = {}
    for line in trust_md.splitlines():
        for cid in ("hotel-aurora", "school-tampere", "hospital-meilahti"):
            if line.startswith(f"| `{cid}` |"):
                matrix_rows[cid] = line
    assert set(matrix_rows) == {"hotel-aurora", "school-tampere", "hospital-meilahti"}

    assert "€500" in matrix_rows["hotel-aurora"] and "5,000" in matrix_rows["hotel-aurora"]
    assert "463038" in matrix_rows["hotel-aurora"] and "464306" in matrix_rows["hotel-aurora"]

    assert "€300" in matrix_rows["school-tampere"] and "3,000" in matrix_rows["school-tampere"]
    assert "470889" in matrix_rows["school-tampere"]
    assert "milk" in matrix_rows["school-tampere"] and "nuts" in matrix_rows["school-tampere"]
    assert "472833" not in matrix_rows["school-tampere"].split("(")[0]

    assert "€1,000" in matrix_rows["hospital-meilahti"] and "10,000" in matrix_rows["hospital-meilahti"]
    assert "gluten" in matrix_rows["hospital-meilahti"]

    for fname in ("hotel-aurora.yaml", "school-tampere.yaml", "hospital-meilahti.yaml"):
        assert fname in assumptions_md
    assert "470889" in assumptions_md
    assert "milk and nuts" in assumptions_md
    assert "€500 / €5,000" in assumptions_md
    assert "€50" in assumptions_md


def test_injection_fp_repro_script_or_screening_zero_fp_documented():
    script = ROOT / "docs" / "pitch" / "repro_injection_fp.py"
    assert script.is_file()
    assert script.stat().st_size > 0
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(script))
    func_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "main" in func_names
    assert "screen_text" in source

    zero_fp_re = re.compile(r"\b0\s*(?:of|/)\s*34,159\b")
    nonzero_fp_re = re.compile(r"\b[1-9]\d*\s*(?:of|/)\s*34,159\b")

    for rel in ("docs/TRUST.md", "docs/NUMBERS.md", "docs/SUBMISSION.md"):
        doc_text = _read_text(rel)
        assert zero_fp_re.search(doc_text), f"{rel} missing '0 of 34,159' / '0 / 34,159'"
        assert not nonzero_fp_re.search(doc_text), f"{rel} claims non-zero FP over 34,159"

    numbers_md = _read_text("docs/NUMBERS.md")
    assert "docs/pitch/repro_injection_fp.py" in numbers_md
    assert "0 of 34,159" in numbers_md and "0 / 34,159" in numbers_md


def test_numbers_md_fallback_live_run_and_hygiene_contracts():
    numbers_md = _read_text("docs/NUMBERS.md")
    for tok in (
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash",
        "29 s",
        "scripts/verify_quote.py",
        "docs/evidence/live-run/quote.json",
        "docs/evidence/live-run/audit-events.json",
        "docs/evidence/live-run/jwks.json",
        "docs/evidence/live-run/summarize.py",
    ):
        assert tok in numbers_md, f"docs/NUMBERS.md missing {tok!r}"

    live_dir = ROOT / "docs" / "evidence" / "live-run"
    live_quote = json.loads((live_dir / "quote.json").read_text(encoding="utf-8"))
    live_order = json.loads((live_dir / "order.json").read_text(encoding="utf-8"))
    live_run = json.loads((live_dir / "run.json").read_text(encoding="utf-8"))
    assert live_quote["quote_id"] in numbers_md
    assert live_order["order_id"] in numbers_md
    assert live_run["run_id"] in numbers_md
    assert str(live_run["latency_s"]) in numbers_md

    for py_rel in (
        "docs/evidence/montage_stats.py",
        "docs/evidence/live-run/summarize.py",
        "tests/test_numbers_reproducibility.py",
        "tests/test_live_run_evidence.py",
    ):
        src = _read_text(py_rel)
        assert "\x23" not in src, f"Comment marker found in {py_rel}"
        assert "\u2014" not in src, f"Em dash found in {py_rel}"

    for md_rel in (
        "docs/NUMBERS.md",
        "docs/OPENNESS.md",
        "docs/demo-montage.md",
        "docs/windtunnel-results.md",
        "docs/evidence/README.md",
        "docs/evidence/windtunnel/README.md",
        "docs/evidence/live-run/README.md",
        "docs/evidence/live-run/transcript.md",
        "docs/evidence/live-run/audit-report.md",
    ):
        assert "\u2014" not in _read_text(md_rel), f"Em dash found in {md_rel}"


    for json_rel in (
        "docs/evidence/live-run/quote.json",
        "docs/evidence/live-run/audit-events.json",
        "docs/evidence/live-run/order.json",
        "docs/evidence/live-run/jwks.json",
    ):
        assert "\u2014" not in _read_text(json_rel), f"Em dash found in {json_rel}"

    self_src = _read_text("tests/test_numbers_reproducibility.py")
    comment_tokens = [
        tok
        for tok in tokenize.generate_tokens(io.StringIO(self_src).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comment_tokens == []


def test_live_run_crypto_chain_order_binding_and_scenario_92_contract():
    live_dir = ROOT / "docs" / "evidence" / "live-run"
    jwks = json.loads((live_dir / "jwks.json").read_text(encoding="utf-8"))
    quote = json.loads((live_dir / "quote.json").read_text(encoding="utf-8"))
    order = json.loads((live_dir / "order.json").read_text(encoding="utf-8"))
    events = json.loads((live_dir / "audit-events.json").read_text(encoding="utf-8"))
    run = json.loads((live_dir / "run.json").read_text(encoding="utf-8"))

    jwk = jwks["keys"][0]
    jwk_canon = conf_verify.canonical_json({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]})
    recomputed_kid = conf_verify.b64e(hashlib.sha256(jwk_canon.encode("utf-8")).digest())
    assert recomputed_kid == conf_verify.thumbprint(jwk) == jwk["kid"] == quote["kid"]
    pub_bytes = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)

    quote_core = {k: v for k, v in quote.items() if k not in ("quote_hash", "signature", "kid")}
    recomputed_qhash = hashlib.sha256(conf_verify.canonical_json(quote_core).encode("utf-8")).hexdigest()
    assert recomputed_qhash == quote["quote_hash"] == conf_verify.quote_hash(quote)

    qheader, qpayload = conf_verify.verify_jws(quote["signature"], jwks)
    assert qheader == {"alg": "EdDSA", "kid": jwk["kid"], "typ": "quote+jws"}
    assert qpayload["typ"] == "quote"
    assert qpayload["quote_id"] == quote["quote_id"] == "Q-20261115-964294b6"
    assert qpayload["quote_hash"] == recomputed_qhash
    assert qpayload["total_net"] == 1010

    q_res = conf_verify.verify_quote(quote, jwks)
    assert q_res == {
        "ok": True,
        "hash_matches": True,
        "signature_valid": True,
        "quote_hash": recomputed_qhash,
        "reasons": [],
    }
    qhash = q_res["quote_hash"]

    qh64, qp64, qs64 = quote["signature"].split(".")
    qsig = bytearray(base64.urlsafe_b64decode(qs64 + "=" * (-len(qs64) % 4)))
    pub_key.verify(bytes(qsig), f"{qh64}.{qp64}".encode("ascii"))
    qsig[0] ^= 0x01
    with pytest.raises(ValueError):
        conf_verify.verify_jws(f"{qh64}.{qp64}.{conf_verify.b64e(bytes(qsig))}", jwks)
    other_pub = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    with pytest.raises(ValueError):
        conf_verify.verify_jws(quote["signature"], {"keys": [{**jwk, "x": conf_verify.b64e(other_pub)}]})
    none_hdr = conf_verify.b64e(conf_verify.canonical_json({"alg": "none", "kid": jwk["kid"]}).encode("utf-8"))
    with pytest.raises(ValueError):
        conf_verify.verify_jws(f"{none_hdr}.{qp64}.{qs64}", jwks)

    ev_res = conf_verify.verify_events(events, jwks, contiguous=True)
    assert ev_res == {"ok": True, "checked": 6, "problems": []}
    assert events[0]["prev_hash"] == "0" * 64
    for idx, ev in enumerate(events):
        if idx > 0:
            assert ev["prev_hash"] == events[idx - 1]["hash"]
        core = {k: v for k, v in ev.items() if k not in ("hash", "signature", "kid")}
        recomputed_ev_hash = hashlib.sha256(
            (ev["prev_hash"] + conf_verify.canonical_json(core)).encode("utf-8")
        ).hexdigest()
        assert recomputed_ev_hash == ev["hash"] == conf_verify.event_hash(ev)
        eheader, epayload = conf_verify.verify_jws(ev["signature"], jwks)
        assert eheader == {"alg": "EdDSA", "kid": jwk["kid"], "typ": "audit+jws"}
        assert epayload == {"typ": "audit-event", "seq": ev["seq"], "hash": ev["hash"]}
        eh64, ep64, es64 = ev["signature"].split(".")
        esig = base64.urlsafe_b64decode(es64 + "=" * (-len(es64) % 4))
        pub_key.verify(esig, f"{eh64}.{ep64}".encode("ascii"))
        assert ev["data"]["quote_hash"] == qhash

    assert order["quote_id"] == quote["quote_id"]
    assert order["quote_hash"] == qhash
    assert order["status"] == "placed"
    assert order["response_code"] == "AP"
    assert run["quote"]["quote_hash"] == qhash
    assert run["order"]["quote_hash"] == qhash
    assert events[3]["action"] == "approval.verified"
    assert events[3]["data"]["quote_hash"] == order["quote_hash"] == qhash
    assert events[3]["data"]["approver"] == order["approved_by"]
    assert events[5]["action"] == "order.placed"
    assert events[5]["data"]["order"]["quote_hash"] == order["quote_hash"] == qhash

    numbers_md = _read_text("docs/NUMBERS.md")
    openness_md = _read_text("docs/OPENNESS.md")
    scenarios_md = _read_text("docs/SCENARIOS.md")

    for doc_rel, doc_text in (("docs/NUMBERS.md", numbers_md), ("docs/OPENNESS.md", openness_md)):
        doc_dir = (ROOT / doc_rel).parent
        for target_rel in ("evidence/live-run/README.md", "evidence/windtunnel/README.md"):
            assert f"({target_rel})" in doc_text, f"{doc_rel} missing link to {target_rel}"
            resolved = (doc_dir / target_rel).resolve()
            assert resolved.is_file() and resolved.stat().st_size > 0

    openness_sec5 = openness_md.split("\x23\x23 5. Verify offline", 1)[1].split("\x23\x23 ", 1)[0]
    for sec5_tok in (
        "docs/evidence/live-run/",
        "docs/evidence/live-run/quote.json",
        "docs/evidence/live-run/audit-events.json",
        "docs/evidence/live-run/jwks.json",
        "scripts/verify_quote.py",
        "VERIFIED",
        "02575d8",
        "62e8384",
    ):
        assert sec5_tok in openness_sec5, f"docs/OPENNESS.md section 5 missing {sec5_tok!r}"

    scen_rows = [
        int(m.group(1))
        for m in re.finditer(r"\|\s*PASS\s*\|\s*s[0-7]_[a-z_]+\s*\|[^|]+\|\s*(\d+)\s*\|", scenarios_md)
    ]
    assert scen_rows == [12, 11, 11, 9, 8, 14, 14, 13]
    assert sum(scen_rows) == 92

    for num_tok in (
        "8/8 PASS (92 checks",
        "92 checks",
        "88 checks",
        "`s0` 12, `s1` 11, `s2` 11, `s3` 9, `s4` 8, `s5` 14, `s6` 14, `s7` 13",
        "375 passed, 7 skipped, 1 xfailed, 1 xpassed",
        "274 passed, 0 failed",
        "1,168 passed",
        "1150 passed",
        "743 passed",
        "2,448",
        "1d0b238",
        "02575d8",
        "62e8384",
        "PASS=17 FAIL=0 SKIP=3",
        "PASS=16 FAIL=0 SKIP=4",
        "demo-walkthrough.mp4",
        "build_walkthrough.sh",
        "ffprobe",
        "2,251,113",
        "109.72 s",
        "110 s",
        "1280x720",
        "yuv420p",
        "44,100 Hz",
        "f3c5996",
    ):
        assert num_tok in numbers_md, f"docs/NUMBERS.md missing {num_tok!r}"

    mp4_path = ROOT / "docs" / "pitch" / "demo-walkthrough.mp4"
    assert mp4_path.is_file()
    assert mp4_path.stat().st_size == 2_251_113
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        probe = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,width,height,sample_rate,channels",
                "-show_entries",
                "format=duration,size",
                "-of",
                "json",
                str(mp4_path),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        pinfo = json.loads(probe.stdout)
        assert int(pinfo["format"]["size"]) == 2_251_113
        dur = float(pinfo["format"]["duration"])
        assert round(dur, 2) == 109.72
        assert round(dur) == 110
        vstreams = [s for s in pinfo["streams"] if s.get("codec_name") == "h264"]
        astreams = [s for s in pinfo["streams"] if s.get("codec_name") == "aac"]
        assert len(vstreams) == 1 and (vstreams[0]["width"], vstreams[0]["height"]) == (1280, 720)
        assert len(astreams) == 1 and (int(astreams[0]["sample_rate"]), int(astreams[0]["channels"])) == (44100, 1)

    passed_tokens = re.findall(r"\b(\d+)\s+passed\b", numbers_md)
    assert passed_tokens and int(passed_tokens[0]) == 1150


