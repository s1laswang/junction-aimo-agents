import ast
import asyncio
import io
import json
import re
import tokenize
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimo import mandates
from aimo.api import ParseResponse
from aimo.app import app
from aimo.models import Order, Quote
from pitch.measure_data import _resolve_data_dir
from scripts.smoke_demo import CHECKS as SMOKE_CHECKS, EXPECTED_TOOLS
from tests.test_pitch_numbers import EXPECTED_MEASURED

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = "\u2014"
HASH_CHAR = "\u0023"

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

EXPECTED_PUBLIC_URL_PATHS = {
    "/health",
    "/.well-known/agent-card.json",
    "/.well-known/jwks.json",
    "/openapi.json",
    "/v1/products",
    "/v1/products/{product_id}",
    "/v1/needs/parse",
    "/v1/offers",
    "/v1/quotes",
    "/v1/orders",
    "/v1/orders/{order_id}",
    "/v1/approvals",
    "/v1/approvals/{order_id}",
    "/v1/audit",
    "/v1/mandate",
    "/v1/admin/simulate",
}

EXPECTED_SEEDED_CUSTOMERS = {
    "hotel-aurora": {
        "per_order_eur": 2500.0,
        "per_week_eur": 15000.0,
        "exclude_allergens": [],
        "substitutions": "within_prefs",
        "approvers": ["Laura Virtanen", "Mikko Heikkinen"],
    },
    "hospital-tays": {
        "per_order_eur": 1500.0,
        "per_week_eur": 8000.0,
        "exclude_allergens": ["peanut", "nuts", "egg"],
        "substitutions": "ask",
        "approvers": ["Helena Korhonen", "Antti Lahtinen"],
    },
    "school-hervanta": {
        "per_order_eur": 600.0,
        "per_week_eur": 3000.0,
        "exclude_allergens": ["peanut", "nuts"],
        "substitutions": "within_prefs",
        "approvers": ["Sari Nieminen"],
    },
}

JUDGING_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:"
    r"(?:aimo|agents|scenarios|scripts|tests)/[A-Za-z0-9_.-]+\.py"
    r"|(?:docs|deploy)/[A-Za-z0-9_.-]+\.md"
    r"|(?:web|pitch|data_extra)/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9]+)+"
    r")(?![A-Za-z0-9_])"
)

JUDGING_SYMBOL_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(tests/test_[A-Za-z0-9_]+\.py)::(?:[A-Za-z0-9_]+::)?(test_[A-Za-z0-9_]+)"
)

JUDGING_PAREN_SYMBOLS_RE = re.compile(
    r"`(tests/test_[A-Za-z0-9_]+\.py)`\s*\(((?:`test_[A-Za-z0-9_]+`(?:\s*,\s*`test_[A-Za-z0-9_]+`)*))\)"
)

ROUTE_CELL_RE = re.compile(r"`(GET|POST|PUT|DELETE|PATCH)\s+(/[^`\s*]+)`")

ALLERGEN_TOKEN_RE = re.compile(
    r"\b(peanut|peanuts|nuts|egg|eggs|milk|lactose|gluten|wheat|soy|fish|celery|mustard|sesame)\b"
)

ALLERGEN_CANONICAL = {
    "peanuts": "peanut",
    "eggs": "egg",
}

PRODUCT_JSON_COUNT_RE = re.compile(
    r"(?:product\s+json|product_data\.json)[^\n]*?`?([\d,]+)`?\s+(?:active\s+)?(?:items|products)\b",
    re.IGNORECASE,
)

JSON_STREAM_TOKEN_RE = re.compile(
    rb'"(?:\\.|[^"\\])*"'
    rb'|(?P<inc>"(?:\\.|[^"\\])*\\?\Z)'
    rb"|(?P<struct>[\[\]{},])"
    rb'|(?P<prim>[^\s\[\]{},":]+)',
    re.DOTALL,
)


def extract_judging_paths(markdown_text):
    return sorted(set(JUDGING_PATH_RE.findall(markdown_text)))


def extract_judging_test_symbols(markdown_text, include_parenthetical=True):
    refs = set(JUDGING_SYMBOL_RE.findall(markdown_text))
    if include_parenthetical:
        for rel_file, group in JUDGING_PAREN_SYMBOLS_RE.findall(markdown_text):
            stem = Path(rel_file).stem
            for fn in re.findall(r"`(test_[A-Za-z0-9_]+)`", group):
                if fn != stem:
                    refs.add((rel_file, fn))
    return sorted(refs)


def defined_function_names(py_path):
    tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def extract_public_url_routes(markdown_text):
    documented = {}
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        first_col = line[1:-1].split("|", 1)[0]
        for method, raw_path in ROUTE_CELL_RE.findall(first_col):
            path = raw_path.split("?", 1)[0]
            documented.setdefault(path, set()).add(method.upper())
    return documented


def registered_app_routes(fastapi_app):
    routes = {}
    for path, ops in fastapi_app.openapi().get("paths", {}).items():
        methods = {m.upper() for m in ops if m.upper() in HTTP_METHODS}
        if methods:
            routes.setdefault(path, set()).update(methods)
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        raw_methods = getattr(route, "methods", None) or set()
        methods = {m.upper() for m in raw_methods if m.upper() in HTTP_METHODS}
        if path and methods:
            routes.setdefault(path, set()).update(methods)
    return routes


def extract_mcp_clients_tools(markdown_text):
    match = re.search(r"(?m)^(\d+)\s+(\[[A-Za-z0-9_',\s\"]+\])\s*$", markdown_text)
    assert match is not None, "MCP tool list line not found in deploy/mcp-clients.md"
    count = int(match.group(1))
    tools = ast.literal_eval(match.group(2))
    return count, tools


def _find_header_index(headers, keywords):
    for idx, h in enumerate(headers):
        if any(kw in h for kw in keywords):
            return idx
    return None


def _parse_allergens(text):
    return {ALLERGEN_CANONICAL.get(tok, tok) for tok in ALLERGEN_TOKEN_RE.findall(text.lower())}


def parse_assumptions_mandates(markdown_text):
    parsed = {}
    headers = []
    known_approvers = [
        name for spec in EXPECTED_SEEDED_CUSTOMERS.values() for name in spec["approvers"]
    ]
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            headers = []
            continue
        cols = [c.strip() for c in line[1:-1].split("|")]
        if len(cols) < 3:
            continue
        if all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cols if c):
            continue
        customer = cols[0].strip("` ").strip()
        if customer not in EXPECTED_SEEDED_CUSTOMERS:
            headers = [c.replace("`", "").strip().lower() for c in cols]
            continue

        plain = line.replace("`", "")
        lower = plain.lower()
        order_col = _find_header_index(headers, ("per order", "per-order", "order limit"))
        week_col = _find_header_index(headers, ("per week", "per-week", "weekly", "week limit"))
        allergen_col = _find_header_index(headers, ("allergen", "exclude"))
        sub_col = _find_header_index(headers, ("substitut",))
        approver_col = _find_header_index(headers, ("approver",))

        order_m = re.search(r"([\d,]+(?:\.\d+)?)\s*eur\s*(?:per\s*|/\s*)?order", lower)
        week_m = re.search(r"([\d,]+(?:\.\d+)?)\s*eur\s*(?:per\s*|/\s*)?week", lower)
        if order_m and week_m:
            per_order = float(order_m.group(1).replace(",", ""))
            per_week = float(week_m.group(1).replace(",", ""))
        elif order_col is not None and week_col is not None and max(order_col, week_col) < len(cols):
            om = re.search(r"([\d,]+(?:\.\d+)?)", cols[order_col])
            wm = re.search(r"([\d,]+(?:\.\d+)?)", cols[week_col])
            assert om and wm, f"Missing numeric limits for {customer} in docs/ASSUMPTIONS.md"
            per_order = float(om.group(1).replace(",", ""))
            per_week = float(wm.group(1).replace(",", ""))
        else:
            eur_vals = re.findall(r"([\d,]+(?:\.\d+)?)\s*eur", lower)
            assert len(eur_vals) >= 2, f"Missing EUR limits for {customer} in docs/ASSUMPTIONS.md"
            per_order = float(eur_vals[0].replace(",", ""))
            per_week = float(eur_vals[1].replace(",", ""))

        if allergen_col is not None and allergen_col < len(cols):
            allergens = _parse_allergens(cols[allergen_col])
        else:
            excl_m = re.search(
                r"exclud[a-z_]*\s*:?\s*([^;|]*?)(?=\bsubstitut|\bapprover|\bprefer|$)",
                lower,
            )
            allergens = _parse_allergens(excl_m.group(1)) if excl_m else set()

        sub_source = (
            cols[sub_col].replace("`", "").lower()
            if sub_col is not None and sub_col < len(cols)
            else lower
        )
        if "within_prefs" in sub_source or "within preferences" in sub_source:
            substitutions = "within_prefs"
        elif re.search(r"\bask\b", sub_source):
            substitutions = "ask"
        elif re.search(r"\bnever\b", sub_source):
            substitutions = "never"
        else:
            raise AssertionError(f"Unrecognized substitutions rule for {customer}: {plain}")

        has_approvers = approver_col is not None or any(name in plain for name in known_approvers)
        parsed[customer] = {
            "per_order_eur": per_order,
            "per_week_eur": per_week,
            "exclude_allergens": allergens,
            "substitutions": substitutions,
            "has_approvers": has_approvers,
            "raw_row": plain,
        }
    return parsed


def extract_mcp_clients_json_lines(markdown_text):
    results = []
    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(obj, dict):
                results.append(obj)
    return results


def extract_mcp_clients_mandate_jsons(markdown_text):
    return [obj for obj in extract_mcp_clients_json_lines(markdown_text) if "per_order_eur" in obj]


def count_json_array_items(json_path, chunk_size=262144):
    depth = 0
    in_element = False
    count = 0
    carry = b""
    with open(json_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            eof = not chunk
            buf = carry + chunk
            carry = b""
            if not buf:
                break
            for m in JSON_STREAM_TOKEN_RE.finditer(buf):
                kind = m.lastgroup
                if kind == "inc":
                    if eof:
                        raise ValueError("Unterminated JSON string")
                    carry = m.group(0)
                    break
                if kind == "prim" and not eof and m.end() == len(buf):
                    carry = m.group(0)
                    break
                tok = m.group(0)
                if kind == "struct":
                    if tok in (b"[", b"{"):
                        if depth == 1 and not in_element:
                            count += 1
                            in_element = True
                        depth += 1
                    elif tok in (b"]", b"}"):
                        depth -= 1
                        if depth == 0:
                            return count
                    elif tok == b"," and depth == 1:
                        in_element = False
                elif depth == 1 and not in_element:
                    count += 1
                    in_element = True
            if eof:
                break
    raise ValueError("JSON top-level array did not close")


def require_catalog_data_json():
    data_json = _resolve_data_dir() / "valio_aimo_junction26_product_data.json"
    if not data_json.exists():
        pytest.skip("no data")
    return data_json


@pytest.fixture(scope="module")
def measured_catalog_count():
    data_json = require_catalog_data_json()
    return count_json_array_items(data_json)


def test_judging_md_proof_files_and_symbols_exist():
    text = (ROOT / "docs" / "JUDGING.md").read_text(encoding="utf-8")
    rel_paths = extract_judging_paths(text)
    assert len(rel_paths) >= 40
    missing_files = [rel for rel in rel_paths if not (ROOT / rel).is_file()]
    assert not missing_files, f"Missing files in docs/JUDGING.md: {missing_files}"

    colon_refs = extract_judging_test_symbols(text, include_parenthetical=False)
    assert len(colon_refs) >= 10
    all_symbol_refs = extract_judging_test_symbols(text, include_parenthetical=True)

    fn_cache = {}
    missing_symbols = []
    for rel_file, func_name in all_symbol_refs:
        target = ROOT / rel_file
        if not target.is_file():
            missing_symbols.append(f"{rel_file} (missing file)")
            continue
        if rel_file not in fn_cache:
            fn_cache[rel_file] = defined_function_names(target)
        if func_name not in fn_cache[rel_file]:
            missing_symbols.append(f"{rel_file}::{func_name}")
    assert not missing_symbols, f"Missing test symbols in docs/JUDGING.md: {missing_symbols}"


def test_judging_extractors_catch_missing_symbol(tmp_path):
    sample_test = tmp_path / "test_demo.py"
    sample_test.write_text("def test_present():\n    assert True\n", encoding="utf-8")
    names = defined_function_names(sample_test)
    assert "test_present" in names
    assert "test_absent" not in names

    snippet = (
        "| Proof | `aimo/api.py`, `web/console.js`, `data_extra/recipes.json`, "
        "`tests/test_flow.py::test_milk_flow`, `tests/test_parse.py` (`test_milk_en`, `test_allergens`) |"
    )
    assert extract_judging_paths(snippet) == [
        "aimo/api.py",
        "data_extra/recipes.json",
        "tests/test_flow.py",
        "tests/test_parse.py",
        "web/console.js",
    ]
    assert extract_judging_test_symbols(snippet) == [
        ("tests/test_flow.py", "test_milk_flow"),
        ("tests/test_parse.py", "test_allergens"),
        ("tests/test_parse.py", "test_milk_en"),
    ]


def test_public_url_routes_and_mcp_tool_parity(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "parity.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

    public_url_text = (ROOT / "deploy" / "PUBLIC_URL.md").read_text(encoding="utf-8")
    documented_routes = extract_public_url_routes(public_url_text)
    assert set(documented_routes) == EXPECTED_PUBLIC_URL_PATHS

    registered_routes = registered_app_routes(app)
    for path in sorted(EXPECTED_PUBLIC_URL_PATHS):
        assert path in registered_routes, f"Route {path} not registered on aimo.app.app"
        assert registered_routes[path] == documented_routes[path], (
            f"{path}: deploy/PUBLIC_URL.md has {sorted(documented_routes[path])}, "
            f"aimo.app.app has {sorted(registered_routes[path])}"
        )
    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)

    mcp_clients_text = (ROOT / "deploy" / "mcp-clients.md").read_text(encoding="utf-8")
    doc_count, doc_tools = extract_mcp_clients_tools(mcp_clients_text)
    assert doc_count == 12
    assert len(doc_tools) == 12
    assert len(set(doc_tools)) == 12
    assert set(doc_tools) == EXPECTED_TOOLS

    with TestClient(app) as client:
        card_resp = client.get("/.well-known/agent-card.json")
        assert card_resp.status_code == 200
        card = card_resp.json()
        card_tools = [t["name"] for t in card["tools"]]
        assert len(card_tools) == 12
        assert set(card_tools) == set(doc_tools) == EXPECTED_TOOLS

    from aimo import mcp_server

    mcp_obj = getattr(mcp_server, "mcp", None) or getattr(mcp_server, "server", None)
    assert mcp_obj is not None
    live_mcp_names = {t.name for t in asyncio.run(mcp_obj.list_tools())}
    assert live_mcp_names == EXPECTED_TOOLS


def test_seeded_mandates_match_assumptions_and_mcp_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "mandates.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

    seeded = {m.customer: m for m in mandates.all()}
    assert set(seeded) == set(EXPECTED_SEEDED_CUSTOMERS)

    for customer, expected in EXPECTED_SEEDED_CUSTOMERS.items():
        m = seeded[customer]
        assert m.per_order_eur == expected["per_order_eur"]
        assert m.per_week_eur == expected["per_week_eur"]
        assert m.exclude_allergens == expected["exclude_allergens"]
        assert m.substitutions == expected["substitutions"]
        assert m.approvers == expected["approvers"]

    assumptions_text = (ROOT / "docs" / "ASSUMPTIONS.md").read_text(encoding="utf-8")
    assumptions_mandates = parse_assumptions_mandates(assumptions_text)
    assert set(assumptions_mandates) == set(EXPECTED_SEEDED_CUSTOMERS)

    for customer, m in seeded.items():
        doc_row = assumptions_mandates[customer]
        assert doc_row["per_order_eur"] == m.per_order_eur, customer
        assert doc_row["per_week_eur"] == m.per_week_eur, customer
        assert doc_row["exclude_allergens"] == set(m.exclude_allergens), customer
        assert doc_row["substitutions"] == m.substitutions, customer
        if doc_row["has_approvers"]:
            for approver in m.approvers:
                assert approver in doc_row["raw_row"], f"Missing approver {approver} for {customer}"

    mcp_clients_text = (ROOT / "deploy" / "mcp-clients.md").read_text(encoding="utf-8")
    for demo_key, (customer, agent_id) in mandates._DEMO_KEYS.items():
        assert f"`{demo_key}`" in mcp_clients_text
        resolved = mandates.agent_for_key(demo_key)
        assert resolved == (customer, agent_id, seeded[customer])

    json_lines = extract_mcp_clients_json_lines(mcp_clients_text)
    json_mandates = [obj for obj in json_lines if "per_order_eur" in obj]
    assert len(json_mandates) >= 2
    for payload in json_mandates:
        customer = payload["customer"]
        assert customer in seeded
        expected_dump = seeded[customer].model_dump()
        assert payload["per_order_eur"] == expected_dump["per_order_eur"]
        assert payload["per_week_eur"] == expected_dump["per_week_eur"]
        assert payload["exclude_allergens"] == expected_dump["exclude_allergens"]
        assert payload["approvers"] == expected_dump["approvers"]
        assert payload == expected_dump

    parse_payloads = [obj for obj in json_lines if "deliver_by" in obj and "items" in obj]
    quote_payloads = [obj for obj in json_lines if "quote_id" in obj and "order_id" not in obj]
    order_payloads = [obj for obj in json_lines if "order_id" in obj]
    for payload in parse_payloads:
        parsed_need = ParseResponse.model_validate(payload)
        assert parsed_need.customer in seeded
        assert parsed_need.items
    quotes_by_id = {}
    for payload in quote_payloads:
        q = Quote.model_validate(payload)
        assert q.customer in seeded
        for line in q.lines:
            assert round(line.quantity * line.unit_price_net, 2) == round(line.line_total_net, 2)
        assert round(sum(line.line_total_net for line in q.lines), 2) == round(q.total_net, 2)
        quotes_by_id[q.quote_id] = q
    for payload in order_payloads:
        o = Order.model_validate(payload)
        assert o.customer in seeded
        if o.quote_id in quotes_by_id:
            q = quotes_by_id[o.quote_id]
            assert o.customer == q.customer
            assert o.agent_id == q.agent_id
            assert o.total_net == q.total_net

    with TestClient(app) as client:
        for demo_key, (customer, _) in mandates._DEMO_KEYS.items():
            resp = client.get("/v1/mandate", headers={"X-Agent-Key": demo_key})
            assert resp.status_code == 200
            assert resp.json() == seeded[customer].model_dump()


def test_parse_assumptions_mandates_variants():
    multi_col = "\n".join([
        "| Customer | Per order (EUR) | Per week (EUR) | Excluded allergens | Substitutions | Approvers |",
        "|---|---|---|---|---|---|",
        "| `hotel-aurora` | 2,500 | 15,000 | none | `within_prefs` | Laura Virtanen, Mikko Heikkinen |",
        "| `hospital-tays` | 1,500 | 8,000 | peanuts, nuts, eggs | `ask` | Helena Korhonen, Antti Lahtinen |",
        "| `school-hervanta` | 600 | 3,000 | peanut, nuts | `within_prefs` | Sari Nieminen |",
    ])
    parsed = parse_assumptions_mandates(multi_col)
    assert set(parsed) == set(EXPECTED_SEEDED_CUSTOMERS)
    assert parsed["hotel-aurora"]["exclude_allergens"] == set()
    assert parsed["hospital-tays"]["exclude_allergens"] == {"peanut", "nuts", "egg"}
    assert parsed["school-hervanta"]["exclude_allergens"] == {"peanut", "nuts"}
    assert all(row["has_approvers"] for row in parsed.values())


def test_catalog_headline_count_matches_docs_offline():
    products_count = EXPECTED_MEASURED["products"]
    formatted_count = f"{products_count:,}"
    assert formatted_count == "17,546"
    lactose_free_count = f"{EXPECTED_MEASURED['lactose_free_claim']:,}"
    assert lactose_free_count == "4,178"

    for rel in (
        "CONTRACT.md",
        "docs/ASSUMPTIONS.md",
        "README.md",
        "pitch/NUMBERS.md",
        "pitch/PITCH.md",
        "pitch/DECK.md",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert formatted_count in text, f"{formatted_count} missing from {rel}"

    for rel in (
        "docs/ASSUMPTIONS.md",
        "README.md",
        "pitch/NUMBERS.md",
        "pitch/PITCH.md",
        "pitch/DECK.md",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert lactose_free_count in text, f"{lactose_free_count} missing from {rel}"

    for rel in ("CONTRACT.md", "docs/ASSUMPTIONS.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        matches = PRODUCT_JSON_COUNT_RE.findall(text)
        assert matches, f"Product JSON item count line missing from {rel}"
        for raw_num in matches:
            assert int(raw_num.replace(",", "")) == products_count, rel

    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "smoke:" in makefile_text
    assert "scripts/smoke_demo.py" in makefile_text
    assert len(SMOKE_CHECKS) == 6


def test_catalog_headline_count_matches_contract_and_assumptions(measured_catalog_count):
    assert measured_catalog_count == EXPECTED_MEASURED["products"] == 17546
    formatted_count = f"{measured_catalog_count:,}"
    for rel in ("CONTRACT.md", "docs/ASSUMPTIONS.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert formatted_count in text, f"{formatted_count} missing from {rel}"
        matches = PRODUCT_JSON_COUNT_RE.findall(text)
        assert matches, f"Product JSON item count line missing from {rel}"
        for raw_num in matches:
            assert int(raw_num.replace(",", "")) == measured_catalog_count, rel


def test_count_json_array_items_streaming(tmp_path):
    empty_file = tmp_path / "empty.json"
    empty_file.write_text(" [ \n ] ", encoding="utf-8")
    assert count_json_array_items(empty_file, chunk_size=1) == 0

    tricky_payload = [
        {"id": "1", "text": 'brackets [] {} and escaped \\"quote\\" and slash \\\\', "nested": [1, 2, {"x": 3}]},
        {"id": "2", "flag": True, "empty": [], "note": "maito"},
        "scalar_item",
        42,
    ]
    tricky_file = tmp_path / "tricky.json"
    tricky_file.write_text(json.dumps(tricky_payload), encoding="utf-8")
    for size in (1, 7, 64, 262144):
        assert count_json_array_items(tricky_file, chunk_size=size) == 4


def test_live_milk_flow_numbers_consistent_across_docs():
    for rel in ("README.md", "pitch/PITCH.md", "pitch/NUMBERS.md", "deploy/mcp-clients.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "464119" in text, rel
        assert "13.32" in text, rel
        assert "1,332" in text or "1332.0" in text, rel
        assert "1,512" in text or "1512.0" in text, rel
        assert "hotel-aurora" in text, rel
        assert "WITHIN_MANDATE" in text, rel


def test_catalog_headline_skips_cleanly_without_data(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "missing_data"))
    with pytest.raises(pytest.skip.Exception):
        require_catalog_data_json()


def test_pitch_consistency_hygiene():
    source = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in source
    assert HASH_CHAR not in source
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    assert not [tok for tok in tokens if tok.type == tokenize.COMMENT]
