from __future__ import annotations

import io
import json
import os
import pathlib
import re
import subprocess
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[1]
HASH = chr(35)
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
FORBIDDEN_TRAILER = "co-authored" + "-by"

STALE_ROUND5_BRANCHES = (
    "team/buyer",
    "team/catalog",
    "team/frontend",
    "team/gateway",
    "team/pitch",
    "team/port",
    "team/trust",
    "team/windtunnel",
    "justSilas-aimo-integration",
)

ACTIVE_SESSION_BRANCHES = (
    "team/replay",
    "team/harden",
    "team/need",
    "team/submit",
    "team/ui",
    "team/live",
    "team/tests",
)

WIP_RESCUE_SNAPSHOT_BRANCHES = (
    "wip/rescue-1115/aimo-buyer-integration",
    "wip/rescue-1115/aimo-catalog-needtypes",
    "wip/rescue-1115/aimo-gateway-budget",
    "wip/rescue-1115/aimo-trust-learned",
    "wip/rescue-1115/aimo-web",
)

OPENNESS_CONFORMANCE_REPORTS = (
    "docs/openness/conformance-valio-aimo.json",
    "docs/openness/conformance-nordic-bakery.json",
)

FORBIDDEN_WORKSPACE_DIRS = (
    ".venv",
    "venv",
    "node_modules",
)


def _read_text(rel_path: str) -> str:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing required file: {rel_path}"
    return path.read_text(encoding="utf-8")


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


def _combine_sections() -> tuple[str, str, str, list[str]]:
    combine = _read_text("docs/COMBINE.md")
    assert EM_DASH not in combine
    assert EN_DASH not in combine
    assert "--force" not in combine

    h2_disposition = f"{HASH}{HASH} Disposition of stale team/* branches"
    assert h2_disposition in combine
    after_h2 = combine.split(h2_disposition, 1)[1]
    disposition_section = after_h2.split(f"\n{HASH}{HASH} ", 1)[0]

    h3_wip = f"{HASH}{HASH}{HASH} Stale worktree snapshot branches (wip/rescue-1115/*)"
    assert h3_wip in disposition_section
    primary_part, wip_part = disposition_section.split(h3_wip, 1)

    code_blocks = re.findall(r"```(?:bash|sh)?\n(.*?)```", disposition_section, flags=re.DOTALL)
    push_blocks = [blk for blk in code_blocks if "git push" in blk]
    return combine, primary_part, wip_part, push_blocks


def _parse_primary_branch_rows(primary_part: str) -> dict[str, tuple[str, int, int, int, str]]:
    rows: dict[str, tuple[str, int, int, int, str]] = {}
    for line in primary_part.splitlines():
        if not line.startswith("| `origin/"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) >= 7, f"Malformed primary disposition row: {line}"
        branch_cell = cells[0].strip("`")
        assert branch_cell.startswith("origin/")
        branch = branch_cell.removeprefix("origin/")
        assert branch not in rows, f"Duplicate disposition row for {branch}"
        tip_sha = cells[1].strip("`")
        unmerged_main = cells[2].strip("`")
        unmerged_combined = cells[3].strip("`")
        unmerged_fr8 = cells[4].strip("`")
        disposition_text = cells[6]
        assert re.fullmatch(r"[0-9a-f]{7,40}", tip_sha), f"Invalid SHA {tip_sha!r} for {branch}"
        assert unmerged_main.isdigit() and int(unmerged_main) == 0
        assert unmerged_combined.isdigit()
        assert unmerged_fr8.isdigit()
        rows[branch] = (
            tip_sha,
            int(unmerged_main),
            int(unmerged_combined),
            int(unmerged_fr8),
            disposition_text,
        )
    return rows


def _parse_delete_block_branches(block: str) -> list[str]:
    tokens = block.replace("\\\n", " ").split()
    assert tokens[:4] == ["git", "push", "origin", "--delete"], (
        f"Expected 'git push origin --delete ...', got {tokens[:4]}"
    )
    for tok in tokens:
        assert tok != "-f" and not tok.startswith("--force"), f"Forbidden force flag: {tok}"
        assert not tok.startswith("+"), f"Forbidden force refspec: {tok}"
    return tokens[4:]


def test_combine_documents_nine_stale_round5_branches_and_non_force_delete():
    _, primary_part, _, push_blocks = _combine_sections()
    rows = _parse_primary_branch_rows(primary_part)

    assert len(STALE_ROUND5_BRANCHES) == 9
    for branch in STALE_ROUND5_BRANCHES:
        assert branch in rows, f"docs/COMBINE.md missing stale round-5 branch {branch}"
        _, unmerged_main, unmerged_combined, unmerged_fr8, disposition = rows[branch]
        assert unmerged_main == 0
        assert unmerged_combined == 0
        assert unmerged_fr8 == 0
        assert "safe to delete now" in disposition.lower()

    assert len(push_blocks) == 2
    immediate_targets = _parse_delete_block_branches(push_blocks[0])
    assert len(immediate_targets) == 9
    assert set(immediate_targets) == set(STALE_ROUND5_BRANCHES)


def test_combine_documents_seven_session_branches_and_freeze_cleanup():
    _, primary_part, _, push_blocks = _combine_sections()
    rows = _parse_primary_branch_rows(primary_part)

    assert len(ACTIVE_SESSION_BRANCHES) == 7
    expected_session_markers = {
        "team/replay": "s1",
        "team/harden": "s2",
        "team/need": "s3",
        "team/submit": "s4",
        "team/ui": "s5",
        "team/live": "live",
        "team/tests": "tests",
    }
    for branch in ACTIVE_SESSION_BRANCHES:
        assert branch in rows, f"docs/COMBINE.md missing session branch {branch}"
        _, unmerged_main, unmerged_combined, unmerged_fr8, disposition = rows[branch]
        assert unmerged_main == 0
        assert unmerged_combined >= 1
        assert unmerged_fr8 >= 1
        low = disposition.lower()
        assert "06:00" in low
        assert expected_session_markers[branch] in low

    assert set(rows) == set(STALE_ROUND5_BRANCHES) | set(ACTIVE_SESSION_BRANCHES)
    assert set(STALE_ROUND5_BRANCHES).isdisjoint(ACTIVE_SESSION_BRANCHES)

    freeze_targets = _parse_delete_block_branches(push_blocks[1])
    assert len(freeze_targets) == 7
    assert set(freeze_targets) == set(ACTIVE_SESSION_BRANCHES)


def test_combine_documents_five_wip_rescue_branches_excluded_from_delete():
    _, _, wip_part, push_blocks = _combine_sections()
    assert len(WIP_RESCUE_SNAPSHOT_BRANCHES) == 5
    assert "never force-push" in wip_part.lower()
    assert "post-freeze" in wip_part.lower()

    wip_rows: dict[str, tuple[str, str, int, int, int, str]] = {}
    for line in wip_part.splitlines():
        if not line.startswith("| `origin/"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) >= 7, f"Malformed wip/rescue-1115/* row: {line}"
        branch_cell = cells[0].strip("`")
        assert branch_cell.startswith("origin/")
        branch = branch_cell.removeprefix("origin/")
        assert branch not in wip_rows
        tip_sha = cells[1].strip("`")
        base_sha = cells[2].strip("`")
        unmerged_main = cells[3].strip("`")
        unmerged_combined = cells[4].strip("`")
        unmerged_fr8 = cells[5].strip("`")
        disposition = cells[6].lower()
        assert re.fullmatch(r"[0-9a-f]{7,40}", tip_sha)
        assert re.fullmatch(r"[0-9a-f]{7,40}", base_sha)
        assert unmerged_main.isdigit() and int(unmerged_main) >= 1
        assert unmerged_combined.isdigit() and int(unmerged_combined) >= 1
        assert unmerged_fr8.isdigit() and int(unmerged_fr8) >= 1
        assert "untouched" in disposition
        assert "post-freeze" in disposition
        assert "never force-push" in disposition
        wip_rows[branch] = (
            tip_sha,
            base_sha,
            int(unmerged_main),
            int(unmerged_combined),
            int(unmerged_fr8),
            disposition,
        )

    assert set(wip_rows) == set(WIP_RESCUE_SNAPSHOT_BRANCHES)

    deleted_anywhere: set[str] = set()
    for blk in push_blocks:
        deleted_anywhere.update(_parse_delete_block_branches(blk))
    assert deleted_anywhere.isdisjoint(WIP_RESCUE_SNAPSHOT_BRANCHES)


def test_gitignore_and_openness_conformance_reports_have_44_passed_checks():
    raw_entries = _ignore_entries(".gitignore")
    normalized = {entry.rstrip("/") for entry in raw_entries}

    assert ".env" in normalized
    assert ".venv" in normalized
    assert "node_modules" in normalized
    assert "conformance-*.json" in raw_entries
    assert "/conformance-*.json" not in raw_entries

    if (ROOT / ".git").exists():
        ignored_candidates = [
            ".env",
            ".venv/pyvenv.cfg",
            "node_modules/pkg/index.js",
            "conformance-127.0.0.1_8000.json",
            "conformance-root.json",
            "sub/conformance-stray.json",
        ]
        proc = subprocess.run(
            ["git", "check-ignore", *ignored_candidates],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert set(proc.stdout.splitlines()) == set(ignored_candidates)

        tracked_openness = set(
            subprocess.check_output(
                ["git", "ls-files", "docs/openness/conformance-*.json"],
                cwd=ROOT,
                text=True,
            ).splitlines()
        )
        assert set(OPENNESS_CONFORMANCE_REPORTS).issubset(tracked_openness)

        tracked_root_conf = [
            path
            for path in subprocess.check_output(
                ["git", "ls-files", "conformance-*.json"],
                cwd=ROOT,
                text=True,
            ).splitlines()
            if path.strip()
        ]
        assert tracked_root_conf == []

        for rel in tracked_openness:
            not_ignored = subprocess.run(
                ["git", "check-ignore", "-q", rel],
                cwd=ROOT,
                check=False,
            )
            assert not_ignored.returncode != 0, f"Tracked conformance report {rel} must not be ignored"
    else:
        tracked_openness = set(OPENNESS_CONFORMANCE_REPORTS)

    report_paths = sorted(
        {*(ROOT / rel for rel in tracked_openness), *(ROOT / "docs" / "openness").glob("conformance-*.json")}
    )
    assert len(report_paths) >= 2

    for report_path in report_paths:
        assert report_path.is_file()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload.get("profile") == "otap/0.1"
        mcp_tools = payload.get("mcp_tools")
        assert isinstance(mcp_tools, list) and len(mcp_tools) == 13
        assert [tool for tool in mcp_tools if "approv" in tool.lower()] == []

        summary = payload.get("summary")
        assert summary == {"pass": 44, "fail": 0, "skip": 0, "total": 44}, (
            f"Unexpected summary in {report_path.name}: {summary}"
        )

        results = payload.get("results")
        assert isinstance(results, list) and len(results) == 44
        assert all(item.get("status") == "PASS" for item in results)
        by_id = {item["id"]: item for item in results}
        assert by_id["D8"]["status"] == "PASS"
        assert by_id["M1"]["detail"] == "13"


def test_dockerfile_copies_snapshot_exposes_8000_and_has_zero_leaked_secrets():
    dockerfile_text = _read_text("Dockerfile")
    instructions = _dockerfile_instructions(dockerfile_text)

    assert "COPY tests/fixtures/snapshot tests/fixtures/snapshot" in instructions
    for meta_rel in (
        "tests/fixtures/snapshot/meta.json",
        "tests/fixtures/snapshot/replay/meta.json",
    ):
        meta_data = json.loads(_read_text(meta_rel))
        assert isinstance(meta_data, dict) and meta_data

    assert "EXPOSE 8000" in instructions
    assert any(
        inst.startswith("HEALTHCHECK ") and ":8000/healthz" in inst
        for inst in instructions
    )
    assert any(
        inst.startswith("CMD ") and "--port 8000" in inst
        for inst in instructions
    )
    user_insts = [inst for inst in instructions if inst.startswith("USER ")]
    assert user_insts and user_insts[-1] == "USER aimo"

    secret_name_re = re.compile(
        r"(?:API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|APPROVER_PIN|AGENT_KEY)",
        flags=re.IGNORECASE,
    )
    secret_value_patterns = (
        re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
        re.compile(r"\bsk-[0-9A-Za-z_-]{20,}"),
        re.compile(r"\bghp_[0-9A-Za-z]{20,}"),
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    )
    for pattern in secret_value_patterns:
        assert pattern.search(dockerfile_text) is None

    forbidden_copy_roots = {
        ".",
        ".env",
        "keys",
        "data",
        ".git",
        ".venv",
        "venv",
        "node_modules",
    }
    for inst in instructions:
        if inst.startswith(("ENV ", "ARG ")):
            body = inst.split(None, 1)[1]
            for token in body.split():
                key = token.split("=", 1)[0]
                assert secret_name_re.search(key) is None, f"Secret env/arg key in Dockerfile: {key}"
        if inst.startswith(("COPY ", "ADD ")):
            args = [tok for tok in inst.split()[1:] if not tok.startswith("--")]
            assert len(args) >= 2
            for src in args[:-1]:
                clean = src.rstrip("/")
                assert clean not in forbidden_copy_roots, f"Forbidden COPY/ADD source {src!r}"
                assert not clean.startswith((".env", "keys/", "data/", ".venv/", "venv/", "node_modules/"))
                assert not clean.endswith((".pem", ".key", ".sqlite"))

    dockerignore_norm = {entry.rstrip("/") for entry in _ignore_entries(".dockerignore")}
    for required in (".env", "keys", "*.pem", "data", ".venv", "node_modules", "conformance-*.json"):
        assert required in dockerignore_norm


def test_no_venv_or_node_modules_exists_in_repo_root_or_git_index():
    for dirname in FORBIDDEN_WORKSPACE_DIRS:
        candidate = ROOT / dirname
        assert not os.path.lexists(candidate), f"Forbidden directory/symlink in repo root: {dirname}"

    if (ROOT / ".git").exists():
        tracked = subprocess.check_output(
            ["git", "ls-files"],
            cwd=ROOT,
            text=True,
        ).splitlines()
        forbidden_prefixes = tuple(f"{name}/" for name in FORBIDDEN_WORKSPACE_DIRS) + (
            "keys/",
            "data/",
        )
        for rel in tracked:
            assert rel != ".env"
            assert not rel.startswith(forbidden_prefixes), f"Forbidden path tracked in git: {rel}"
            assert not rel.endswith((".pem", ".key", ".sqlite")), f"Forbidden artifact tracked in git: {rel}"


def test_self_hygiene_zero_comments_zero_dashes_and_no_co_authored_by():
    self_text = pathlib.Path(__file__).resolve().read_text(encoding="utf-8")
    assert HASH not in self_text
    assert EM_DASH not in self_text
    assert EN_DASH not in self_text
    assert FORBIDDEN_TRAILER not in self_text.casefold()
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(self_text).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
