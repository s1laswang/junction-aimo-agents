import ast
import asyncio
import io
import json
import re
import tokenize
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo import audit, keys, mandates, mcp_server, quotes
from aimo.app import app
from aimo.models import QuoteLine
from tests.test_pitch_numbers import EXPECTED_MEASURED

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = "\u2014"
HASH_CHAR = "\u0023"
H2_PREFIX = HASH_CHAR * 2 + " "

ENGINEERING_DOCS = (
    "docs/ARCHITECTURE.md",
    "docs/TRUST.md",
    "docs/STANDARDS.md",
    "docs/PRODUCTION.md",
    "docs/ASSUMPTIONS.md",
)

EXPLICIT_INTERNAL_HANDLES = {
    "lead-gateway",
    "lead-agents",
    "lead-ui",
    "gw-catalog",
    "gw-offers",
    "gw-orders",
    "gw-mcp",
    "tr-sign",
    "tr-policy",
    "tr-approve",
    "tr-audit",
    "br-parse",
    "br-subst",
    "br-split",
    "br-recipe",
    "ag-buyer",
    "ag-seller",
    "ag-drift",
    "ag-scenarios",
    "ui-console",
    "ui-inbox",
    "ui-audit",
    "ui-admin",
    "sh-qa",
    "sh-deploy",
    "sh-docs",
    "sh-pitch",
}

HANDLE_PATTERN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"lead-(?:gateway|agents|ui)"
    r"|gw-[a-z0-9]+"
    r"|tr-[a-z0-9]+"
    r"|br-[a-z0-9]+"
    r"|ag-[a-z0-9]+"
    r"|ui-(?:console|inbox|audit|admin)"
    r"|sh-(?:qa|deploy|docs|pitch)"
    r"|valio-(?:lead|gateway|scenarios|ui|tests|pitch)(?:-[a-z0-9]+)*"
    r")(?![A-Za-z0-9_-])"
)

DIRECT_REPO_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"((?:aimo|agents|web|tests|data_extra|scenarios|scripts|pitch)/[A-Za-z0-9_.*-]+)"
)

DOTTED_MODULE_RE = re.compile(r"\b((?:aimo|agents|pitch|scripts|scenarios)\.[a-z_][a-z0-9_]*)\b")

QUOTE_ERROR_CODES = {
    "BAD_SIGNATURE",
    "QUOTE_MISMATCH",
    "WRONG_AGENT",
    "QUOTE_EXPIRED",
    "QUOTE_USED",
    "UNKNOWN_QUOTE",
    "OUT_OF_STOCK",
    "INVALID_QUOTE",
}

APPROVAL_ERROR_CODES = {
    "NOT_APPROVER",
    "NOT_PENDING",
    "NOT_FOUND",
    "QUOTE_EXPIRED",
}

MANDATE_ERROR_CODES = {
    "INVALID_MANDATE",
}

POLICY_REASON_CODES = {
    "WITHIN_MANDATE",
    "OVER_ORDER_LIMIT",
    "OVER_WEEK_LIMIT",
    "SUBSTITUTION_NEEDS_APPROVAL",
    "UNKNOWN_PRODUCT",
    "ALLERGEN_CONFLICT",
    "CATEGORY_NOT_ALLOWED",
    "SUBSTITUTION_NOT_ALLOWED",
    "PAST_CUTOFF",
    "INVALID_QUOTE",
    "AGENT_REQUESTED",
    "STOCKOUT",
    "NON_PREFERRED_BRAND",
}

STANDARDS_REQUIRED_TOKENS = (
    "aimo.quotes verify",
    "aimo.audit verify-checkpoint",
    "aimo.keys rotate",
    "/.well-known/jwks.json",
    "/.well-known/agent-card.json",
    "/openapi.json",
    "/mcp",
)


def extract_internal_handles(text):
    found = set(HANDLE_PATTERN_RE.findall(text))
    for handle in EXPLICIT_INTERNAL_HANDLES:
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(handle)}(?![A-Za-z0-9_-])", text):
            found.add(handle)
    return sorted(found)


def extract_backtick_repo_paths(markdown_text, resolve_modules_table=False):
    exact_files = set()
    glob_patterns = set()

    for span in re.findall(r"`([^`\n]+)`", markdown_text):
        for raw_match in DIRECT_REPO_PATH_RE.findall(span):
            cleaned = raw_match.rstrip(".,:;)")
            if "<" in cleaned or ">" in cleaned or cleaned.endswith("/"):
                continue
            if "*" in cleaned:
                glob_patterns.add(cleaned)
            elif "." in Path(cleaned).name:
                exact_files.add(cleaned)
        for dotted in DOTTED_MODULE_RE.findall(span):
            cand = dotted.replace(".", "/") + ".py"
            if cand.startswith(("aimo/", "agents/", "pitch/", "scripts/", "scenarios/")):
                if (ROOT / cand).is_file() or cand.startswith(("aimo/", "agents/")):
                    exact_files.add(cand)

    if resolve_modules_table:
        in_modules = False
        for raw_line in markdown_text.splitlines():
            line = raw_line.strip()
            if line.startswith(H2_PREFIX):
                in_modules = line[len(H2_PREFIX):].strip().lower().startswith("modules")
                continue
            if not in_modules or not (line.startswith("|") and line.endswith("|")):
                continue
            cols = [c.strip() for c in line[1:-1].split("|")]
            if not cols or re.fullmatch(r":?-+:?", cols[0].replace(" ", "")):
                continue
            tokens = re.findall(r"`([^`]+)`", cols[0])
            prefix = None
            for tok in tokens:
                tok = tok.strip()
                if tok in ("aimo/", "agents/", "web/"):
                    prefix = tok
                    continue
                if "/" in tok:
                    maybe_prefix = tok.rsplit("/", 1)[0] + "/"
                    if maybe_prefix in ("aimo/", "agents/", "web/"):
                        prefix = maybe_prefix
                    if "*" in tok:
                        glob_patterns.add(tok)
                    elif "." in Path(tok).name:
                        exact_files.add(tok)
                elif prefix and (tok.endswith((".py", ".html", ".js", ".css")) or ".*" in tok):
                    combined = prefix + tok
                    if "*" in combined:
                        glob_patterns.add(combined)
                    else:
                        exact_files.add(combined)

    return sorted(exact_files), sorted(glob_patterns)


def extract_architecture_module_rows(markdown_text):
    rows = []
    in_modules = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if line.startswith(H2_PREFIX):
            in_modules = line[len(H2_PREFIX):].strip().lower().startswith("modules")
            continue
        if not in_modules or not (line.startswith("|") and line.endswith("|")):
            continue
        cols = [c.strip() for c in line[1:-1].split("|")]
        if len(cols) < 2 or all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cols if c):
            continue
        if cols[0].lower() == "module":
            continue
        exact_files, _ = extract_backtick_repo_paths(
            H2_PREFIX + "Modules\n| " + cols[0] + " |",
            resolve_modules_table=True,
        )
        row_text = " ".join(cols[1:])
        callables = []
        for span in re.findall(r"`([^`]+)`", row_text):
            for fn_match in re.findall(r"(?:([a-z_][a-z0-9_]*)\.)?([a-z_][a-z0-9_]*)\(\)", span):
                callables.append(fn_match)
        rows.append((exact_files, callables))
    return rows


def ast_top_level_names(py_path):
    tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def extract_live_error_and_policy_codes():
    quote_codes = set()
    approval_codes = set()
    policy_codes = set()

    for rel in ("aimo/quotes.py", "aimo/offers.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "QuoteError" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    quote_codes.add(arg0.value)

    for rel in ("aimo/approvals.py", "aimo/mandates.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ApprovalError" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    approval_codes.add(arg0.value)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "code":
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            approval_codes.add(node.value.value)

    for rel in ("aimo/policy.py", "aimo/orders.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = getattr(node.func, "id", None)
            if func_name == "add" and len(node.args) >= 2:
                arg1 = node.args[1]
                if isinstance(arg1, ast.Constant) and isinstance(arg1.value, str):
                    policy_codes.add(arg1.value)
            elif func_name == "Reason":
                for kw in node.keywords:
                    if kw.arg == "code" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        policy_codes.add(kw.value.value)

    return quote_codes, approval_codes, policy_codes


def _col_index(headers, keywords, default_idx):
    for idx, header in enumerate(headers):
        if any(kw in header for kw in keywords):
            return idx
    return default_idx


def parse_assumptions_seeded_customers(markdown_text):
    rows = {}
    in_seeded = False
    headers = []
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if line.startswith(H2_PREFIX):
            in_seeded = "seeded customers" in line[len(H2_PREFIX):].strip().lower()
            headers = []
            continue
        if not in_seeded or not (line.startswith("|") and line.endswith("|")):
            continue
        cols = [c.strip() for c in line[1:-1].split("|")]
        if len(cols) < 5 or all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cols if c):
            continue
        if not headers:
            headers = [c.replace("`", "").strip().lower() for c in cols]
            continue

        agent_idx = _col_index(headers, ("agent",), 1)
        limit_idx = _col_index(headers, ("limit", "eur", "budget"), 3)
        allergen_idx = _col_index(headers, ("allergen", "exclude"), 4)
        sub_idx = _col_index(headers, ("substitut",), 5)
        approver_idx = _col_index(headers, ("approver",), 6)

        customer = cols[0].strip("` ").strip()
        agent_tokens = re.findall(r"`([^`]+)`", cols[agent_idx])
        agent_id = agent_tokens[0] if agent_tokens else None
        demo_key = agent_tokens[1] if len(agent_tokens) >= 2 else None

        eur_nums = [float(x.replace(",", "")) for x in re.findall(r"([\d,]+(?:\.\d+)?)\s*EUR", cols[limit_idx], re.I)]
        assert len(eur_nums) == 2, f"Expected 2 EUR limits for {customer}: {cols[limit_idx]}"

        allergen_cell = cols[allergen_idx].strip()
        if allergen_cell.lower() == "none":
            allergens = []
        else:
            allergens = [tok.strip() for tok in re.findall(r"`([^`]+)`", allergen_cell)]
            if not allergens:
                allergens = [p.strip() for p in allergen_cell.split(",") if p.strip()]

        sub_tokens = re.findall(r"`([^`]+)`", cols[sub_idx])
        substitutions = sub_tokens[0] if sub_tokens else cols[sub_idx].strip()
        preferred_brands = sub_tokens[1:] if len(sub_tokens) >= 2 else []

        approvers = [name.strip("` ").strip() for name in cols[approver_idx].split(",") if name.strip("` ").strip()]

        rows[customer] = {
            "agent_id": agent_id,
            "demo_key": demo_key,
            "per_order_eur": eur_nums[0],
            "per_week_eur": eur_nums[1],
            "exclude_allergens": allergens,
            "substitutions": substitutions,
            "preferred_brands": preferred_brands,
            "approvers": approvers,
        }
    return rows


def test_engineering_docs_exist_and_zero_worker_handles():
    contract_text = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
    contract_handles = set(extract_internal_handles(contract_text))
    assert contract_handles >= {"lead-gateway", "gw-catalog", "tr-sign", "br-parse", "sh-docs"}

    for rel in ENGINEERING_DOCS:
        doc_path = ROOT / rel
        assert doc_path.is_file(), f"Missing engineering doc: {rel}"
        assert doc_path.read_text(encoding="utf-8").strip(), f"Empty engineering doc: {rel}"

    all_docs = sorted((ROOT / "docs").glob("*.md"))
    assert len(all_docs) >= 6
    leaks = {}
    for md_path in all_docs:
        txt = md_path.read_text(encoding="utf-8")
        found = extract_internal_handles(txt)
        if found:
            leaks[str(md_path.relative_to(ROOT))] = found
    assert not leaks, f"Internal worker handles leaked into docs/*.md: {leaks}"


def test_internal_handle_detector_precision():
    clean_text = "Every step writes a hash-chained trail and runs ui-friendly checks."
    assert extract_internal_handles(clean_text) == []
    dirty_text = "Owned by lead-gateway, tr-sign, br-parse, sh-docs, and valio-pitch-w6."
    assert extract_internal_handles(dirty_text) == [
        "br-parse",
        "lead-gateway",
        "sh-docs",
        "tr-sign",
        "valio-pitch-w6",
    ]


def test_architecture_backtick_paths_modules_and_symbols_exist():
    arch_text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    exact_files, glob_patterns = extract_backtick_repo_paths(arch_text, resolve_modules_table=True)

    missing_files = [rel for rel in exact_files if not (ROOT / rel).is_file()]
    assert not missing_files, f"Missing files in docs/ARCHITECTURE.md: {missing_files}"

    assert set(glob_patterns) == {"web/admin.*", "web/audit.*", "web/console.*", "web/inbox.*"}
    for pattern in glob_patterns:
        matched = sorted(p.suffix for p in ROOT.glob(pattern) if p.is_file())
        assert matched == [".css", ".html", ".js"], f"Unexpected glob expansion for {pattern}: {matched}"

    live_aimo_modules = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "aimo").glob("*.py")
        if p.name != "__init__.py"
    }
    live_agent_modules = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "agents").glob("*.py")
        if p.name != "__init__.py"
    }
    assert live_aimo_modules <= set(exact_files)
    assert live_agent_modules <= set(exact_files)
    assert "web/index.html" in exact_files

    module_rows = extract_architecture_module_rows(arch_text)
    assert len(module_rows) >= 10
    for row_files, callables in module_rows:
        py_files = [f for f in row_files if f.endswith(".py")]
        defined_by_stem = {Path(f).stem: ast_top_level_names(ROOT / f) for f in py_files}
        all_defined = set().union(*defined_by_stem.values()) if defined_by_stem else set()
        for mod_prefix, fn_name in callables:
            if mod_prefix:
                assert mod_prefix in defined_by_stem, f"Module {mod_prefix} missing from row {py_files}"
                assert fn_name in defined_by_stem[mod_prefix], f"{mod_prefix}.{fn_name} not defined"
            else:
                assert fn_name in all_defined, f"{fn_name}() not defined in {py_files}"

    for rel in ENGINEERING_DOCS:
        doc_text = (ROOT / rel).read_text(encoding="utf-8")
        doc_files, doc_globs = extract_backtick_repo_paths(doc_text, resolve_modules_table=(rel == "docs/ARCHITECTURE.md"))
        missing = [f for f in doc_files if not (ROOT / f).is_file()]
        assert not missing, f"Broken file references in {rel}: {missing}"
        for pat in doc_globs:
            assert list(ROOT.glob(pat)), f"Empty glob {pat} in {rel}"


def test_backtick_path_extractor_catches_missing_module():
    snippet = "\n".join([
        H2_PREFIX + "Modules",
        "| Module | What it does |",
        "|---|---|",
        "| `aimo/app.py`, `nonexistent_xyz.py` | broken module reference |",
        "| `agents/` (`buyer.py`, `ghost_agent.py`) | broken agent reference |",
    ])
    exact_files, _ = extract_backtick_repo_paths(snippet, resolve_modules_table=True)
    assert "aimo/nonexistent_xyz.py" in exact_files
    assert "agents/ghost_agent.py" in exact_files
    missing = [f for f in exact_files if not (ROOT / f).is_file()]
    assert missing == ["agents/ghost_agent.py", "aimo/nonexistent_xyz.py"]


def test_assumptions_and_production_match_live_mandates_and_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "mandates.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

    live_mandates = {m.customer: m for m in mandates.all()}
    assert set(live_mandates) == {"hotel-aurora", "hospital-tays", "school-hervanta"}

    assumptions_text = (ROOT / "docs" / "ASSUMPTIONS.md").read_text(encoding="utf-8")
    doc_customers = parse_assumptions_seeded_customers(assumptions_text)
    assert set(doc_customers) == set(live_mandates)

    demo_keys_by_customer = {cust: (key, aid) for key, (cust, aid) in mandates._DEMO_KEYS.items()}

    for customer, live in live_mandates.items():
        doc = doc_customers[customer]
        expected_key, expected_agent = demo_keys_by_customer[customer]
        assert doc["agent_id"] == live.agent_id == expected_agent
        assert doc["demo_key"] == expected_key
        assert mandates.agent_for_key(doc["demo_key"]) == (customer, live.agent_id, live)
        assert doc["per_order_eur"] == live.per_order_eur
        assert doc["per_week_eur"] == live.per_week_eur
        assert doc["exclude_allergens"] == live.exclude_allergens
        assert doc["substitutions"] == live.substitutions
        assert doc["preferred_brands"] == live.preferred_brands
        assert doc["approvers"] == live.approvers

    missing_allergen_items = EXPECTED_MEASURED["products"] - EXPECTED_MEASURED["with_any_allergen_info"]
    assert missing_allergen_items == 8110

    for expected_num in (
        f"{EXPECTED_MEASURED['products']:,}",
        f"{EXPECTED_MEASURED['categories']:,}",
        f"{EXPECTED_MEASURED['names_en']:,}",
        f"{EXPECTED_MEASURED['lactose_free_claim']:,}",
        f"{EXPECTED_MEASURED['low_lactose_claim']:,}",
        f"{EXPECTED_MEASURED['gluten_free_claim']:,}",
        f"{EXPECTED_MEASURED['vegan_claim']:,}",
        f"{EXPECTED_MEASURED['with_any_allergen_info']:,}",
        f"{EXPECTED_MEASURED['with_allergen_declared']:,}",
        f"{EXPECTED_MEASURED['with_free_from_declared']:,}",
        f"{missing_allergen_items:,}",
        f"{EXPECTED_MEASURED['reliability_rows']:,}",
        f"{EXPECTED_MEASURED['co2_rows']:,}",
    ):
        assert expected_num in assumptions_text, f"Missing {expected_num} in docs/ASSUMPTIONS.md"

    production_text = (ROOT / "docs" / "PRODUCTION.md").read_text(encoding="utf-8")
    assert f"{EXPECTED_MEASURED['products']:,}" in production_text
    assert f"{missing_allergen_items:,}" in production_text
    for required_seam in (
        "AIMO_AGENT_KEYS",
        "AIMO_ADMIN_KEY",
        "AIMO_KEY_DIR",
        "AIMO_DB",
        "python -m aimo.keys rotate",
        "python -m aimo.audit verify-checkpoint",
        "UPSTREAM_UNAVAILABLE",
    ):
        assert required_seam in production_text, f"Missing {required_seam} in docs/PRODUCTION.md"


def test_trust_documents_all_error_and_policy_codes():
    live_quote_codes, live_approval_codes, live_policy_codes = extract_live_error_and_policy_codes()

    assert live_quote_codes == QUOTE_ERROR_CODES
    assert live_approval_codes == APPROVAL_ERROR_CODES | MANDATE_ERROR_CODES
    assert live_policy_codes == POLICY_REASON_CODES

    trust_text = (ROOT / "docs" / "TRUST.md").read_text(encoding="utf-8")
    all_required_codes = (
        QUOTE_ERROR_CODES
        | APPROVAL_ERROR_CODES
        | MANDATE_ERROR_CODES
        | POLICY_REASON_CODES
    )
    missing = sorted(code for code in all_required_codes if f"`{code}`" not in trust_text)
    assert not missing, f"Codes missing from docs/TRUST.md: {missing}"

    for token in (
        "python -m aimo.keys rotate",
        "retired-<kid>.json",
        "/.well-known/jwks.json",
        "python -m aimo.audit verify-checkpoint",
        "tests/test_quotes.py",
        "school-hervanta",
        "1,332.00 EUR",
        "600.00 EUR",
        "3,000.00 EUR",
    ):
        assert token in trust_text, f"Missing {token} in docs/TRUST.md"


def test_standards_documents_and_verifies_live_endpoints_and_clis(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "no_data"))
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "standards.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    keys._cache.clear()

    standards_text = (ROOT / "docs" / "STANDARDS.md").read_text(encoding="utf-8")
    for token in STANDARDS_REQUIRED_TOKENS:
        assert token in standards_text, f"Missing {token} in docs/STANDARDS.md"
    assert 'algorithms=["EdDSA"]' in standards_text
    assert "retired-<kid>.json" in standards_text

    line = QuoteLine(
        product_id="464119",
        name="Valio rasvaton maito 1 l laktoositon UHT",
        quantity=100.0,
        sales_unit="KI",
        unit_price_net=13.32,
        line_total_net=1332.0,
    )
    valid_until = datetime.now(timezone.utc) + timedelta(hours=4)
    issued = quotes.issue("hotel-aurora", "buyer-aurora", [line], date(2026, 11, 16), valid_until, 1512.0)
    cp = audit.checkpoint()

    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps(keys.jwks()), encoding="utf-8")

    assert quotes.main(["quotes.py", "verify", issued.jws, str(jwks_file)]) == 0
    q_out = capsys.readouterr().out.strip()
    assert q_out.startswith(f"OK {issued.quote_id} customer=hotel-aurora agent=buyer-aurora lines=1 total_gross=1512.00 EUR")

    assert audit.main(["audit.py", "verify-checkpoint", cp["jws"], str(jwks_file)]) == 0
    cp_out = capsys.readouterr().out.strip()
    assert cp_out == f"OK seq={cp['seq']} hash={cp['hash']}"

    old_kid = keys.kid()
    new_kid = keys.rotate()
    assert new_kid != old_kid
    assert (tmp_path / "keys" / f"retired-{old_kid}.json").is_file()

    jwks_file.write_text(json.dumps(keys.jwks()), encoding="utf-8")
    assert quotes.main(["quotes.py", "verify", issued.jws, str(jwks_file)]) == 0
    capsys.readouterr()
    assert audit.main(["audit.py", "verify-checkpoint", cp["jws"], str(jwks_file)]) == 0
    capsys.readouterr()

    with TestClient(app) as client:
        for path in ("/.well-known/jwks.json", "/.well-known/agent-card.json", "/openapi.json"):
            resp = client.get(path)
            assert resp.status_code == 200, path
        jwks_payload = client.get("/.well-known/jwks.json").json()
        assert [k["kid"] for k in jwks_payload["keys"]] == [new_kid, old_kid]
        card = client.get("/.well-known/agent-card.json").json()
        openapi_spec = client.get("/openapi.json").json()

    mcp_tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert len(mcp_tools) == 12
    assert {t["name"] for t in card["tools"]} == mcp_tools
    assert len(openapi_spec["paths"]) == 15
    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)
    keys._cache.clear()


def test_docs_contract_self_hygiene():
    source = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in source
    assert HASH_CHAR not in source
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    assert not [tok for tok in tokens if tok.type == tokenize.COMMENT]
