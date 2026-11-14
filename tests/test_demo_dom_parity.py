from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import tokenize

from aimo import mandates

ROOT = Path(__file__).resolve().parents[1]
DEMO_MD = ROOT / "pitch" / "DEMO.md"
JUDGING_MD = ROOT / "docs" / "JUDGING.md"
WEB_DIR = ROOT / "web"
SCENARIOS_DIR = ROOT / "scenarios"

EM_DASH = "\u2014"
HASH_CHAR = "\u0023"

EXPECTED_CONSOLE_CHIPS = [
    ("milk", "Lactose-free milk"),
    ("lasagne", "Menu: lasagne (40)"),
    ("school", "Over limit, school"),
    ("hospital", "Allergen-safe hospital"),
    ("event", "Last-minute event"),
]

EXPECTED_CHIP_CUSTOMERS = {
    "milk": "hotel-aurora",
    "lasagne": "hotel-aurora",
    "school": "school-hervanta",
    "hospital": "hospital-tays",
    "event": "hotel-aurora",
}

EXPECTED_SCENE_WEIGHTS = [
    (1, 30),
    (2, 20),
    (3, 20),
    (4, 15),
    (5, 15),
]

EXPECTED_SCENARIO_SCRIPTS = [
    "s0_guardrails.py",
    "s1_milk.py",
    "s2_need_recipe_order.py",
    "s3_event_substitution.py",
    "s4_stockout.py",
    "s5_allergen_safe.py",
    "s6_proactive_seller.py",
    "s7_drift.py",
]

EXPECTED_DEMO_APPROVERS = {
    "school-hervanta": ["Sari Nieminen"],
    "hotel-aurora": ["Laura Virtanen", "Mikko Heikkinen"],
}

EXPECTED_DEMO_LIMIT_STRINGS = {
    "hotel-aurora": "2,500",
    "school-hervanta": "600",
}

EXPECTED_NAV_ITEMS = [
    ("/console.html", "Console"),
    ("/inbox.html", "Approvals"),
    ("/audit.html", "Audit"),
    ("/admin.html", "Mandates"),
]

JS_TOKEN_RE = re.compile(
    r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|([,{]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)'
)

SCENE_HEADER_RE = re.compile(
    rf"(?m)^{HASH_CHAR}{{3}}\s+Scene\s+(\d+)\b([^\n]*)$"
)

JUDGING_CRITERION_RE = re.compile(
    r"(?m)^\|\s*([A-Za-z][A-Za-z0-9 -]+?)\s*\((\d+)\)\s*\|"
)


class _ConsoleAndNavParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.in_scenarios = 0
        self.in_nav = 0
        self._active_chip_key = None
        self._chip_buf = []
        self.chips = []
        self._active_nav_href = None
        self._nav_buf = []
        self.nav_links = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        elem_id = attr_map.get("id")
        if elem_id:
            self.ids.add(elem_id)
        if self.in_scenarios > 0:
            self.in_scenarios += 1
        elif elem_id == "scenarios":
            self.in_scenarios = 1
        if self.in_nav > 0:
            self.in_nav += 1
        elif tag == "nav":
            self.in_nav = 1
        if self.in_scenarios > 0 and tag == "button" and "data-s" in attr_map:
            self._active_chip_key = attr_map["data-s"]
            self._chip_buf = []
        if self.in_nav > 0 and tag == "a" and "href" in attr_map:
            self._active_nav_href = attr_map["href"]
            self._nav_buf = []

    def handle_data(self, data):
        if self._active_chip_key is not None:
            self._chip_buf.append(data)
        if self._active_nav_href is not None:
            self._nav_buf.append(data)

    def handle_endtag(self, tag):
        if self._active_chip_key is not None and tag == "button":
            label = "".join(self._chip_buf).strip()
            self.chips.append((self._active_chip_key, label))
            self._active_chip_key = None
            self._chip_buf = []
        if self._active_nav_href is not None and tag == "a":
            label = "".join(self._nav_buf).strip()
            self.nav_links.append((self._active_nav_href, label))
            self._active_nav_href = None
            self._nav_buf = []
        if self.in_scenarios > 0:
            self.in_scenarios -= 1
        if self.in_nav > 0:
            self.in_nav -= 1


def parse_html_dom(html_text):
    parser = _ConsoleAndNavParser()
    parser.feed(html_text)
    return parser


def extract_js_object(js_text, var_name):
    decl_re = re.compile(rf"\b(?:const|let|var)\s+{re.escape(var_name)}\s*=\s*\{{")
    match = decl_re.search(js_text)
    assert match is not None, f"JS object {var_name} not found"
    start = match.end() - 1
    depth = 0
    in_str = None
    escaped = False
    end = None
    for idx in range(start, len(js_text)):
        ch = js_text[idx]
        if in_str is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    assert end is not None, f"Unclosed JS object {var_name}"
    raw_obj = js_text[start:end]

    def _quote_key(m):
        literal = m.group(1)
        if literal is not None:
            if literal.startswith("'"):
                inner = literal[1:-1].replace('\\"', '"').replace("\\'", "'")
                return json.dumps(inner)
            return literal
        return f'{m.group(2)}"{m.group(3)}"{m.group(4)}'

    normalized = JS_TOKEN_RE.sub(_quote_key, raw_obj)
    normalized = re.sub(r":\s*\.(\d+)", r": 0.\1", normalized)
    return json.loads(normalized)


def extract_scene_weights(demo_text):
    scenes = []
    for num_str, rest in SCENE_HEADER_RE.findall(demo_text):
        pts_match = re.search(r"\((\d+)\s*pts\)", rest)
        assert pts_match is not None, f"Missing (N pts) in Scene {num_str}: {rest}"
        raw_token = pts_match.group(0)
        scenes.append((int(num_str), int(pts_match.group(1)), raw_token))
    return scenes


def extract_demo_approver_clauses(demo_text):
    clause_match = re.search(r"pick an approver from the mandate\s*\(([^)]+)\)", demo_text)
    assert clause_match is not None, "Approver mandate clause missing in pitch/DEMO.md"
    mapping = {}
    for part in clause_match.group(1).split(";"):
        cust_match = re.search(r"for\s+`([a-z0-9-]+)`", part)
        assert cust_match is not None, f"Customer missing in approver clause: {part}"
        customer = cust_match.group(1)
        before_for = part[: cust_match.start()]
        names = re.findall(r"`([^`]+)`", before_for)
        assert names, f"Approver names missing for {customer}: {part}"
        mapping[customer] = names
    return mapping


def test_console_chips_match_demo_md_and_console_dom_and_js():
    demo_text = DEMO_MD.read_text(encoding="utf-8")
    console_html = (WEB_DIR / "console.html").read_text(encoding="utf-8")
    console_js = (WEB_DIR / "console.js").read_text(encoding="utf-8")

    dom = parse_html_dom(console_html)
    assert dom.chips == EXPECTED_CONSOLE_CHIPS
    assert {"scenarios", "req", "text", "run", "result", "trace", "customer"}.issubset(dom.ids)

    texts_obj = extract_js_object(console_js, "TEXTS")
    cust_obj = extract_js_object(console_js, "CUST")

    chip_keys = [key for key, _ in EXPECTED_CONSOLE_CHIPS]
    assert set(chip_keys) == set(texts_obj) == set(cust_obj)
    assert cust_obj == EXPECTED_CHIP_CUSTOMERS

    for key, label in EXPECTED_CONSOLE_CHIPS:
        assert f'"{label}"' in demo_text, f'Chip "{label}" missing from pitch/DEMO.md'
        assert re.search(rf'\bclick chip "{re.escape(label)}"', demo_text, re.IGNORECASE), (
            f'Click action for "{label}" missing from pitch/DEMO.md'
        )
        assert label in console_html
        prompt = texts_obj[key]
        assert isinstance(prompt, str) and prompt.strip()

    assert texts_obj["lasagne"] in demo_text
    assert texts_obj["hospital"] in demo_text


def test_scene_headers_point_weights_sum_to_100():
    demo_text = DEMO_MD.read_text(encoding="utf-8")
    scenes = extract_scene_weights(demo_text)
    assert [(num, pts) for num, pts, _ in scenes] == EXPECTED_SCENE_WEIGHTS
    assert [raw for _, _, raw in scenes] == [
        "(30 pts)",
        "(20 pts)",
        "(20 pts)",
        "(15 pts)",
        "(15 pts)",
    ]
    assert sum(pts for _, pts, _ in scenes) == 100

    judging_text = JUDGING_MD.read_text(encoding="utf-8")
    judging_weights = [int(w) for _, w in JUDGING_CRITERION_RE.findall(judging_text)]
    assert judging_weights == [pts for _, pts in EXPECTED_SCENE_WEIGHTS]
    assert sum(judging_weights) == 100


def test_every_scenario_script_referenced_in_demo_md_and_exists():
    demo_text = DEMO_MD.read_text(encoding="utf-8")
    disk_scripts = sorted(p.name for p in SCENARIOS_DIR.glob("s*.py") if p.is_file())
    assert disk_scripts == EXPECTED_SCENARIO_SCRIPTS

    for filename in EXPECTED_SCENARIO_SCRIPTS:
        script_path = SCENARIOS_DIR / filename
        assert script_path.is_file() and script_path.stat().st_size > 0
        stem = script_path.stem
        assert re.search(rf"\b{re.escape(stem)}(?:\.py)?\b", demo_text), (
            f"Scenario {filename} not referenced in pitch/DEMO.md"
        )

    explicit_rel_paths = set(
        re.findall(r"(?:scenarios|scripts)/[A-Za-z0-9_]+\.py", demo_text)
    )
    assert explicit_rel_paths
    for rel in sorted(explicit_rel_paths):
        assert (ROOT / rel).is_file(), f"Referenced script missing on disk: {rel}"

    explicit_scen_files = set(re.findall(r"\b(s\d_[A-Za-z0-9_]+\.py)\b", demo_text))
    assert explicit_scen_files
    for name in sorted(explicit_scen_files):
        assert (SCENARIOS_DIR / name).is_file(), f"Referenced scenario missing: {name}"


def test_demo_md_and_web_mandates_match_aimo_mandates(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "mandates.db"))
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)

    seeded = {m.customer: m for m in mandates.all()}
    assert set(seeded) == {"hotel-aurora", "hospital-tays", "school-hervanta"}

    demo_text = DEMO_MD.read_text(encoding="utf-8")
    for approver_name in ("Sari Nieminen", "Laura Virtanen", "Mikko Heikkinen"):
        assert approver_name in demo_text

    parsed_approvers = extract_demo_approver_clauses(demo_text)
    assert parsed_approvers == EXPECTED_DEMO_APPROVERS
    for customer, names in parsed_approvers.items():
        assert seeded[customer].approvers == names

    for customer, formatted_limit in EXPECTED_DEMO_LIMIT_STRINGS.items():
        mandate = seeded[customer]
        assert f"{int(mandate.per_order_eur):,}" == formatted_limit
        assert formatted_limit in demo_text
        assert f"{mandate.per_order_eur:,.2f} EUR" in demo_text

    aurora = seeded["hotel-aurora"]
    assert f"{aurora.per_week_eur:,.2f} EUR" in demo_text

    school_limit_match = re.search(
        r"`school-hervanta`\s+per-order limit is\s+`([\d,]+(?:\.\d+)?)\s*EUR`",
        demo_text,
    )
    assert school_limit_match is not None
    assert float(school_limit_match.group(1).replace(",", "")) == seeded["school-hervanta"].per_order_eur

    aurora_row_match = re.search(
        r"`hotel-aurora`\s+selected;\s+mandate limits\s+`([\d,]+(?:\.\d+)?)\s*EUR`\s+per order,\s+`([\d,]+(?:\.\d+)?)\s*EUR`\s+per week",
        demo_text,
    )
    assert aurora_row_match is not None
    assert float(aurora_row_match.group(1).replace(",", "")) == aurora.per_order_eur
    assert float(aurora_row_match.group(2).replace(",", "")) == aurora.per_week_eur

    tays_excl_match = re.search(r"`hospital-tays`\s+excludes\s+([^)]+)", demo_text)
    assert tays_excl_match is not None
    tays_allergens = re.findall(r"`([a-z_]+)`", tays_excl_match.group(1))
    assert tays_allergens == seeded["hospital-tays"].exclude_allergens

    console_mandates = extract_js_object(
        (WEB_DIR / "console.js").read_text(encoding="utf-8"), "MANDATES"
    )
    inbox_approvers = extract_js_object(
        (WEB_DIR / "inbox.js").read_text(encoding="utf-8"), "APPR"
    )
    admin_mocks = extract_js_object(
        (WEB_DIR / "admin.js").read_text(encoding="utf-8"), "MOCK"
    )
    shell_keys = extract_js_object(
        (WEB_DIR / "shell.js").read_text(encoding="utf-8"), "KEYS"
    )

    assert set(console_mandates) == set(seeded)
    assert set(inbox_approvers) == set(seeded)
    assert set(admin_mocks) == set(seeded)
    assert set(shell_keys) == set(seeded)

    for customer, m in seeded.items():
        assert console_mandates[customer]["o"] == m.per_order_eur
        assert console_mandates[customer]["w"] == m.per_week_eur
        assert inbox_approvers[customer] == m.approvers
        assert admin_mocks[customer]["per_order_eur"] == m.per_order_eur
        assert admin_mocks[customer]["per_week_eur"] == m.per_week_eur
        assert admin_mocks[customer]["approvers"] == m.approvers
        assert admin_mocks[customer]["exclude_allergens"] == m.exclude_allergens
        assert shell_keys[customer] == f"demo-{customer}"


def test_demo_md_ui_routes_and_labels_exist_in_web():
    demo_text = DEMO_MD.read_text(encoding="utf-8")

    for route, nav_label in EXPECTED_NAV_ITEMS:
        assert f"`{route}`" in demo_text
        assert f"`{nav_label}`" in demo_text
        assert (WEB_DIR / route.lstrip("/")).is_file()

    for page_name in ("index.html", "console.html", "inbox.html", "audit.html", "admin.html"):
        dom = parse_html_dom((WEB_DIR / page_name).read_text(encoding="utf-8"))
        assert dom.nav_links == EXPECTED_NAV_ITEMS, page_name

    console_bundle = (
        (WEB_DIR / "console.html").read_text(encoding="utf-8")
        + "\n"
        + (WEB_DIR / "console.js").read_text(encoding="utf-8")
    )
    for snippet in (
        "Request",
        "Run",
        "Agent trace",
        "Parsed need",
        "Offers",
        "Signed quote",
        "signed Ed25519",
        "Copy",
        "Auto-approved",
        "Needs approval",
        "Open in Approvals",
        "Audit trail",
        "Simulate stock-out",
        "Clear",
        "Simulated stock-out on ",
        "Cleared simulation",
        "parse_request",
        "get_offers",
        "request_quote",
        "place_order",
    ):
        assert snippet in demo_text, snippet
        assert snippet in console_bundle, snippet

    inbox_bundle = (
        (WEB_DIR / "inbox.html").read_text(encoding="utf-8")
        + "\n"
        + (WEB_DIR / "inbox.js").read_text(encoding="utf-8")
    )
    for snippet in (
        "Waiting for you",
        "Decided today",
        "Who is approving?",
        "Continue",
        "Reason for rejecting (required)",
        "Reject order",
    ):
        assert snippet in demo_text, snippet
        assert snippet in inbox_bundle, snippet

    audit_bundle = (
        (WEB_DIR / "audit.html").read_text(encoding="utf-8")
        + "\n"
        + (WEB_DIR / "audit.js").read_text(encoding="utf-8")
    )
    for snippet in ("Order receipt", "Ledger", "Plain text", "Print", "Export JSON"):
        assert snippet in demo_text, snippet
        assert snippet in audit_bundle, snippet


def test_parsers_detect_drift():
    sample_html = (
        '<nav><a href="/console.html">Console</a></nav>'
        '<div id="scenarios"><button type="button" class="chip" data-s="milk">Lactose-free milk</button></div>'
    )
    dom = parse_html_dom(sample_html)
    assert dom.chips == [("milk", "Lactose-free milk")]
    assert dom.nav_links == [("/console.html", "Console")]

    sample_js = 'const TEXTS={event:"Friday: 60 kg, butter: 25 kg",milk:\'1000 L\'};'
    assert extract_js_object(sample_js, "TEXTS") == {
        "event": "Friday: 60 kg, butter: 25 kg",
        "milk": "1000 L",
    }

    heading = HASH_CHAR * 3
    sample_scenes = f"{heading} Scene 1, demo (60 pts)\n{heading} Scene 2, ask (40 pts)\n"
    assert extract_scene_weights(sample_scenes) == [
        (1, 60, "(60 pts)"),
        (2, 40, "(40 pts)"),
    ]


def test_demo_dom_parity_hygiene():
    source = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in source
    assert HASH_CHAR not in source
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    assert not [tok for tok in tokens if tok.type == tokenize.COMMENT]
