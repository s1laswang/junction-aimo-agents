from __future__ import annotations

import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
HASH = chr(35)
EM_DASH = chr(0x2014)

REQUIRED_DOCKER_RUNTIME_DIRS = (
    "aimo",
    "scripts",
    "web",
    "scenarios",
    "conformance",
    "toywholesaler",
)

REQUIRED_IGNORE_ENTRIES = {
    ".env",
    "data/",
    "keys/",
    "/conformance-*.json",
}

REQUIRED_MAKE_TARGETS = {
    "accept",
    "serve",
    "test",
    "fetch-data",
    "conformance",
    "mcp-check",
}

EXPECTED_TEAM_BRANCHES = (
    "team/buyer",
    "team/catalog",
    "team/frontend",
    "team/gateway",
    "team/harden",
    "team/need",
    "team/pitch",
    "team/port",
    "team/replay",
    "team/submit",
    "team/trust",
    "team/ui",
    "team/windtunnel",
)

EXPECTED_REMOTE_BRANCHES = (
    *EXPECTED_TEAM_BRANCHES,
    "justSilas-aimo-integration",
)


def _read_text(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def _dockerfile_instructions(text: str) -> list[str]:
    instructions: list[str] = []
    buf: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(HASH):
            continue
        if stripped.endswith("\\"):
            buf.append(stripped[:-1].strip())
            continue
        buf.append(stripped)
        instructions.append(" ".join(" ".join(buf).split()))
        buf.clear()
    if buf:
        instructions.append(" ".join(" ".join(buf).split()))
    return instructions


def _ignore_entries(rel_path: str) -> set[str]:
    entries: set[str] = set()
    for raw_line in _read_text(rel_path).splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(HASH):
            continue
        entries.add(stripped)
    return entries


def test_dockerfile_copies_snapshot_and_runtime_packages():
    instructions = _dockerfile_instructions(_read_text("Dockerfile"))
    assert "COPY tests/fixtures/snapshot tests/fixtures/snapshot" in instructions
    assert (ROOT / "tests" / "fixtures" / "snapshot" / "meta.json").is_file()
    assert (ROOT / "tests" / "fixtures" / "snapshot" / "replay" / "meta.json").is_file()

    for pkg in REQUIRED_DOCKER_RUNTIME_DIRS:
        assert (ROOT / pkg).is_dir(), f"Missing runtime directory copied by Dockerfile: {pkg}"
        assert f"COPY {pkg} {pkg}" in instructions, f"Dockerfile missing COPY {pkg} {pkg}"


def test_dockerfile_sets_non_root_user_port_healthcheck_and_never_copies_secrets():
    instructions = _dockerfile_instructions(_read_text("Dockerfile"))
    user_instructions = [inst for inst in instructions if inst.startswith("USER ")]
    assert user_instructions, "Dockerfile must set a non-root USER"
    assert user_instructions[-1] == "USER aimo"
    assert "EXPOSE 8000" in instructions
    assert any(
        inst.startswith("HEALTHCHECK ") and "8000/healthz" in inst
        for inst in instructions
    ), "Dockerfile must configure HEALTHCHECK against port 8000 /healthz"

    copy_or_add = [
        inst for inst in instructions if inst.startswith(("COPY ", "ADD "))
    ]
    assert copy_or_add
    forbidden_roots = {".", ".env", "keys", "data", ".git", ".venv"}
    for inst in copy_or_add:
        tokens = [tok for tok in inst.split()[1:] if not tok.startswith("--")]
        assert len(tokens) >= 2, f"Malformed COPY/ADD instruction: {inst}"
        sources = tokens[:-1]
        for src in sources:
            clean = src.rstrip("/")
            assert clean not in forbidden_roots, f"Dockerfile must never copy {src!r}: {inst}"
            assert not clean.startswith(".env"), f"Dockerfile must never copy dotenv file {src!r}: {inst}"
            assert not clean.startswith("keys/"), f"Dockerfile must never copy keys path {src!r}: {inst}"
            assert not clean.endswith(".pem"), f"Dockerfile must never copy PEM key {src!r}: {inst}"


def test_gitignore_and_dockerignore_contain_required_entries():
    gitignore_entries = _ignore_entries(".gitignore")
    dockerignore_entries = _ignore_entries(".dockerignore")

    missing_git = REQUIRED_IGNORE_ENTRIES - gitignore_entries
    missing_docker = REQUIRED_IGNORE_ENTRIES - dockerignore_entries
    assert missing_git == set(), f".gitignore missing required entries: {sorted(missing_git)}"
    assert missing_docker == set(), f".dockerignore missing required entries: {sorted(missing_docker)}"


def test_git_check_ignore_blocks_root_conformance_but_allows_openness_reports():
    openness_reports = (
        "docs/openness/conformance-valio-aimo.json",
        "docs/openness/conformance-nordic-bakery.json",
    )
    for rel in openness_reports:
        assert (ROOT / rel).is_file(), f"Missing committed conformance report: {rel}"

    if not (ROOT / ".git").exists():
        return

    ignored_probe = subprocess.run(
        ["git", "check-ignore", "-q", "conformance-127.0.0.1_8000.json"],
        cwd=ROOT,
        check=False,
    )
    assert ignored_probe.returncode == 0, "conformance-127.0.0.1_8000.json must be ignored by git"

    for rel in openness_reports:
        allowed_probe = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=ROOT,
            check=False,
        )
        assert allowed_probe.returncode != 0, f"{rel} must not be ignored by git"


def test_makefile_targets_and_judge_accept_script_valid():
    makefile_text = _read_text("Makefile")
    defined_targets: set[str] = set()
    phony_targets: set[str] = set()
    target_re = re.compile(r"^([A-Za-z0-9_-]+)\s*:(?!=)")

    for raw_line in makefile_text.splitlines():
        if not raw_line or raw_line.lstrip().startswith(HASH):
            continue
        if raw_line.startswith(".PHONY:"):
            phony_targets.update(raw_line.split(":", 1)[1].split())
            continue
        if raw_line[0].isspace():
            continue
        match = target_re.match(raw_line)
        if match:
            defined_targets.add(match.group(1))

    missing_targets = REQUIRED_MAKE_TARGETS - defined_targets
    assert missing_targets == set(), f"Makefile missing required targets: {sorted(missing_targets)}"
    assert REQUIRED_MAKE_TARGETS.issubset(phony_targets)
    assert "scripts/judge_accept.sh" in makefile_text

    judge_accept = ROOT / "scripts" / "judge_accept.sh"
    assert judge_accept.is_file()
    assert judge_accept.stat().st_size > 0
    syntax_check = subprocess.run(
        ["bash", "-n", str(judge_accept)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax_check.returncode == 0, f"bash -n scripts/judge_accept.sh failed: {syntax_check.stderr}"
    assert os.access(judge_accept, os.X_OK) or syntax_check.returncode == 0


def test_combine_doc_documents_all_six_judge_review_gaps():
    combine = _read_text("docs/COMBINE.md")
    assert EM_DASH not in combine

    assert "quantity <= 0" in combine
    assert "Field(gt=0)" in combine

    assert "GEMINI_API_KEY" in combine
    assert "scripted" in combine.lower()
    assert "aimo demo" in combine

    assert "plan_need" in combine
    assert "allergen" in combine.lower()
    assert "hard-block" in combine.lower()

    assert "known customer" in combine.lower()
    assert "/v1" in combine

    assert "injection_screen" in combine
    assert "PlanLine" in combine

    assert re.search(r"\b13\b[^\n]*MCP|\bMCP\b[^\n]*\b13\b", combine)
    assert "build_mcp().list_tools()" in combine


def test_combine_doc_disposition_table_covers_all_remote_branches_without_force_push():
    combine = _read_text("docs/COMBINE.md")
    section_header = f"{HASH}{HASH} Disposition of stale team/* branches"
    assert section_header in combine
    after_header = combine.split(section_header, 1)[1]
    next_h2 = f"\n{HASH}{HASH} "
    disposition_section = after_header.split(next_h2, 1)[0]

    table_branches: list[str] = []
    for line in disposition_section.splitlines():
        if not line.startswith("| `origin/"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) >= 7, f"Expected 7 columns in disposition table row: {line}"
        branch_cell = cells[0].strip("`")
        assert branch_cell.startswith("origin/")
        branch_name = branch_cell.removeprefix("origin/")
        tip_sha = cells[1].strip("`")
        unmerged_main = cells[2].strip("`")
        unmerged_combined = cells[3].strip("`")
        unmerged_fr8 = cells[4].strip("`")

        assert re.fullmatch(r"[0-9a-f]{7,40}", tip_sha), f"Invalid tip SHA {tip_sha!r} for {branch_name}"
        assert unmerged_main == "0", f"Expected 0 unmerged vs origin/main for {branch_name}, got {unmerged_main!r}"
        assert unmerged_combined.isdigit(), f"Non-numeric unmerged count vs combined for {branch_name}"
        assert unmerged_fr8.isdigit(), f"Non-numeric unmerged count vs fr8/valio/main for {branch_name}"
        table_branches.append(branch_name)

    assert len(EXPECTED_TEAM_BRANCHES) == 13
    assert len(EXPECTED_REMOTE_BRANCHES) == 14
    assert len(table_branches) == 14, f"Expected 14 rows in disposition table, got {len(table_branches)}: {table_branches}"
    assert set(table_branches) == set(EXPECTED_REMOTE_BRANCHES)

    assert "--force" not in combine
    code_blocks = re.findall(r"```(?:bash|sh)?\n(.*?)```", combine, flags=re.DOTALL)
    push_blocks = [blk for blk in code_blocks if "git push" in blk]
    assert push_blocks, "Expected non-force git push origin --delete cleanup blocks in docs/COMBINE.md"
    deleted_in_blocks: set[str] = set()
    for blk in push_blocks:
        normalized = blk.replace("\\\n", " ")
        tokens = normalized.split()
        for tok in tokens:
            assert tok != "-f" and not tok.startswith("--force"), f"Force flag {tok!r} forbidden in push block"
            assert not tok.startswith("+"), f"Force refspec {tok!r} forbidden in push block"
        for branch_name in EXPECTED_REMOTE_BRANCHES:
            if branch_name in tokens:
                deleted_in_blocks.add(branch_name)
    assert deleted_in_blocks == set(EXPECTED_REMOTE_BRANCHES)
