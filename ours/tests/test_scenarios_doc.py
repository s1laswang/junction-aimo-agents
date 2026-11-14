import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"
SCENARIOS_DOC = ROOT / "docs" / "SCENARIOS.md"
EM_DASH = "\u2014"
HASH_CHAR = "\u0023"

REQUIRED_CODES = (
    "BAD_SIGNATURE",
    "QUOTE_USED",
    "WRONG_AGENT",
    "NOT_APPROVER",
    "OVER_ORDER_LIMIT",
    "ALLERGEN_CONFLICT",
    "STOCKOUT",
    "WITHIN_MANDATE",
)

REQUIRED_LINKS = (
    "[Judging checklist](JUDGING.md)",
    "[Demo script](../pitch/DEMO.md)",
    "[Trust protocol](TRUST.md)",
)

REQUIRED_SUMMARY_COLUMNS = (
    "Scenario",
    "Customer(s)",
    "Judging criterion",
    "What it proves",
    "Key gateway/agent seams exercised",
)

REQUIRED_CLI_AND_AUDIT_TOKENS = (
    "make demo",
    ".venv/bin/python scenarios/run_all.py --base http://127.0.0.1:8081 --boot",
    "--base http://localhost:8080",
    "LLM_*",
    "hotel-aurora",
    "hospital-tays",
    "school-hervanta",
    "409",
    "403",
    "order.rejected",
    "order.blocked",
    "order.stockout",
    "approval.approved",
    "verified",
    "checkpoint.jws",
)

GATEWAY_CODE_SOURCES = (
    ROOT / "aimo" / "quotes.py",
    ROOT / "aimo" / "approvals.py",
    ROOT / "aimo" / "policy.py",
    ROOT / "aimo" / "orders.py",
)


def extract_relative_link_targets(markdown_text: str) -> list[str]:
    cleaned = re.sub(r"```[\s\S]*?```", "", markdown_text)
    cleaned = re.sub(r"`[^`]*`", "", cleaned)
    targets: list[str] = []
    for match in re.finditer(r"!?\[[^\]]+\]\(([^)]+)\)", cleaned):
        raw = match.group(1).strip().strip("<>").split()[0]
        if raw.startswith(("http://", "https://", "mailto:", HASH_CHAR, "/")):
            continue
        path_part = raw.split(HASH_CHAR, 1)[0]
        if path_part:
            targets.append(path_part)
    return targets


def test_every_scenario_script_on_disk_is_documented():
    assert SCENARIOS_DOC.is_file(), "Missing docs/SCENARIOS.md"
    doc_text = SCENARIOS_DOC.read_text(encoding="utf-8")

    scenario_files = sorted(
        p for p in SCENARIOS_DIR.glob("s*.py") if p.is_file()
    )
    assert len(scenario_files) >= 8

    h3_prefix = HASH_CHAR * 3 + " "
    table_lines = [line for line in doc_text.splitlines() if line.startswith("|")]
    heading_lines = [line for line in doc_text.splitlines() if line.startswith(h3_prefix)]

    for path in scenario_files:
        stem = path.stem
        filename = path.name
        assert stem in doc_text, f"Scenario {stem} missing from docs/SCENARIOS.md"
        assert filename in doc_text, f"Filename {filename} missing from docs/SCENARIOS.md"
        assert any(stem in row for row in table_lines), (
            f"Scenario {stem} missing from summary table in docs/SCENARIOS.md"
        )
        assert any(stem in heading for heading in heading_lines), (
            f"Scenario {stem} missing dedicated section heading in docs/SCENARIOS.md"
        )

    disk_stems = {p.stem for p in scenario_files}
    mentioned_stems = set(re.findall(r"\bs[0-9]+_[a-z0-9_]+\b", doc_text))
    unknown = sorted(mentioned_stems - disk_stems)
    assert not unknown, f"Unknown scenario names in docs/SCENARIOS.md: {unknown}"


def test_summary_table_columns_and_cli_tokens():
    doc_text = SCENARIOS_DOC.read_text(encoding="utf-8")
    for col in REQUIRED_SUMMARY_COLUMNS:
        assert col in doc_text, f"Missing summary table column: {col!r}"
    for token in REQUIRED_CLI_AND_AUDIT_TOKENS:
        assert token in doc_text, f"Missing required token in docs/SCENARIOS.md: {token!r}"


def test_zero_em_dashes_and_file_hygiene():
    doc_text = SCENARIOS_DOC.read_text(encoding="utf-8")
    assert EM_DASH not in doc_text, "Em dash found in docs/SCENARIOS.md"

    self_text = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in self_text, "Em dash found in tests/test_scenarios_doc.py"
    assert HASH_CHAR not in self_text, "Comment character found in tests/test_scenarios_doc.py"


def test_markdown_links_resolve_and_required_cross_links_exist():
    doc_text = SCENARIOS_DOC.read_text(encoding="utf-8")
    for required_link in REQUIRED_LINKS:
        assert required_link in doc_text, f"Missing required cross-link: {required_link}"

    targets = extract_relative_link_targets(doc_text)
    assert len(targets) >= len(REQUIRED_LINKS)

    broken: list[str] = []
    for target in targets:
        resolved = (SCENARIOS_DOC.parent / target).resolve()
        if not resolved.exists():
            broken.append(target)
    assert not broken, f"Broken relative markdown links in docs/SCENARIOS.md: {broken}"


@pytest.mark.parametrize("code", REQUIRED_CODES)
def test_key_error_and_reason_codes_present_and_grounded(code):
    doc_text = SCENARIOS_DOC.read_text(encoding="utf-8")
    assert code in doc_text, f"Code {code} missing from docs/SCENARIOS.md"

    gateway_blob = "\n".join(
        src.read_text(encoding="utf-8") for src in GATEWAY_CODE_SOURCES if src.is_file()
    )
    assert code in gateway_blob, f"Code {code} not found in gateway source modules"


def test_link_extractor_ignores_code_and_external_urls():
    sample = "\n".join(
        [
            "Check [Judging checklist](JUDGING.md) and [Demo](../pitch/DEMO.md" + HASH_CHAR + "scenes).",
            "Skip `[Inline](MISSING.md)` and [HTTP](http://localhost:8080) and [Root](/audit.html).",
            "```sh",
            "[Fenced](FENCED.md)",
            "```",
        ]
    )
    assert extract_relative_link_targets(sample) == ["JUDGING.md", "../pitch/DEMO.md"]
