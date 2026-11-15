from __future__ import annotations

import gzip
import io
import json
import pathlib
import re
import tarfile
import tokenize
import xml.etree.ElementTree as ET
from collections import Counter

from aimo.backend.peppol import (
    CUSTOMIZATION_ID,
    NS_CAC,
    NS_CBC,
    PROFILE_ID,
    check_t116_rules,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
HASH_CHAR = chr(35)
PNG_SIG = b"\x89PNG\r\n\x1a\n"

WAVE14_DOCS = (
    "docs/peppol/README.md",
    "docs/evidence/windtunnel/README.md",
    "docs/reports/frontend.md",
    "docs/reports/ui.md",
    "docs/reports/port.md",
    "docs/reports/trust.md",
)

WAVE14_REPORT_DOCS = (
    "docs/reports/frontend.md",
    "docs/reports/ui.md",
    "docs/reports/port.md",
    "docs/reports/trust.md",
)

REQUIRED_SURFACE_TOKENS = (
    "One-Page Judge Walkthrough",
    "Try to Scam the Agent",
    "docs/demo/index.html",
    "docs/arena/index.html",
    "docs/demo/ASSETS.md",
    "CC0-1.0",
    "Kenney",
    "manifest.json",
)


def _read_doc(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing doc: {rel_path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"Empty doc: {rel_path}"
    return text


def _extract_resolved_links(
    rel_path: str,
    *,
    strip_inline_code: bool = True,
) -> list[tuple[str, pathlib.Path]]:
    doc_path = ROOT / rel_path
    text = _read_doc(rel_path)
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    if strip_inline_code:
        stripped = re.sub(r"`[^`\n]+`", "", stripped)
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    out: list[tuple[str, pathlib.Path]] = []
    for raw_target in link_re.findall(stripped):
        target = raw_target.strip().split()[0]
        if target.startswith(("http://", "https://", "mailto:", "data:", HASH_CHAR)):
            continue
        clean = target.split(HASH_CHAR, 1)[0].split("?", 1)[0]
        if not clean:
            continue
        out.append((raw_target, (doc_path.parent / clean).resolve()))
    return out


def test_wave14_docs_exist_and_have_zero_em_dashes_todos_or_conflicts():
    todo_re = re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE)
    freeze_re = re.compile(r"filled at(?:\s+the|\s+sun\s+06:00)?\s+freeze", re.IGNORECASE)
    conflict_start = "<" * 7 + " "
    conflict_mid = "=" * 7
    conflict_end = ">" * 7 + " "

    for rel in WAVE14_DOCS:
        text = _read_doc(rel)
        assert EM_DASH not in text, f"Em dash found in {rel}"
        assert todo_re.findall(text) == [], f"TODO/FIXME marker in {rel}"
        assert freeze_re.findall(text) == [], f"Freeze placeholder in {rel}"
        for idx, line in enumerate(text.splitlines(), 1):
            assert not line.startswith(conflict_start), f"Conflict start marker in {rel}:{idx}"
            assert line.strip() != conflict_mid, f"Conflict separator marker in {rel}:{idx}"
            assert not line.startswith(conflict_end), f"Conflict end marker in {rel}:{idx}"


def test_wave14_docs_crosslink_walkthrough_arena_and_cc0_ledger():
    arena_dir = (ROOT / "docs" / "arena").resolve()
    demo_index = (ROOT / "docs" / "demo" / "index.html").resolve()
    assets_md = (ROOT / "docs" / "demo" / "ASSETS.md").resolve()
    manifest_json = (ROOT / "docs" / "arena" / "assets" / "manifest.json").resolve()

    assert arena_dir.is_dir()
    assert demo_index.is_file() and demo_index.stat().st_size > 0
    assert assets_md.is_file() and assets_md.stat().st_size > 0
    assert manifest_json.is_file() and manifest_json.stat().st_size > 0

    manifest = json.loads(manifest_json.read_text(encoding="utf-8"))
    assert manifest.get("license") == "CC0-1.0"
    assert manifest.get("author") == "Kenney Vleugels (Kenney.nl)"
    packs = manifest.get("packs") or {}
    assert len(packs) == 4
    for pack_meta in packs.values():
        assert pack_meta.get("license") == "CC0-1.0"
        assert pack_meta.get("author") == "Kenney Vleugels (Kenney.nl)"

    pngs = sorted((ROOT / "docs" / "arena" / "assets").glob("*.png"))
    assert len(pngs) == 24
    for png in pngs:
        assert png.stat().st_size > 0
        assert png.read_bytes()[:8] == PNG_SIG

    arena_targets = {arena_dir, (arena_dir / "index.html").resolve()}

    for rel in WAVE14_DOCS:
        text = _read_doc(rel)
        for token in REQUIRED_SURFACE_TOKENS:
            assert token in text, f"{rel} missing required cross-link token {token!r}"
        resolved_set = {resolved for _, resolved in _extract_resolved_links(rel)}
        assert demo_index in resolved_set, f"{rel} missing relative link to docs/demo/index.html"
        assert resolved_set & arena_targets, f"{rel} missing relative link to docs/arena/"
        assert assets_md in resolved_set, f"{rel} missing relative link to docs/demo/ASSETS.md"
        assert manifest_json in resolved_set, (
            f"{rel} missing relative link to docs/arena/assets/manifest.json"
        )

    demo_html = demo_index.read_text(encoding="utf-8")
    assert "<cbc:OrderResponseCode>AP</cbc:OrderResponseCode>" in demo_html
    peppol_readme = _read_doc("docs/peppol/README.md")
    assert "<cbc:OrderResponseCode>AP</cbc:OrderResponseCode>" in peppol_readme


def test_wave14_docs_strict_relative_links_resolve_without_pending_exemptions():
    root_resolved = ROOT.resolve()
    expected_raw_links = {
        "docs/peppol/README.md": {
            "sample-order-response-advanced-AP.xml",
            "sample-order-response-advanced-CA.xml",
            "sample-order-response-advanced-RE.xml",
            "../evidence/live-run/order.json",
            "../evidence/live-run/README.md",
            "../demo/index.html",
            "../arena/",
            "../demo/ASSETS.md",
            "../arena/assets/manifest.json",
        },
        "docs/evidence/windtunnel/README.md": {
            "../../demo/index.html",
            "../../arena/",
            "../../demo/ASSETS.md",
            "../../arena/assets/manifest.json",
            "../README.md",
            "../live-run/README.md",
        },
        "docs/reports/frontend.md": {
            "frontend/live-tamper-red.png",
            "frontend/live-restore-green.png",
            "../demo/index.html",
            "../arena/",
            "../demo/ASSETS.md",
            "../arena/assets/manifest.json",
        },
        "docs/reports/ui.md": {
            "../demo/index.html",
            "../arena/",
            "../demo/ASSETS.md",
            "../arena/assets/manifest.json",
        },
        "docs/reports/port.md": {
            "../demo/index.html",
            "../arena/",
            "../demo/ASSETS.md",
            "../arena/assets/manifest.json",
        },
        "docs/reports/trust.md": {
            "../demo/index.html",
            "../arena/",
            "../demo/ASSETS.md",
            "../arena/assets/manifest.json",
        },
    }

    for rel in WAVE14_DOCS:
        for strip_inline in (True, False):
            links = _extract_resolved_links(rel, strip_inline_code=strip_inline)
            assert links, f"Expected markdown relative links in {rel}"
            found_raw = {raw.strip().split()[0] for raw, _ in links}
            assert expected_raw_links[rel].issubset(found_raw), (
                f"{rel} missing expected relative links: {sorted(expected_raw_links[rel] - found_raw)}"
            )
            for raw_target, resolved in links:
                assert resolved.is_relative_to(root_resolved), (
                    f"Relative link escapes repo root in {rel}: {raw_target}"
                )
                assert resolved.exists(), f"Broken relative link in {rel}: {raw_target}"
                if resolved.is_file():
                    assert resolved.stat().st_size > 0, f"Empty linked file in {rel}: {raw_target}"
                    if resolved.suffix.lower() == ".png":
                        assert resolved.read_bytes()[:8] == PNG_SIG, (
                            f"Invalid PNG header in linked image {rel}: {raw_target}"
                        )


def test_peppol_readme_and_sample_xml_validity_and_t116_rules():
    ns = {
        "ubl": "urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2",
        "cac": NS_CAC,
        "cbc": NS_CBC,
    }
    peppol_dir = ROOT / "docs" / "peppol"
    expected_line_counts = {"AP": 0, "CA": 2, "RE": 0}

    for code, expected_lines in expected_line_counts.items():
        xml_path = peppol_dir / f"sample-order-response-advanced-{code}.xml"
        assert xml_path.is_file() and xml_path.stat().st_size > 0
        raw_xml = xml_path.read_text(encoding="utf-8")
        assert EM_DASH not in raw_xml
        assert check_t116_rules(raw_xml) == []
        root = ET.fromstring(raw_xml.encode("utf-8"))
        assert root.tag == f"{{{ns['ubl']}}}OrderResponse"
        assert (root.findtext("cbc:CustomizationID", "", ns)).strip() == CUSTOMIZATION_ID
        assert (root.findtext("cbc:ProfileID", "", ns)).strip() == PROFILE_ID
        assert (root.findtext("cbc:OrderResponseCode", "", ns)).strip() == code
        lines = root.findall("cac:OrderLine", ns)
        assert len(lines) == expected_lines
        if code == "CA":
            statuses = [
                li.findtext("cbc:LineStatusCode", "", ns)
                for li in root.findall("cac:OrderLine/cac:LineItem", ns)
            ]
            assert statuses == ["5", "3"]

    live_order = json.loads(
        (ROOT / "docs" / "evidence" / "live-run" / "order.json").read_text(encoding="utf-8")
    )
    assert live_order["order_id"] == "ORD-20261115-06eda13d"
    assert live_order["quote_id"] == "Q-20261115-964294b6"
    assert live_order["response_code"] == "AP"

    peppol_readme = _read_doc("docs/peppol/README.md")
    for tok in (
        CUSTOMIZATION_ID,
        PROFILE_ID,
        "GET /v1/orders/{order_id}/peppol-order-response.xml",
        "PEPPOL-T116-R001 to R010",
        "AIMO_UBL_XSD",
        "ORD-20261115-06eda13d",
        "Q-20261115-964294b6",
        "scripts/verify_quote.py",
    ):
        assert tok in peppol_readme, f"docs/peppol/README.md missing {tok!r}"


def test_windtunnel_evidence_gz_archives_and_readme_metric_parity():
    wt_dir = ROOT / "docs" / "evidence" / "windtunnel"
    snap_tar = wt_dir / "snapshot.tar.gz"
    assert snap_tar.is_file() and snap_tar.stat().st_size > 0
    with tarfile.open(snap_tar, "r:gz") as tf:
        names = tf.getnames()
    assert len(names) >= 15
    assert any(n.startswith("snapshot/price_") and n.endswith(".json") for n in names)
    assert any(n.startswith("snapshot/availability_") and n.endswith(".json") for n in names)

    scen_gz = wt_dir / "wt640_gem.jsonl.gz"
    assert scen_gz.is_file() and scen_gz.stat().st_size > 0
    scen_rows = [
        json.loads(ln)
        for ln in gzip.decompress(scen_gz.read_bytes()).decode("utf-8").splitlines()
        if ln.strip()
    ]
    assert len(scen_rows) == 640
    fam_counts = Counter(row["family"] for row in scen_rows)
    assert len(fam_counts) == 16
    assert set(fam_counts.values()) == {40}

    for run_fname in (
        "wt640_gem__pipeline__team__final.jsonl.gz",
        "wt640_gem__pipeline__baseline.jsonl.gz",
        "wt640_gem__pipeline__team__a99692f.jsonl.gz",
    ):
        gz_path = wt_dir / run_fname
        assert gz_path.is_file() and gz_path.stat().st_size > 0
        rows = [
            json.loads(ln)
            for ln in gzip.decompress(gz_path.read_bytes()).decode("utf-8").splitlines()
            if ln.strip()
        ]
        assert len(rows) == 640
        assert all("customer_id" in r and "family" in r for r in rows)

    wt_json = json.loads((ROOT / "docs" / "windtunnel-results.json").read_text(encoding="utf-8"))
    main_team = next(s for s in wt_json["main"]["systems"] if (s.get("meta") or {}).get("sut") == "team")
    assert main_team["policy_violations"] == 0
    assert main_team["allergen_violations"] == 0
    assert (main_team["escalation"]["tp"], main_team["escalation"]["fp"]) == (331, 145)
    assert f"{wt_json['screen_eval']['regex']['recall'] * 100:.1f}%" == "44.4%"
    assert f"{wt_json['screen_eval']['gemini']['recall'] * 100:.1f}%" == "94.4%"

    wt_readme = _read_doc("docs/evidence/windtunnel/README.md")
    for tok in (
        "wt640",
        "640",
        "99.7%",
        "69.5%",
        "100%",
        "372",
        "193",
        "17.8%",
        "90.8%",
        "331",
        "145",
        "476",
        "68.2%",
        "44%",
        "44.4%",
        "94.4%",
        "AIMO_SCREEN_GEMINI=1",
        "docs/windtunnel-results.json",
        "docs/windtunnel-results.md",
        "wt640_gem.jsonl.gz",
        "snapshot.tar.gz",
        "wt640_gem__pipeline__team__final.jsonl.gz",
        "wt640_gem__pipeline__baseline.jsonl.gz",
        "wt640_gem__pipeline__team__a99692f.jsonl.gz",
        "auto_approve_alternatives_below_eur",
        "aimo/trust/learned.py",
    ):
        assert tok in wt_readme, f"docs/evidence/windtunnel/README.md missing {tok!r}"


def test_wave14_reports_post_freeze_fr8_guardrail_and_mcp_parity_notes():
    fr8_heading = HASH_CHAR * 2 + " Post-freeze FR8 sync and zero-key browser surfaces"
    for rel in WAVE14_REPORT_DOCS:
        text = _read_doc(rel)
        assert fr8_heading in text, f"{rel} missing {fr8_heading!r}"
        assert "agent_cannot_approve" in text, f"{rel} missing 'agent_cannot_approve'"

    frontend = _read_doc("docs/reports/frontend.md")
    assert "X-Approver-Session" in frontend
    assert "check_approval" in frontend

    ui = _read_doc("docs/reports/ui.md")
    assert "X-Approver-Session" in ui
    assert "X-Approver-Pin" in ui
    assert "requote_needed" in ui

    port = _read_doc("docs/reports/port.md")
    assert "13 MCP tools" in port
    assert "0 approval tools" in port
    assert "44/44" in port
    assert "92 checks" in port

    trust = _read_doc("docs/reports/trust.md")
    assert "PlanLine.injection_screen" in trust
    assert "need.product_query" in trust
    assert "69.5%" in trust
    assert "99.7%" in trust


def test_self_zero_comments_zero_hash_and_zero_em_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    assert EN_DASH not in raw
    assert HASH_CHAR not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
