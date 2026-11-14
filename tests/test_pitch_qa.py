import asyncio
import io
import re
import tokenize
from pathlib import Path

from aimo import allocate, api, approvals, audit, keys, mandates, offers, orders, policy, quotes, recipes, substitute, valio
from aimo.app import app
from aimo.mcp_server import mcp
from aimo.models import Constraint, NeedItem, Product
from aimo.substitute import match_score
from tests.test_pitch_numbers import EXPECTED_MEASURED

ROOT = Path(__file__).resolve().parents[1]
QA_PATH = ROOT / "pitch" / "QA.md"
NUMBERS_PATH = ROOT / "pitch" / "NUMBERS.md"

EM_DASH = "\u2014"
EN_DASH = "\u2013"
HASH_CHAR = "\u0023"

EXPECTED_SECTIONS = [
    ("Working End-to-End Demo", 30),
    ("Precise Match to the Ask", 20),
    ("Autonomy and Controls", 20),
    ("Robustness to Exceptions", 15),
    ("Openness and Path to Production", 15),
]

REQUIRED_NUMBERS_CONSTANTS = (
    "17,546",
    "4,178",
    "8,110",
    "464119",
    "1,332.00",
    "1,512.00",
    "12",
)

REQUIRED_CODE_SEAMS = (
    "aimo/quotes.py",
    "aimo/policy.py",
    "aimo/substitute.py",
    "aimo/audit.py",
    "aimo/keys.py",
    "aimo/valio.py",
)

SECTION_RE = re.compile(rf"^{HASH_CHAR}{{2}}\s+\d+\.\s+(.+?)\s+\((\d+)\)\s*$")
QUESTION_RE = re.compile(rf"^{HASH_CHAR}{{3}}\s+Q:\s+(.+?)\s*$")
MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
REPO_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:"
    r"(?:aimo|agents|scenarios|scripts|tests)/[A-Za-z0-9_.-]+\.py"
    r"|(?:docs|deploy|pitch)/[A-Za-z0-9_.-]+\.md"
    r"|data_extra/[A-Za-z0-9_.-]+\.json"
    r")(?![A-Za-z0-9_])"
)
PLACEHOLDER_RE = re.compile(r"\[(?:TODO|MEASURE|TBD|FIXME|XXX)\b", re.IGNORECASE)


def parse_qa_document(markdown_text):
    sections = []
    current_section = None
    current_qa = None

    for raw_line in markdown_text.splitlines():
        sec_match = SECTION_RE.match(raw_line)
        if sec_match:
            if current_qa is not None and current_section is not None:
                current_qa["answer"] = "\n".join(current_qa["lines"]).strip()
                del current_qa["lines"]
                current_section["questions"].append(current_qa)
                current_qa = None
            current_section = {
                "title": sec_match.group(1),
                "weight": int(sec_match.group(2)),
                "questions": [],
            }
            sections.append(current_section)
            continue

        q_match = QUESTION_RE.match(raw_line)
        if q_match:
            assert current_section is not None, f"Question outside section: {raw_line}"
            if current_qa is not None:
                current_qa["answer"] = "\n".join(current_qa["lines"]).strip()
                del current_qa["lines"]
                current_section["questions"].append(current_qa)
            current_qa = {"question": q_match.group(1), "lines": []}
            continue

        if current_qa is not None:
            current_qa["lines"].append(raw_line)

    if current_qa is not None and current_section is not None:
        current_qa["answer"] = "\n".join(current_qa["lines"]).strip()
        del current_qa["lines"]
        current_section["questions"].append(current_qa)

    return sections


def count_spoken_sentences(answer_markdown):
    cleaned = re.sub(r"```[\s\S]*?```", " ", answer_markdown)
    cleaned = re.sub(r"`[^`]+`", "CODE", cleaned)
    cleaned = MD_LINK_RE.sub("LINK", cleaned)
    cleaned = re.sub(r"\d+\.\d+", "DEC", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return 0
    parts = [seg.strip(" )") for seg in re.split(r"[.!?]+(?:\s+|$)", cleaned)]
    return len([seg for seg in parts if seg])


def test_qa_file_hygiene_and_no_placeholders():
    assert QA_PATH.is_file()
    text = QA_PATH.read_text(encoding="utf-8")
    assert text.strip()
    assert EM_DASH not in text
    assert EN_DASH not in text
    assert PLACEHOLDER_RE.search(text) is None
    assert "[TODO" not in text
    assert "[MEASURE" not in text


def test_qa_links_and_referenced_code_seams_resolve():
    text = QA_PATH.read_text(encoding="utf-8")
    cleaned = re.sub(r"```[\s\S]*?```", "", text)
    cleaned = re.sub(r"`[^`]*`", "", cleaned)

    resolved_targets = []
    missing_links = []
    for match in MD_LINK_RE.finditer(cleaned):
        raw_target = match.group(2).strip().strip("<>").split()[0]
        if raw_target.startswith(("http://", "https://", "mailto:", "/")):
            continue
        if raw_target.startswith(HASH_CHAR):
            continue
        path_part = raw_target.split(HASH_CHAR, 1)[0]
        if not path_part:
            continue
        target_path = (QA_PATH.parent / path_part).resolve()
        resolved_targets.append(target_path)
        if not target_path.is_file():
            missing_links.append(raw_target)

    assert len(resolved_targets) >= 15
    assert not missing_links, f"Broken links in pitch/QA.md: {missing_links}"

    referenced_paths = sorted(set(REPO_PATH_RE.findall(text)))
    for seam in REQUIRED_CODE_SEAMS:
        assert seam in referenced_paths, f"Missing required code seam {seam} in pitch/QA.md"
    missing_files = [rel for rel in referenced_paths if not (ROOT / rel).is_file()]
    assert not missing_files, f"Missing referenced repo paths in pitch/QA.md: {missing_files}"


def test_qa_covers_all_five_judging_weights_and_sentence_budget():
    text = QA_PATH.read_text(encoding="utf-8")
    sections = parse_qa_document(text)

    observed = [(sec["title"], sec["weight"]) for sec in sections]
    assert observed == EXPECTED_SECTIONS
    assert [sec["weight"] for sec in sections] == [30, 20, 20, 15, 15]
    assert sum(sec["weight"] for sec in sections) == 100

    total_questions = 0
    for sec in sections:
        assert len(sec["questions"]) >= 3, f"Too few Q&A entries in {sec['title']}"
        for qa in sec["questions"]:
            total_questions += 1
            assert qa["question"].endswith("?"), qa["question"]
            answer = qa["answer"]
            assert answer, f"Empty answer for {qa['question']}"
            assert "```" not in answer, f"Code block not allowed in spoken answer: {qa['question']}"
            n_sentences = count_spoken_sentences(answer)
            assert 1 <= n_sentences <= 3, (
                f"Answer exceeds 3 sentences ({n_sentences}) for '{qa['question']}': {answer}"
            )

    assert total_questions >= 14


def test_sentence_counter_and_parser_detect_overlong_answers():
    three_sentences = (
        "Product `464119` costs `1,332.00 EUR` net via [aimo/offers.py](../aimo/offers.py). "
        "Auth swaps to OAuth 2.0 in `aimo/api.py` ([NUMBERS.md](NUMBERS.md)). "
        "Human approvers decide in `/inbox.html`."
    )
    assert count_spoken_sentences(three_sentences) == 3

    four_sentences = three_sentences + " This fourth sentence pushes the answer over the stage limit."
    assert count_spoken_sentences(four_sentences) == 4

    sample_md = "\n".join([
        f"{HASH_CHAR * 2} 1. Working End-to-End Demo (30)",
        "",
        f"{HASH_CHAR * 3} Q: Sample question?",
        "",
        four_sentences,
    ])
    parsed = parse_qa_document(sample_md)
    assert len(parsed) == 1
    assert parsed[0]["weight"] == 30
    assert count_spoken_sentences(parsed[0]["questions"][0]["answer"]) == 4


def test_qa_matches_numbers_md_and_live_code_constants(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "qa_check.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

    qa_text = QA_PATH.read_text(encoding="utf-8")
    numbers_text = NUMBERS_PATH.read_text(encoding="utf-8")

    for const in REQUIRED_NUMBERS_CONSTANTS:
        assert const in numbers_text, f"{const} missing from pitch/NUMBERS.md"
        assert const in qa_text, f"{const} missing from pitch/QA.md"

    products_total = EXPECTED_MEASURED["products"]
    lactose_free = EXPECTED_MEASURED["lactose_free_claim"]
    gluten_free = EXPECTED_MEASURED["gluten_free_claim"]
    any_allergen = EXPECTED_MEASURED["with_any_allergen_info"]
    missing_allergen = products_total - any_allergen
    assert f"{products_total:,}" == "17,546"
    assert f"{lactose_free:,}" == "4,178"
    assert f"{missing_allergen:,}" == "8,110"

    for derived in (
        f"{any_allergen:,}",
        f"{EXPECTED_MEASURED['with_allergen_declared']:,}",
        f"{EXPECTED_MEASURED['with_free_from_declared']:,}",
        f"{gluten_free:,}",
        f"{EXPECTED_MEASURED['reliability_rows']:,}",
        f"{EXPECTED_MEASURED['co2_rows']:,}",
    ):
        assert derived in numbers_text
        assert derived in qa_text

    for milk_fact in (
        "464119",
        "13.32",
        "1,332.00",
        "1,512.00",
        "464401",
        "1,100.00",
        "1,250.00",
        "464073",
        "1,640.00",
        "464201",
        "1,560.00",
        "3,632 ms",
        "9999999999",
    ):
        assert milk_fact in numbers_text
        assert milk_fact in qa_text

    milk_item = NeedItem(query="milk", quantity=1000, unit="L", constraints=Constraint(lactose_free=True))
    score_464119 = match_score(
        milk_item,
        Product(product_id="464119", name="Valio rasvaton maito 1 l laktoositon UHT", category="10400"),
    )
    score_464401 = match_score(
        milk_item,
        Product(
            product_id="464401",
            name="Valio Hyvä suomalainen Arki® Eila® rasvaton maitojuoma 1 l laktoositon",
            category="10400",
        ),
    )
    assert score_464119 == 0.965
    assert score_464401 == 0.75
    assert f"`{score_464119}`" in qa_text
    assert f"`{score_464401}`" in qa_text

    mcp_tools = asyncio.run(mcp.list_tools())
    assert len(mcp_tools) == 12
    openapi_paths = set(app.openapi().get("paths", {}))
    assert len(openapi_paths) == 15
    assert len(recipes.DISHES) == 11
    assert valio.CHUNK == 1000
    assert len(audit.GENESIS) == 64

    seeded = {m.customer: m for m in mandates.all()}
    assert f"{seeded['hotel-aurora'].per_order_eur:,.2f} EUR" in qa_text
    assert f"{seeded['hospital-tays'].per_order_eur:,.2f} EUR" in qa_text
    assert f"{seeded['school-hervanta'].per_order_eur:,.2f} EUR" in qa_text
    assert f"{seeded['hotel-aurora'].per_week_eur:,.2f} EUR" in qa_text
    assert f"{seeded['hospital-tays'].per_week_eur:,.2f} EUR" in qa_text
    assert f"{seeded['school-hervanta'].per_week_eur:,.2f} EUR" in qa_text
    for allergen in seeded["hospital-tays"].exclude_allergens:
        assert f"`{allergen}`" in qa_text


def test_qa_domain_codes_and_module_symbols_match_gateway_source():
    qa_text = QA_PATH.read_text(encoding="utf-8")
    aimo_sources = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((ROOT / "aimo").glob("*.py"))
    )

    expected_codes = (
        "ALLERGEN_CONFLICT",
        "UNKNOWN_PRODUCT",
        "BAD_SIGNATURE",
        "QUOTE_USED",
        "WRONG_AGENT",
        "NOT_APPROVER",
        "QUOTE_EXPIRED",
        "NOT_PENDING",
        "OVER_WEEK_LIMIT",
        "STOCKOUT",
        "UPSTREAM_UNAVAILABLE",
        "SUBSTITUTION_NEEDS_APPROVAL",
    )
    for code in expected_codes:
        assert f"`{code}`" in qa_text
        assert f'"{code}"' in aimo_sources or f"'{code}'" in aimo_sources

    expected_symbols = [
        (substitute, "allergen_safe"),
        (substitute, "rank_substitutes"),
        (substitute, "match_score"),
        (offers, "get_offers"),
        (quotes, "issue"),
        (quotes, "verify"),
        (quotes, "redeem"),
        (api, "resolve_admin"),
        (approvals, "decide"),
        (orders, "spent_this_week"),
        (orders, "react_stockout"),
        (orders, "_confirm"),
        (policy, "decide"),
        (allocate, "stockout_plan"),
        (valio, "_fetch"),
        (keys, "rotate"),
        (keys, "jwks"),
        (audit, "verify_checkpoint"),
    ]
    for mod, attr in expected_symbols:
        assert callable(getattr(mod, attr, None)), f"{mod.__name__}.{attr} missing"
        assert attr in qa_text


def test_qa_routes_and_section_topic_coverage():
    sections = parse_qa_document(QA_PATH.read_text(encoding="utf-8"))
    by_weight_title = {sec["title"]: sec for sec in sections}

    section_keywords = {
        "Working End-to-End Demo": ("9999999999", "MOCK-", "agent-card.json"),
        "Precise Match to the Ask": ("464401", "8,110", "data_extra/recipes.json", "11,033"),
        "Autonomy and Controls": ("BAD_SIGNATURE", "NOT_APPROVER", "OVER_WEEK_LIMIT", "verify-checkpoint"),
        "Robustness to Exceptions": ("react_stockout", "UPSTREAM_UNAVAILABLE", "SUBSTITUTION_NEEDS_APPROVAL", "drift.py"),
        "Openness and Path to Production": ("retired-", "PostgreSQL", "seller.py"),
    }
    for title, required_tokens in section_keywords.items():
        combined = " ".join(
            qa["question"] + " " + qa["answer"] for qa in by_weight_title[title]["questions"]
        )
        for tok in required_tokens:
            assert tok in combined, f"Expected '{tok}' in section '{title}'"

    openapi_paths = set(app.openapi().get("paths", {}))
    route_paths = {getattr(r, "path", None) for r in app.routes}
    all_paths = openapi_paths | route_paths
    for path in (
        "/health",
        "/.well-known/agent-card.json",
        "/.well-known/jwks.json",
        "/openapi.json",
        "/mcp",
        "/v1/approvals/{order_id}",
        "/v1/mandate",
        "/v1/audit",
        "/v1/admin/simulate",
    ):
        assert path in all_paths, f"Missing route {path}"


def test_test_pitch_qa_self_hygiene():
    source = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in source
    assert EN_DASH not in source
    assert HASH_CHAR not in source
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    assert not [tok for tok in tokens if tok.type == tokenize.COMMENT]
