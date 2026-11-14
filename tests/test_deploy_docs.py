import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ROOT / "deploy"
RUNBOOK = DEPLOY_DIR / "RUNBOOK.md"
ENV_PREFIXES = ("AIMO_", "VALIO_", "LLM_")
ANCHOR = chr(35)
EM_DASH = "\u2014"


def _is_os_environ(node):
    if isinstance(node, ast.Name) and node.id == "environ":
        return True
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_getenv_call(func):
    if isinstance(func, ast.Name) and func.id == "getenv":
        return True
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        return True
    return (
        isinstance(func, ast.Attribute)
        and func.attr in ("get", "setdefault", "pop")
        and _is_os_environ(func.value)
    )


ENV_REGEX = re.compile(
    r"""(?:os\.)?(?:environ(?:\.(?:get|pop|setdefault))?|getenv)\s*[\(\[]\s*['"]((?:AIMO|VALIO|LLM)_[A-Z0-9_]+)['"](?!\s*\]\s*=)"""
)


def _extract_from_source(src, filename="<mem>"):
    found = set(ENV_REGEX.findall(src))
    tree = ast.parse(src, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_getenv_call(node.func) and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                if arg0.value.startswith(ENV_PREFIXES):
                    found.add(arg0.value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_os_environ(node.value)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            if node.slice.value.startswith(ENV_PREFIXES):
                found.add(node.slice.value)
        elif isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            if any(_is_os_environ(comp) for comp in node.comparators):
                if node.left.value.startswith(ENV_PREFIXES):
                    found.add(node.left.value)
    return found


def _extract_prefixed_env_vars():
    found = set()
    py_files = sorted(ROOT.glob("aimo/*.py")) + sorted(ROOT.glob("agents/*.py"))
    assert py_files
    for path in py_files:
        found.update(_extract_from_source(path.read_text(encoding="utf-8"), str(path)))
    return found


def test_env_extractor_handles_os_environ_and_getenv_forms():
    sample = (
        "import os\n"
        "from os import environ, getenv\n"
        "a = os.getenv('AIMO_SAMPLE_ONE')\n"
        "b = getenv('VALIO_SAMPLE_TWO', 'x')\n"
        "c = os.environ.get('LLM_SAMPLE_THREE')\n"
        "d = environ['AIMO_SAMPLE_FOUR']\n"
        "e = 'VALIO_SAMPLE_FIVE' in os.environ\n"
        "os.environ['AIMO_WRITE_ONLY'] = '1'\n"
    )
    assert _extract_from_source(sample) == {
        "AIMO_SAMPLE_ONE",
        "VALIO_SAMPLE_TWO",
        "LLM_SAMPLE_THREE",
        "AIMO_SAMPLE_FOUR",
        "VALIO_SAMPLE_FIVE",
    }


def test_every_code_env_var_documented_in_runbook():
    assert RUNBOOK.is_file(), "deploy/RUNBOOK.md is missing"
    runbook_text = RUNBOOK.read_text(encoding="utf-8")
    discovered = _extract_prefixed_env_vars()
    expected_floor = {
        "AIMO_DATA_DIR",
        "AIMO_DB",
        "AIMO_KEY_DIR",
        "AIMO_CACHE_DIR",
        "AIMO_ADMIN_KEY",
        "AIMO_AGENT_KEYS",
        "AIMO_AGENT_KEY",
        "VALIO_API_BASE",
        "VALIO_API_KEY",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
    }
    assert expected_floor.issubset(discovered)
    missing = sorted(var for var in discovered if f"`{var}`" not in runbook_text)
    assert not missing, f"Env vars read in aimo/*.py or agents/*.py missing from deploy/RUNBOOK.md: {missing}"
    for extra in ("PORT", "AIMO_EVAL"):
        assert f"`{extra}`" in runbook_text, f"Expected `{extra}` in deploy/RUNBOOK.md"


def test_runbook_checklist_defaults_and_recovery_sections():
    text = RUNBOOK.read_text(encoding="utf-8")
    required_snippets = (
        "Pre-Stage T-15 Min Checklist",
        "Environment Variables Reference",
        "Stage Recovery and Operations",
        "data/",
        ".env",
        "make smoke",
        "6/6",
        "make demo",
        "var/cache",
        "var/demo.db",
        "8080",
        "scripts/smoke_demo.py --url http://localhost:8080",
        "/console.html",
        "/inbox.html",
        "/audit.html",
        "/admin.html",
        "DELETE",
        "/v1/admin/simulate",
        "X-Admin-Key: demo-admin",
        "make stage",
        "make run",
        "python -m aimo.keys rotate",
        "aimo.quotes verify",
        "aimo.audit verify-checkpoint",
        "/.well-known/jwks.json",
        '"var/aimo.db"',
        '"var/keys"',
        '"var/cache"',
        '"demo-admin"',
    )
    for snippet in required_snippets:
        assert snippet in text, f"Missing required runbook detail: {snippet}"


def test_deploy_docs_no_em_dashes():
    deploy_mds = sorted(DEPLOY_DIR.glob("*.md"))
    assert len(deploy_mds) >= 3
    offenders = [
        str(md.relative_to(ROOT))
        for md in deploy_mds
        if EM_DASH in md.read_text(encoding="utf-8")
    ]
    assert not offenders, f"Em dashes found in deploy docs: {offenders}"


def test_deploy_docs_relative_links_resolve():
    deploy_mds = sorted(DEPLOY_DIR.glob("*.md"))
    assert RUNBOOK in deploy_mds
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    broken = []
    runbook_links = 0
    for md in deploy_mds:
        raw = md.read_text(encoding="utf-8")
        cleaned = re.sub(r"```[\s\S]*?```", "", raw)
        cleaned = re.sub(r"`[^`]*`", "", cleaned)
        for match in link_re.finditer(cleaned):
            target = match.group(1).strip().strip("<>").split()[0]
            if target.startswith(("http://", "https://", "mailto:", ANCHOR, "/")):
                continue
            path_part = target.split(ANCHOR, 1)[0]
            if not path_part:
                continue
            if md == RUNBOOK:
                runbook_links += 1
            resolved = (md.parent / path_part).resolve()
            if not resolved.exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
    assert runbook_links >= 4
    assert not broken, f"Broken relative links in deploy/*.md: {broken}"


def test_runbook_referenced_repo_paths_exist():
    text = RUNBOOK.read_text(encoding="utf-8")
    refs = sorted(set(re.findall(r"`((?:aimo|agents|scenarios|scripts|pitch|tests)/[A-Za-z0-9_]+\.py)`", text)))
    assert len(refs) >= 12
    missing = [rel for rel in refs if not (ROOT / rel).is_file()]
    assert not missing, f"Non-existent repo files referenced in deploy/RUNBOOK.md: {missing}"


def test_self_hygiene_no_comments_or_em_dashes():
    self_src = Path(__file__).read_text(encoding="utf-8")
    assert ANCHOR not in self_src
    assert EM_DASH not in self_src
