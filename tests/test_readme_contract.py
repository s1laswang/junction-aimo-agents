import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README_PATH = ROOT / "README.md"
MAKEFILE_PATH = ROOT / "Makefile"
EM_DASH = "\u2014"
HASH_CHAR = "\u0023"

EXPLICIT_PITCH_DOCS = (
    "pitch/PITCH.md",
    "pitch/DEMO.md",
    "pitch/DECK.md",
    "pitch/NUMBERS.md",
)

VERIFIED_TOKENS = (
    "464119",
    "1,332.00 EUR",
    "1,512.00 EUR",
    "17,546",
    "6/6 checks",
    "/.well-known/agent-card.json",
    "/.well-known/jwks.json",
    "/openapi.json",
    "/mcp",
    "/health",
    "make smoke",
    "scripts/smoke_demo.py",
    "/v1/mandate",
    "pitch.measure_live --inprocess",
    "--url http://localhost:8080",
)


def parse_phony_targets(makefile_text: str) -> set[str]:
    targets: set[str] = set()
    joined = re.sub(r"\\\n\s*", " ", makefile_text)
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith(".PHONY:"):
            targets.update(stripped.split(":", 1)[1].split())
    return targets


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


def test_every_makefile_phony_target_in_readme():
    makefile_text = MAKEFILE_PATH.read_text(encoding="utf-8")
    readme_text = README_PATH.read_text(encoding="utf-8")
    phony_targets = parse_phony_targets(makefile_text)
    assert len(phony_targets) >= 11
    missing = [
        target
        for target in sorted(phony_targets)
        if not re.search(rf"(?:`{re.escape(target)}`|make\s+{re.escape(target)}\b)", readme_text)
    ]
    assert not missing, f"Makefile .PHONY targets missing from README.md: {missing}"


def test_every_required_doc_linked_in_readme():
    readme_text = README_PATH.read_text(encoding="utf-8")
    raw_targets = extract_relative_link_targets(readme_text)
    assert raw_targets

    resolved_links: set[Path] = set()
    broken_links: list[str] = []
    for target in raw_targets:
        resolved = (README_PATH.parent / target).resolve()
        if not resolved.is_file():
            broken_links.append(target)
        else:
            resolved_links.add(resolved)
    assert not broken_links, f"Broken relative links in README.md: {broken_links}"

    docs_files = sorted(p.resolve() for p in (ROOT / "docs").glob("*.md") if p.is_file())
    deploy_files = sorted(p.resolve() for p in (ROOT / "deploy").glob("*.md") if p.is_file())
    assert docs_files
    assert deploy_files

    contract_file = (ROOT / "CONTRACT.md").resolve()
    assert contract_file.is_file()

    pitch_files: list[Path] = []
    for rel in EXPLICIT_PITCH_DOCS:
        p = (ROOT / rel).resolve()
        assert p.is_file(), f"Missing required pitch doc: {rel}"
        pitch_files.append(p)

    required_docs = docs_files + deploy_files + [contract_file] + pitch_files
    unlinked = [
        str(doc.relative_to(ROOT))
        for doc in required_docs
        if doc not in resolved_links
    ]
    assert not unlinked, f"Required docs not linked in README.md: {unlinked}"


@pytest.mark.parametrize("token", VERIFIED_TOKENS)
def test_readme_contains_verified_token(token):
    readme_text = README_PATH.read_text(encoding="utf-8")
    assert token in readme_text, f"Missing verified token in README.md: {token!r}"


def test_readme_no_stale_counts_or_em_dashes():
    readme_text = README_PATH.read_text(encoding="utf-8")
    assert EM_DASH not in readme_text
    assert "5/5 checks" not in readme_text


def test_contract_helper_parsers():
    sample_makefile = ".PHONY: setup check \\\n  smoke unit\n.PHONY: demo\n"
    assert parse_phony_targets(sample_makefile) == {"setup", "check", "smoke", "unit", "demo"}

    sample_md = "\n".join(
        [
            "See [Arch](docs/ARCHITECTURE.md) and [Trust](docs/TRUST.md" + HASH_CHAR + "jws).",
            "Ignore `[Fake](docs/FAKE.md)` and [Site](https://example.com) and [Root](/console.html).",
            "```sh",
            "[CodeBlock](docs/CODE.md)",
            "```",
        ]
    )
    assert extract_relative_link_targets(sample_md) == ["docs/ARCHITECTURE.md", "docs/TRUST.md"]


def test_owned_files_hygiene():
    readme_text = README_PATH.read_text(encoding="utf-8")
    assert EM_DASH not in readme_text
    self_text = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in self_text
    assert HASH_CHAR not in self_text
