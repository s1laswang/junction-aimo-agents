from __future__ import annotations

import asyncio
import gzip
import json
import pathlib
import re
import xml.etree.ElementTree as ET

from aimo.buyer.costs import summarize
from aimo.gateway.mcp_server import build_mcp
from conformance import profile

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
    assert len(before_rows) == 55
    assert len(after_rows) == 50
    after_summary = summarize(after_rows)
    assert after_summary["cached"] == 57661
    assert after_summary["in"] == 183196
    assert round(after_summary["cached"] / after_summary["in"] * 100) == 31

    for doc_text in (gemini, numbers):
        assert "57,661" in doc_text
        assert "183,196" in doc_text
        assert "31%" in doc_text
        assert "4,255" in doc_text


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
