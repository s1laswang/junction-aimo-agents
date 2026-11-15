from __future__ import annotations

import io
import json
import os
import pathlib
import re
import subprocess
import tokenize
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
TRAILER_NEEDLE = "-".join(("co", "authored", "by"))
CONFLICT_START = "<" * 7
CONFLICT_MID = "=" * 7
CONFLICT_END = ">" * 7

FORBIDDEN_WORKSPACE_DIRS = (
    ".venv",
    "venv",
    "node_modules",
)

FORBIDDEN_TRACKED_DIR_NAMES = (
    ".venv",
    "venv",
    "node_modules",
    "keys",
    "data",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)

FORBIDDEN_TRACKED_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "approver_pin",
    "approver_pins.json",
    "approver_session_secret",
    "id_rsa",
    "id_ed25519",
}

FORBIDDEN_TRACKED_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pyc",
    ".pyo",
    ".webm",
}

REQUIRED_DOCS_JSON_FILES = (
    "docs/windtunnel-results.json",
    "docs/valio-mock-openapi.json",
    "docs/arena/assets/manifest.json",
    "docs/openness/conformance-valio-aimo.json",
    "docs/openness/conformance-nordic-bakery.json",
    "docs/evidence/live-run/audit-events.json",
    "docs/evidence/live-run/jwks.json",
    "docs/evidence/live-run/order.json",
    "docs/evidence/live-run/quote.json",
    "docs/evidence/live-run/run.json",
)

REQUIRED_PEPPOL_XML_FILES = (
    "docs/peppol/sample-order-response-advanced-AP.xml",
    "docs/peppol/sample-order-response-advanced-CA.xml",
    "docs/peppol/sample-order-response-advanced-RE.xml",
)


def _git_ref_exists(ref: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _assert_zero_conflict_markers(rel_path: str, text: str) -> None:
    for line_no, line in enumerate(text.splitlines(), 1):
        assert not line.startswith(CONFLICT_START), (
            f"Conflict start marker in {rel_path}:{line_no}"
        )
        assert not line.startswith(CONFLICT_END), (
            f"Conflict end marker in {rel_path}:{line_no}"
        )
        assert line.strip() != CONFLICT_MID, (
            f"Conflict separator marker in {rel_path}:{line_no}"
        )


def _tracked_files_under(prefix: str, suffix: str) -> list[str]:
    if (ROOT / ".git").exists():
        out = subprocess.check_output(
            ["git", "ls-files", prefix],
            cwd=ROOT,
            text=True,
        )
        return sorted(
            line.strip()
            for line in out.splitlines()
            if line.strip().endswith(suffix)
        )
    base = ROOT / prefix.rstrip("/")
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in base.rglob(f"*{suffix}")
        if p.is_file()
    )


def test_zero_co_authored_by_trailers_in_docs_commits_since_combined():
    if not (ROOT / ".git").exists():
        return

    head_msg = subprocess.check_output(
        ["git", "log", "-1", "--format=%H:%B", "HEAD"],
        cwd=ROOT,
        text=True,
    )
    assert TRAILER_NEEDLE not in head_msg.casefold(), (
        f"Forbidden trailer found on HEAD commit: {head_msg.splitlines()[0]}"
    )

    if not (_git_ref_exists("origin/combined") and _git_ref_exists("origin/main")):
        return

    ranges = ["origin/combined..HEAD"]
    if _git_ref_exists("origin/fr8/valio/docs"):
        ranges.append("origin/combined..origin/fr8/valio/docs")

    for rev_range in ranges:
        log_output = subprocess.check_output(
            ["git", "log", "--format=%H:%B", rev_range, "--not", "origin/main"],
            cwd=ROOT,
            text=True,
        )
        assert TRAILER_NEEDLE not in log_output.casefold(), (
            f"Forbidden trailer in git log {rev_range} --not origin/main"
        )

        record_output = subprocess.check_output(
            ["git", "log", "--format=%H%x00%s%x00%B%x1e", rev_range, "--not", "origin/main"],
            cwd=ROOT,
            text=True,
        )
        records = [rec.strip() for rec in record_output.split("\x1e") if rec.strip()]
        for rec in records:
            sha, subject, body = rec.split("\x00", 2)
            assert re.fullmatch(r"[0-9a-f]{40}", sha), f"Malformed commit SHA: {sha!r}"
            assert subject.strip(), f"Empty commit subject on {sha}"
            assert TRAILER_NEEDLE not in body.casefold(), (
                f"Forbidden trailer on commit {sha}"
            )


def test_zero_workspace_venvs_and_zero_forbidden_tracked_files():
    for dirname in FORBIDDEN_WORKSPACE_DIRS:
        candidate = ROOT / dirname
        assert not os.path.lexists(candidate), (
            f"Forbidden directory or symlink in repo root: {dirname}"
        )

    if not (ROOT / ".git").exists():
        return

    tracked = [
        line.strip()
        for line in subprocess.check_output(
            ["git", "ls-files"],
            cwd=ROOT,
            text=True,
        ).splitlines()
        if line.strip()
    ]
    assert len(tracked) >= 100

    for rel in tracked:
        rel_path = pathlib.Path(rel)
        parts = set(rel_path.parts[:-1])
        forbidden_dir_hits = parts.intersection(FORBIDDEN_TRACKED_DIR_NAMES)
        assert not forbidden_dir_hits, (
            f"Forbidden directory tracked in git ls-files ({sorted(forbidden_dir_hits)}): {rel}"
        )
        assert rel_path.name not in FORBIDDEN_TRACKED_FILENAMES, (
            f"Forbidden secret or runtime file tracked in git ls-files: {rel}"
        )
        assert rel_path.suffix.lower() not in FORBIDDEN_TRACKED_SUFFIXES, (
            f"Forbidden runtime or key artifact tracked in git ls-files: {rel}"
        )
        assert not (
            rel_path.parent == pathlib.Path(".")
            and rel_path.name.startswith("conformance-")
            and rel_path.suffix == ".json"
        ), f"Stray root conformance report tracked in git ls-files: {rel}"


def test_every_tracked_docs_json_is_valid_utf8_clean_and_em_dash_free():
    tracked_json = _tracked_files_under("docs/", ".json")
    assert set(REQUIRED_DOCS_JSON_FILES).issubset(set(tracked_json)), (
        f"Missing required docs/*.json files: {sorted(set(REQUIRED_DOCS_JSON_FILES) - set(tracked_json))}"
    )

    openness_json = [p for p in tracked_json if p.startswith("docs/openness/")]
    evidence_json = [p for p in tracked_json if p.startswith("docs/evidence/live-run/")]
    assert len(openness_json) >= 2
    assert len(evidence_json) >= 5

    parsed_by_rel: dict[str, object] = {}
    for rel in tracked_json:
        full_path = ROOT / rel
        assert full_path.is_file(), f"Tracked JSON missing on disk: {rel}"
        raw_bytes = full_path.read_bytes()
        assert len(raw_bytes) > 0, f"Empty JSON file: {rel}"
        text = raw_bytes.decode("utf-8")
        _assert_zero_conflict_markers(rel, text)
        assert EM_DASH not in text, f"Em dash found in {rel}"

        parsed = json.loads(text)
        assert isinstance(parsed, (dict, list)) and len(parsed) > 0, (
            f"Parsed JSON in {rel} must be a non-empty object or array"
        )
        parsed_by_rel[rel] = parsed

    windtunnel = parsed_by_rel["docs/windtunnel-results.json"]
    assert isinstance(windtunnel, dict) and {"main", "scale", "screen_eval"}.issubset(windtunnel)

    openapi = parsed_by_rel["docs/valio-mock-openapi.json"]
    assert isinstance(openapi, dict) and {"openapi", "info", "paths"}.issubset(openapi)

    manifest = parsed_by_rel["docs/arena/assets/manifest.json"]
    assert isinstance(manifest, dict) and {"schema_version", "license"}.issubset(manifest)


def test_every_tracked_peppol_xml_parses_via_elementtree_with_zero_conflict_markers():
    tracked_xml = _tracked_files_under("docs/peppol/", ".xml")
    assert set(REQUIRED_PEPPOL_XML_FILES).issubset(set(tracked_xml)), (
        f"Missing required docs/peppol/*.xml files: {sorted(set(REQUIRED_PEPPOL_XML_FILES) - set(tracked_xml))}"
    )

    cbc_ns = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}
    seen_codes: set[str] = set()

    for rel in tracked_xml:
        full_path = ROOT / rel
        assert full_path.is_file(), f"Tracked XML missing on disk: {rel}"
        raw_bytes = full_path.read_bytes()
        assert len(raw_bytes) > 0, f"Empty XML file: {rel}"
        text = raw_bytes.decode("utf-8")
        _assert_zero_conflict_markers(rel, text)
        assert EM_DASH not in text, f"Em dash found in {rel}"

        root_elem = ET.fromstring(text)
        assert root_elem.tag.endswith("OrderResponse"), (
            f"Unexpected root element {root_elem.tag!r} in {rel}"
        )
        resp_code = root_elem.findtext(".//cbc:OrderResponseCode", namespaces=cbc_ns)
        assert resp_code in {"AP", "CA", "RE"}, (
            f"Unexpected OrderResponseCode {resp_code!r} in {rel}"
        )
        seen_codes.add(resp_code)

    assert seen_codes == {"AP", "CA", "RE"}


def test_self_hygiene_zero_hash_comments_dashes_and_co_authored_by():
    self_text = pathlib.Path(__file__).resolve().read_text(encoding="utf-8")
    codepoints = set(map(ord, self_text))
    assert 35 not in codepoints
    assert EM_DASH not in self_text
    assert EN_DASH not in self_text
    assert TRAILER_NEEDLE not in self_text.casefold()
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(self_text).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
