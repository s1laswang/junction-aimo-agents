from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
from typing import get_args

from fastapi.testclient import TestClient
import pytest

from aimo import allocate, approvals, audit, catalog, keys, mandates, offers as offers_mod, parse, quotes, substitute, valio
from aimo.app import app
from aimo.models import (
    AuditEvent,
    Constraint,
    Decision,
    Mandate,
    Need,
    NeedItem,
    Offer,
    OfferGroup,
    OffersResponse,
    Order,
    OrderStatus,
    Product,
    QuoteLine,
    Substitution,
    UnitSize,
)

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

NODE_HARNESS = "\n".join([
    'const fs = require("node:fs");',
    'const path = require("node:path");',
    'const vm = require("node:vm");',
    'const webcrypto = require("node:crypto").webcrypto;',
    'const input = JSON.parse(fs.readFileSync(0, "utf8"));',
    'const webDir = input.webDir;',
    'const indexScripts = input.indexScripts;',
    'const EXPORT_TAIL = \';globalThis.__exported={...(typeof MOCK!=="undefined"?{MOCK}:{}),...(typeof MOCKS!=="undefined"?{MOCKS}:{}),...(typeof MANDATES!=="undefined"?{MANDATES}:{}),...(typeof CUST!=="undefined"?{CUST}:{}),...(typeof TEXTS!=="undefined"?{TEXTS}:{}),...(typeof APPR!=="undefined"?{APPR}:{}),...(typeof MM!=="undefined"?{MM}:{}),...(typeof M!=="undefined"?{M}:{}),...(typeof buildMock!=="undefined"?{buildMock}:{}),...(typeof verify!=="undefined"?{verify}:{}),...(typeof hashOf!=="undefined"?{hashOf}:{}),...(typeof MOCK_ORDER!=="undefined"?{MOCK_ORDER}:{}),...(typeof CUSTOMERS!=="undefined"?{CUSTOMERS}:{}),...(typeof KEYS!=="undefined"?{KEYS}:{})};\';',
    'function instrument(src) {',
    '  const trimmed = src.trimEnd();',
    '  const m = trimmed.match(/\\}(\\s*(?:\\)\\s*\\(\\s*\\)|\\(\\s*\\)\\s*\\))\\s*;?\\s*)$/);',
    '  if (m) {',
    '    return trimmed.slice(0, trimmed.length - m[0].length) + EXPORT_TAIL + "}" + m[1];',
    '  }',
    '  return trimmed + "\\n" + EXPORT_TAIL;',
    '}',
    'class MockElement {',
    '  constructor() {',
    '    this.value = "";',
    '    this.textContent = "";',
    '    this.innerHTML = "";',
    '    this.hidden = false;',
    '    this.disabled = false;',
    '    this.open = false;',
    '    this.returnValue = "";',
    '    this.className = "";',
    '    this.nodeType = 1;',
    '    this.dataset = {};',
    '    this.style = {};',
    '    this.classList = { add(){}, remove(){}, toggle(){ return false; }, contains(){ return false; } };',
    '  }',
    '  get parentNode() { return this._p || (this._p = new MockElement()); }',
    '  append() {}',
    '  prepend() {}',
    '  replaceChildren() {}',
    '  insertBefore() {}',
    '  insertAdjacentHTML() {}',
    '  appendChild() {}',
    '  remove() {}',
    '  focus() {}',
    '  click() {}',
    '  showModal() {}',
    '  scrollIntoView() {}',
    '  setAttribute() {}',
    '  getAttribute() { return ""; }',
    '  addEventListener() {}',
    '  closest() { return null; }',
    '  querySelector() { return new MockElement(); }',
    '  querySelectorAll() { return []; }',
    '}',
    'function createSandbox(search, customFetch) {',
    '  const store = new Map();',
    '  const byId = new Map();',
    '  const document = {',
    '    readyState: "complete",',
    '    title: "",',
    '    body: new MockElement(),',
    '    getElementById(id) { if (!byId.has(id)) byId.set(id, new MockElement()); return byId.get(id); },',
    '    querySelector() { return new MockElement(); },',
    '    querySelectorAll() { return []; },',
    '    createElement() { return new MockElement(); },',
    '    createElementNS() { return new MockElement(); },',
    '    createTextNode(t) { return { nodeType: 3, textContent: String(t) }; },',
    '    addEventListener() {},',
    '  };',
    '  const localStorage = {',
    '    getItem(k) { return store.has(k) ? store.get(k) : null; },',
    '    setItem(k, v) { store.set(k, String(v)); },',
    '    removeItem(k) { store.delete(k); },',
    '    clear() { store.clear(); },',
    '  };',
    '  const location = {',
    '    origin: "http://127.0.0.1:8000",',
    '    pathname: "/",',
    '    search: search || "",',
    '    hash: "",',
    '    href: "http://127.0.0.1:8000/" + (search || ""),',
    '  };',
    '  const ctx = {',
    '    document,',
    '    localStorage,',
    '    location,',
    '    history: { replaceState(){}, pushState(){} },',
    '    navigator: { clipboard: { writeText: async () => {} } },',
    '    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },',
    '    URL,',
    '    URLSearchParams,',
    '    Intl,',
    '    Blob,',
    '    TextEncoder,',
    '    Uint8Array,',
    '    crypto: webcrypto,',
    '    performance,',
    '    console,',
    '    fetch: customFetch || (async () => ({ ok: false, status: 503, json: async () => null })),',
    '    setTimeout: () => 0,',
    '    clearTimeout: () => {},',
    '    setInterval: () => 0,',
    '    clearInterval: () => {},',
    '    addEventListener() {},',
    '    dispatchEvent() {},',
    '    scrollTo() {},',
    '    print() {},',
    '    matchMedia: () => ({ matches: true, addEventListener(){} }),',
    '  };',
    '  ctx.window = ctx;',
    '  ctx.globalThis = ctx;',
    '  return vm.createContext(ctx);',
    '}',
    'function runFile(ctx, filename, exportVars) {',
    '  const raw = fs.readFileSync(path.join(webDir, filename), "utf8");',
    '  vm.runInContext(exportVars ? instrument(raw) : raw, ctx, { filename });',
    '  return ctx.__exported || {};',
    '}',
    '(async () => {',
    '  const shellCtx = createSandbox("?mock=1");',
    '  const shellExp = runFile(shellCtx, "shell.js", true);',
    '  const adminCtx = createSandbox("?mock=1");',
    '  runFile(adminCtx, "shell.js", false);',
    '  const adminExp = runFile(adminCtx, "admin.js", true);',
    '  const consoleCtx = createSandbox("?mock=1");',
    '  runFile(consoleCtx, "shell.js", false);',
    '  const consoleExp = runFile(consoleCtx, "console.js", true);',
    '  const inboxCtx = createSandbox("?mock=1");',
    '  runFile(inboxCtx, "shell.js", false);',
    '  const inboxExp = runFile(inboxCtx, "inbox.js", true);',
    '  const auditCtx = createSandbox("?mock=1");',
    '  runFile(auditCtx, "shell.js", false);',
    '  const auditExp = runFile(auditCtx, "audit.js", true);',
    '  const cleanEvents = await auditExp.buildMock(false);',
    '  const brokenEvents = await auditExp.buildMock(true);',
    '  const cleanVerify = await auditCtx.aimoAudit.verify(cleanEvents);',
    '  const brokenVerify = await auditCtx.aimoAudit.verify(brokenEvents);',
    '  const indexCtx = createSandbox("?mock=1");',
    '  runFile(indexCtx, "shell.js", false);',
    '  let indexExp = {};',
    '  for (const snippet of indexScripts) {',
    '    vm.runInContext(instrument(snippet), indexCtx, { filename: "index.html" });',
    '    indexExp = { ...indexExp, ...(indexCtx.__exported || {}) };',
    '  }',
    '  process.stdout.write(JSON.stringify({',
    '    node_version: process.version,',
    '    shell: { CUSTOMERS: shellExp.CUSTOMERS || shellCtx.aimo.CUSTOMERS, KEYS: shellExp.KEYS || null },',
    '    admin: { MOCK: adminExp.MOCK },',
    '    console: { MANDATES: consoleExp.MANDATES, CUST: consoleExp.CUST, MOCKS: consoleExp.MOCKS, TEXTS: consoleExp.TEXTS },',
    '    inbox: { MOCK: inboxExp.MOCK, APPR: inboxExp.APPR },',
    '    audit: { clean: cleanEvents, broken: brokenEvents, clean_verify: cleanVerify, broken_verify: brokenVerify, MOCK_ORDER: auditExp.MOCK_ORDER || null },',
    '    index: { MM: indexExp.MM, M: indexExp.M || null },',
    '  }));',
    '})().catch(err => { console.error(err); process.exit(1); });',
])

SHELL_ERROR_HARNESS = "\n".join([
    'const fs = require("node:fs");',
    'const path = require("node:path");',
    'const vm = require("node:vm");',
    'const input = JSON.parse(fs.readFileSync(0, "utf8"));',
    'const webDir = input.webDir;',
    'const cases = input.cases;',
    '(async () => {',
    '  const shellSrc = fs.readFileSync(path.join(webDir, "shell.js"), "utf8");',
    '  const out = [];',
    '  for (const item of cases) {',
    '    const el = { innerHTML: "", classList: { toggle(){} }, addEventListener(){} };',
    '    const ctx = {',
    '      document: { readyState: "complete", getElementById: () => null, querySelector: () => el, addEventListener(){} },',
    '      localStorage: { getItem: () => null, setItem(){} },',
    '      location: { search: "" },',
    '      CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },',
    '      URLSearchParams,',
    '      addEventListener() {},',
    '      dispatchEvent() {},',
    '      fetch: async () => ({ ok: false, status: item.status, json: async () => item.body }),',
    '    };',
    '    ctx.window = ctx;',
    '    vm.createContext(ctx);',
    '    vm.runInContext(shellSrc, ctx, { filename: "shell.js" });',
    '    const res = await ctx.aimo.api(item.path, { admin: !!item.admin });',
    '    const banner = ctx.aimo.offline(el);',
    '    out.push({ res, lastError: ctx.aimo.lastError, banner });',
    '  }',
    '  process.stdout.write(JSON.stringify(out));',
    '})().catch(err => { console.error(err); process.exit(1); });',
])


class InlineScriptExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline_scripts: list[str] = []
        self.external_local_scripts: list[str] = []
        self._in_inline = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attr_map = dict(attrs)
        src = (attr_map.get("src") or "").strip()
        if src:
            if src != "/shell.js" and src.startswith("/"):
                self.external_local_scripts.append(src.lstrip("/"))
        else:
            self._in_inline = True
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_inline:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_inline:
            self._in_inline = False
            self.inline_scripts.append("".join(self._buf))


def _node_bin() -> str:
    preferred = Path("/home/silas/tools/node/bin/node")
    if preferred.is_file():
        return str(preferred)
    found = shutil.which("node")
    if found:
        return found
    pytest.skip("node binary not found")


def _extract_index_scripts() -> list[str]:
    parser = InlineScriptExtractor()
    parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
    scripts = list(parser.inline_scripts)
    for rel in parser.external_local_scripts:
        candidate = WEB / rel
        if candidate.is_file():
            scripts.append(candidate.read_text(encoding="utf-8"))
    return scripts


@pytest.fixture(scope="module")
def ui_mocks() -> dict:
    node = _node_bin()
    proc = subprocess.run(
        [node, "-e", NODE_HARNESS],
        input=json.dumps({"webDir": str(WEB), "indexScripts": _extract_index_scripts()}),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    major = int(data["node_version"].lstrip("v").split(".")[0])
    assert major >= 20
    return data


def _synthetic_catalog() -> dict[str, dict]:
    return {
        "1001": {
            "product": Product(
                product_id="1001",
                name="Valio Luomu Kevytmaito laktoositon 1 L",
                brand="Valio",
                vendor="VALIO OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["milk"],
                labels=["organic", "free_from_lactose"],
                origin="FI",
                temperature=4,
            ),
            "net": 1.20,
            "gross": 1.37,
            "stock": 2000.0,
            "reliability": 0.99,
            "co2": 1.15,
        },
        "1002": {
            "product": Product(
                product_id="1002",
                name="Valio Kevytmaito laktoositon 1 L",
                brand="Valio",
                vendor="VALIO OY",
                category="10400",
                sales_unit="LTR",
                unit_size=UnitSize(value=1.0, unit="l"),
                allergens=["milk"],
                labels=["free_from_lactose"],
                origin="FI",
                temperature=4,
            ),
            "net": 1.25,
            "gross": 1.43,
            "stock": 2000.0,
            "reliability": 0.97,
            "co2": 1.20,
        },
        "2001": {
            "product": Product(
                product_id="2001",
                name="Valio Provenia Gluteeniton pasta 1 kg",
                brand="Valio",
                vendor="VALIO OY",
                category="15100",
                sales_unit="KGM",
                unit_size=UnitSize(value=1.0, unit="kg"),
                allergens=[],
                labels=["free_from_gluten", "free_from_lactose"],
                origin="FI",
                temperature=20,
            ),
            "net": 4.00,
            "gross": 4.56,
            "stock": 800.0,
            "reliability": 0.96,
            "co2": 1.10,
        },
    }


@pytest.fixture
def parity_client(tmp_path, monkeypatch):
    empty_data = tmp_path / "empty_data"
    empty_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "parity.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(empty_data))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    for env_var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "AIMO_AGENT_KEYS", "AIMO_ADMIN_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    monkeypatch.setattr(keys, "_cache", {})
    monkeypatch.setattr(offers_mod, "_overrides", {})
    monkeypatch.setattr(allocate, "_data", lambda: {})
    monkeypatch.setattr(parse, "_llm_parse", lambda *a, **kw: None)

    items = _synthetic_catalog()

    def fake_search(q: str = "", constraints: Constraint | None = None, limit: int = 50) -> list[Product]:
        c = constraints or Constraint()
        probe = NeedItem(query=q or "", quantity=1.0, unit="pcs", constraints=c)
        scored: list[tuple[float, str, Product]] = []
        for pid, entry in items.items():
            prod = entry["product"]
            if (c.lactose_free or c.gluten_free or c.exclude_allergens) and not substitute.allergen_safe(
                prod, c, c.exclude_allergens
            ):
                continue
            score = 1.0 if not (q or "").strip() else substitute.match_score(probe, prod)
            if score > 0:
                scored.append((-score, pid, prod))
        scored.sort(key=lambda r: (r[0], r[1]))
        return [r[2] for r in scored[:limit]]

    def fake_get(pid: str) -> Product | None:
        entry = items.get(str(pid))
        return entry["product"] if entry else None

    def fake_price(customer: str, day: date, ids) -> dict[str, dict]:
        out = {}
        for raw_id in ids:
            pid = str(raw_id)
            entry = items.get(pid)
            if entry:
                out[pid] = {
                    "productId": pid,
                    "currency": "EUR",
                    "catalogPrice": {"value": entry["net"]},
                    "priceWithTax": {"value": entry["gross"]},
                }
        return out

    def fake_availability(customer: str, day: date, ids) -> dict[str, dict]:
        out = {}
        for raw_id in ids:
            pid = str(raw_id)
            entry = items.get(pid)
            if entry:
                out[pid] = {
                    "productId": pid,
                    "closingTime": "2026-11-12T23:59:59+02:00",
                    "salesBlock": False,
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableAmount": entry["stock"],
                            "availableDate": day.isoformat(),
                            "salesUnit": entry["product"].sales_unit,
                        }
                    ],
                }
        return out

    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "get", fake_get)
    monkeypatch.setattr(catalog, "reliability", lambda pid: items.get(str(pid), {}).get("reliability"))
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: items.get(str(pid), {}).get("co2"))
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})
    monkeypatch.setattr(valio, "price", fake_price)
    monkeypatch.setattr(valio, "availability", fake_availability)

    with TestClient(app) as client:
        yield client


def _agent_headers(slug: str) -> dict[str, str]:
    return {"X-Agent-Key": f"demo-{slug}"}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": "demo-admin"}


def _assert_error_envelope(resp, expected_status: int, expected_code: str | None = None) -> dict:
    assert resp.status_code == expected_status, (resp.status_code, resp.text)
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert isinstance(body, dict)
    assert set(body.keys()) == {"code", "text"}
    assert isinstance(body["code"], str) and body["code"].strip()
    assert isinstance(body["text"], str) and body["text"].strip()
    if expected_code is not None:
        assert body["code"] == expected_code
    return body


def _backend_mandates() -> dict[str, Mandate]:
    return {m.customer: m for m in mandates._MANDATES}


def test_backend_mandates_baseline_contract():
    by_cust = _backend_mandates()
    assert set(by_cust.keys()) == {"hotel-aurora", "hospital-tays", "school-hervanta"}

    aurora = by_cust["hotel-aurora"]
    assert aurora.per_order_eur == 2500.0
    assert aurora.per_week_eur == 15000.0
    assert aurora.approvers == ["Laura Virtanen", "Mikko Heikkinen"]

    tays = by_cust["hospital-tays"]
    assert tays.per_order_eur == 1500.0
    assert tays.per_week_eur == 8000.0
    assert tays.approvers == ["Helena Korhonen", "Antti Lahtinen"]

    hervanta = by_cust["school-hervanta"]
    assert hervanta.per_order_eur == 600.0
    assert hervanta.per_week_eur == 3000.0
    assert hervanta.approvers == ["Sari Nieminen"]


def test_mandate_parity_admin_js_mock_validates_and_matches_backend(ui_mocks: dict):
    by_cust = _backend_mandates()
    admin_mock = ui_mocks["admin"]["MOCK"]
    assert isinstance(admin_mock, dict)
    assert set(admin_mock.keys()) == set(by_cust.keys())

    for customer, raw_entry in admin_mock.items():
        assert set(raw_entry.keys()) == set(Mandate.model_fields.keys())
        parsed = Mandate.model_validate(raw_entry)
        assert parsed == by_cust[customer]


def test_mandate_parity_across_shell_console_inbox_and_index(ui_mocks: dict):
    by_cust = _backend_mandates()
    expected_customers = set(by_cust.keys())

    shell_customers = ui_mocks["shell"]["CUSTOMERS"]
    assert set(shell_customers) == expected_customers
    shell_keys = ui_mocks["shell"]["KEYS"]
    if shell_keys is not None:
        expected_demo_keys = {cust_agent[0]: key for key, cust_agent in mandates._DEMO_KEYS.items()}
        assert shell_keys == expected_demo_keys

    console_mandates = ui_mocks["console"]["MANDATES"]
    assert isinstance(console_mandates, dict)
    assert set(console_mandates.keys()) == expected_customers
    for customer, raw_limits in console_mandates.items():
        per_order = raw_limits["per_order_eur"] if "per_order_eur" in raw_limits else raw_limits["o"]
        per_week = raw_limits["per_week_eur"] if "per_week_eur" in raw_limits else raw_limits["w"]
        assert float(per_order) == by_cust[customer].per_order_eur
        assert float(per_week) == by_cust[customer].per_week_eur

    console_cust = ui_mocks["console"]["CUST"]
    assert isinstance(console_cust, dict)
    assert {"hospital", "milk", "lasagne", "school", "event"}.issubset(console_cust.keys())
    assert set(console_cust.values()).issubset(expected_customers)
    assert console_cust["hospital"] == "hospital-tays"
    assert console_cust["milk"] == "hotel-aurora"
    assert console_cust["school"] == "school-hervanta"

    inbox_appr = ui_mocks["inbox"]["APPR"]
    assert isinstance(inbox_appr, dict)
    assert set(inbox_appr.keys()) == expected_customers
    for customer, approvers_list in inbox_appr.items():
        assert approvers_list == by_cust[customer].approvers

    index_mm = ui_mocks["index"]["MM"]
    assert isinstance(index_mm, dict)
    assert set(index_mm.keys()) == expected_customers
    for customer, val in index_mm.items():
        per_order = val["per_order_eur"] if isinstance(val, dict) else val
        assert float(per_order) == by_cust[customer].per_order_eur

    index_m = ui_mocks["index"]["M"]
    if isinstance(index_m, dict):
        for appr_item in index_m.get("appr", []):
            assert appr_item["customer"] in expected_customers
        for audit_item in index_m.get("audit", []):
            assert audit_item["customer"] in expected_customers


def test_inbox_js_mock_orders_validate_with_pydantic_order_model(ui_mocks: dict):
    by_cust = _backend_mandates()
    raw_orders = ui_mocks["inbox"]["MOCK"]
    assert isinstance(raw_orders, list) and len(raw_orders) >= 3

    seen_customers = set()
    for raw_order in raw_orders:
        assert set(raw_order.keys()).issubset(set(Order.model_fields.keys()))
        order = Order.model_validate(raw_order)
        assert order.customer in by_cust
        seen_customers.add(order.customer)
        assert order.status == "pending_approval"
        assert order.decision.outcome == "needs_approval"
        assert len(order.decision.reasons) >= 1
        assert len(order.lines) >= 1
        assert round(sum(l.line_total_net for l in order.lines), 2) == round(order.total_net, 2)
        assert order.delivery_date is not None
        assert order.valid_until is not None
        assert order.valid_until > order.created_at

    assert seen_customers == set(by_cust.keys())


def test_console_js_mock_scenarios_validate_against_need_and_offer_models(ui_mocks: dict):
    mocks = ui_mocks["console"]["MOCKS"]
    cust_map = ui_mocks["console"]["CUST"]
    assert isinstance(mocks, dict)
    assert {"milk", "lasagne", "school", "event"}.issubset(mocks.keys())
    valid_statuses = set(get_args(OrderStatus))

    for scenario_name, scenario in mocks.items():
        customer = cust_map.get(scenario_name, "hotel-aurora")
        raw_need = scenario["need"]
        assert set(raw_need.keys()).issubset(set(Need.model_fields.keys()))
        assert len(raw_need["items"]) >= 1

        validated_items: list[NeedItem] = []
        for raw_item in raw_need["items"]:
            assert set(raw_item.keys()).issubset(set(NeedItem.model_fields.keys()))
            raw_constraints = raw_item.get("constraints", {})
            assert set(raw_constraints.keys()).issubset(set(Constraint.model_fields.keys()))
            Constraint.model_validate(raw_constraints)
            validated_items.append(NeedItem.model_validate(raw_item))

        need = Need.model_validate({**raw_need, "customer": customer})
        assert len(need.items) == len(validated_items)

        validated_groups: list[OfferGroup] = []
        assert len(scenario["offers"]) >= 1
        for raw_group in scenario["offers"]:
            ni_ref = raw_group["need_item"]
            need_item_obj = validated_items[ni_ref] if isinstance(ni_ref, int) else NeedItem.model_validate(ni_ref)
            group_offers: list[Offer] = []
            assert len(raw_group["offers"]) >= 1
            for raw_offer in raw_group["offers"]:
                assert set(raw_offer.keys()).issubset(set(Offer.model_fields.keys()))
                raw_prod = raw_offer["product"]
                assert set(raw_prod.keys()).issubset(set(Product.model_fields.keys()))
                if raw_prod.get("unit_size") is not None:
                    UnitSize.model_validate(raw_prod["unit_size"])
                if "product_id" in raw_prod:
                    assert raw_prod["product_id"] == raw_offer["product_id"]
                prod = Product.model_validate({"product_id": raw_offer["product_id"], **raw_prod})
                offer = Offer.model_validate(
                    {
                        "total_gross": round(raw_offer["total_net"] * 1.14, 2),
                        **raw_offer,
                        "product": prod.model_dump(),
                    }
                )
                assert round(offer.units_needed * offer.unit_price_net, 2) == round(offer.total_net, 2)
                group_offers.append(offer)

            validated_groups.append(
                OfferGroup.model_validate(
                    {
                        "need_item": need_item_obj.model_dump(),
                        "offers": [o.model_dump() for o in group_offers],
                        "shortfall": raw_group["shortfall"],
                    }
                )
            )

        resp_model = OffersResponse(items=validated_groups)
        assert len(resp_model.items) == len(scenario["offers"])

        raw_order = scenario["order"]
        assert raw_order["status"] in valid_statuses
        Decision.model_validate(raw_order["decision"])
        for sub in raw_order.get("substitutions", []):
            Substitution.model_validate(sub)


def test_audit_js_build_mock_validates_and_matches_python_digest(ui_mocks: dict):
    by_cust = _backend_mandates()
    clean_events = ui_mocks["audit"]["clean"]
    broken_events = ui_mocks["audit"]["broken"]
    assert len(clean_events) >= 5
    assert len(broken_events) == len(clean_events)
    assert ui_mocks["audit"]["clean_verify"] is None

    prev = audit.GENESIS
    for idx, raw_ev in enumerate(clean_events, start=1):
        assert set(raw_ev.keys()) == set(AuditEvent.model_fields.keys())
        ev = AuditEvent.model_validate(raw_ev)
        assert ev.seq == idx
        assert ev.customer in by_cust
        assert raw_ev["prev_hash"] == prev
        assert audit._digest(raw_ev["prev_hash"], raw_ev) == raw_ev["hash"]
        prev = raw_ev["hash"]

    broken_verify = ui_mocks["audit"]["broken_verify"]
    assert isinstance(broken_verify, dict)
    assert broken_verify["at"] == 5

    first_bad_seq = None
    prev_broken = audit.GENESIS
    for idx, raw_ev in enumerate(broken_events, start=1):
        AuditEvent.model_validate(raw_ev)
        if raw_ev["prev_hash"] != prev_broken or audit._digest(raw_ev["prev_hash"], raw_ev) != raw_ev["hash"]:
            first_bad_seq = idx
            break
        prev_broken = raw_ev["hash"]
    assert first_bad_seq == 5

    mock_order = ui_mocks["audit"]["MOCK_ORDER"]
    if mock_order is not None:
        assert mock_order["customer"] in by_cust
        assert mock_order["status"] in set(get_args(OrderStatus))
        Decision.model_validate(mock_order["decision"])
        for sub in mock_order.get("substitutions", []):
            Substitution.model_validate(sub)


def test_error_envelope_401_missing_and_invalid_keys(parity_client: TestClient):
    delivery = (date.today() + timedelta(days=3)).isoformat()
    need_body = {
        "customer": "hotel-aurora",
        "deliver_by": delivery,
        "items": [{"query": "milk", "quantity": 100, "unit": "l", "constraints": {"lactose_free": True}}],
    }
    quote_body = {"offers": [{"product_id": "1001", "quantity": 10}], "delivery_date": delivery}
    order_body = {"quote_id": "q_missing", "jws": "bad.jws.token"}
    mandate_body = mandates._MANDATES[0].model_dump(mode="json")

    for method, path, payload in [
        ("POST", "/v1/offers", need_body),
        ("POST", "/v1/quotes", quote_body),
        ("POST", "/v1/orders", order_body),
        ("GET", "/v1/mandate", None),
        ("GET", "/v1/approvals", None),
        ("GET", "/v1/audit", None),
        ("GET", "/v1/orders", None),
        ("GET", "/v1/orders/ord_missing", None),
        ("PUT", "/v1/mandate", mandate_body),
        ("POST", "/v1/approvals/ord_missing", {"approve": True, "by": "Laura Virtanen"}),
        ("POST", "/v1/admin/simulate", {"product_id": "1001", "available_amount": 0}),
        ("DELETE", "/v1/admin/simulate", None),
    ]:
        resp = parity_client.request(method, path, json=payload)
        _assert_error_envelope(resp, 401, "unauthorized")

    bad_agent = parity_client.post("/v1/offers", headers={"X-Agent-Key": "invalid-agent"}, json=need_body)
    _assert_error_envelope(bad_agent, 401, "unauthorized")

    bad_admin = parity_client.put("/v1/mandate", headers={"X-Admin-Key": "invalid-admin"}, json=mandate_body)
    _assert_error_envelope(bad_admin, 401, "unauthorized")


def test_error_envelope_403_admin_routes_cross_customer_and_not_approver(parity_client: TestClient):
    aurora_headers = _agent_headers("hotel-aurora")
    tays_headers = _agent_headers("hospital-tays")
    school_headers = _agent_headers("school-hervanta")
    admin_headers = _admin_headers()
    delivery = (date.today() + timedelta(days=3)).isoformat()

    mandate_body = mandates._MANDATES[0].model_dump(mode="json")
    for method, path, payload in [
        ("PUT", "/v1/mandate", mandate_body),
        ("POST", "/v1/approvals/ord_any", {"approve": True, "by": "Laura Virtanen"}),
        ("POST", "/v1/admin/simulate", {"product_id": "1001", "available_amount": 0}),
        ("DELETE", "/v1/admin/simulate", None),
    ]:
        resp = parity_client.request(method, path, headers=aurora_headers, json=payload)
        _assert_error_envelope(resp, 403, "forbidden")

    cross_offers = parity_client.post(
        "/v1/offers",
        headers=aurora_headers,
        json={
            "customer": "hospital-tays",
            "deliver_by": delivery,
            "items": [{"query": "milk", "quantity": 50, "unit": "l"}],
        },
    )
    _assert_error_envelope(cross_offers, 403, "forbidden")

    for path in (
        "/v1/mandate?customer=hospital-tays",
        "/v1/approvals?customer=hospital-tays",
        "/v1/orders?customer=hospital-tays",
        "/v1/audit?customer=hospital-tays",
    ):
        resp = parity_client.get(path, headers=aurora_headers)
        _assert_error_envelope(resp, 403, "forbidden")

    tays_quote = parity_client.post(
        "/v1/quotes",
        headers=tays_headers,
        json={"offers": [{"product_id": "1001", "quantity": 20}], "delivery_date": delivery},
    ).json()
    tays_order = parity_client.post(
        "/v1/orders",
        headers=tays_headers,
        json={"quote_id": tays_quote["quote_id"], "jws": tays_quote["jws"]},
    ).json()
    cross_order = parity_client.get(f"/v1/orders/{tays_order['order_id']}", headers=aurora_headers)
    _assert_error_envelope(cross_order, 403, "forbidden")

    school_quote = parity_client.post(
        "/v1/quotes",
        headers=school_headers,
        json={"offers": [{"product_id": "2001", "quantity": 200}], "delivery_date": delivery},
    ).json()
    school_order = parity_client.post(
        "/v1/orders",
        headers=school_headers,
        json={"quote_id": school_quote["quote_id"], "jws": school_quote["jws"]},
    ).json()
    assert school_order["status"] == "pending_approval"

    wrong_approver = parity_client.post(
        f"/v1/approvals/{school_order['order_id']}",
        headers=admin_headers,
        json={"approve": True, "by": "Laura Virtanen", "note": "unauthorized approver"},
    )
    _assert_error_envelope(wrong_approver, 403, "NOT_APPROVER")


def test_error_envelope_404_unknown_product_order_and_mandate(parity_client: TestClient):
    admin_headers = _admin_headers()

    missing_prod = parity_client.get("/v1/products/NO_SUCH_PRODUCT_404")
    _assert_error_envelope(missing_prod, 404, "not_found")

    missing_order = parity_client.get("/v1/orders/ord_missing_404", headers=admin_headers)
    _assert_error_envelope(missing_order, 404, "not_found")

    missing_approval = parity_client.post(
        "/v1/approvals/ord_missing_404",
        headers=admin_headers,
        json={"approve": True, "by": "Laura Virtanen"},
    )
    _assert_error_envelope(missing_approval, 404, "NOT_FOUND")

    missing_mandate = parity_client.get("/v1/mandate?customer=unknown-org", headers=admin_headers)
    _assert_error_envelope(missing_mandate, 404, "not_found")


def test_error_envelope_409_quote_used_quote_expired_and_invalid_mandate(
    parity_client: TestClient, monkeypatch
):
    aurora_headers = _agent_headers("hotel-aurora")
    admin_headers = _admin_headers()
    delivery_day = date.today() + timedelta(days=3)
    delivery = delivery_day.isoformat()

    q1 = parity_client.post(
        "/v1/quotes",
        headers=aurora_headers,
        json={"offers": [{"product_id": "1001", "quantity": 50}], "delivery_date": delivery},
    ).json()
    first_place = parity_client.post(
        "/v1/orders",
        headers=aurora_headers,
        json={"quote_id": q1["quote_id"], "jws": q1["jws"]},
    )
    assert first_place.status_code == 200

    replay = parity_client.post(
        "/v1/orders",
        headers=aurora_headers,
        json={"quote_id": q1["quote_id"], "jws": q1["jws"]},
    )
    _assert_error_envelope(replay, 409, "QUOTE_USED")

    unknown_quote = parity_client.post(
        "/v1/orders",
        headers=aurora_headers,
        json={"quote_id": "q_nonexistent_409", "jws": q1["jws"]},
    )
    _assert_error_envelope(unknown_quote, 409, "UNKNOWN_QUOTE")

    q_tampered = parity_client.post(
        "/v1/quotes",
        headers=aurora_headers,
        json={"offers": [{"product_id": "1001", "quantity": 15}], "delivery_date": delivery},
    ).json()
    bad_sig = parity_client.post(
        "/v1/orders",
        headers=aurora_headers,
        json={"quote_id": q_tampered["quote_id"], "jws": q_tampered["jws"][:-4] + "AAAA"},
    )
    _assert_error_envelope(bad_sig, 409, "BAD_SIGNATURE")

    not_pending = parity_client.post(
        f"/v1/approvals/{first_place.json()['order_id']}",
        headers=admin_headers,
        json={"approve": True, "by": "Laura Virtanen"},
    )
    _assert_error_envelope(not_pending, 409, "NOT_PENDING")

    expired_quote = quotes.issue(
        customer="hotel-aurora",
        agent_id="buyer-aurora",
        lines=[
            QuoteLine(
                product_id="1001",
                name="Valio Luomu Kevytmaito laktoositon 1 L",
                quantity=10.0,
                sales_unit="LTR",
                unit_price_net=1.20,
                line_total_net=12.0,
            )
        ],
        delivery_date=delivery_day,
        valid_until=datetime.now(timezone.utc) - timedelta(minutes=2),
        total_gross=13.70,
    )
    expired_place = parity_client.post(
        "/v1/orders",
        headers=aurora_headers,
        json={"quote_id": expired_quote.quote_id, "jws": expired_quote.jws},
    )
    _assert_error_envelope(expired_place, 409, "QUOTE_EXPIRED")

    q_pending = parity_client.post(
        "/v1/quotes",
        headers=aurora_headers,
        json={"offers": [{"product_id": "1001", "quantity": 25}], "delivery_date": delivery},
    ).json()
    pending_ord = parity_client.post(
        "/v1/orders",
        headers=aurora_headers,
        json={"quote_id": q_pending["quote_id"], "jws": q_pending["jws"], "ask_human": True},
    ).json()
    assert pending_ord["status"] == "pending_approval"

    far_future = datetime.fromisoformat(q_pending["valid_until"]) + timedelta(hours=1)
    if far_future.tzinfo is None:
        far_future = far_future.replace(tzinfo=timezone.utc)
    monkeypatch.setattr(approvals, "_now", lambda: far_future)
    expired_decide = parity_client.post(
        f"/v1/approvals/{pending_ord['order_id']}",
        headers=admin_headers,
        json={"approve": True, "by": "Laura Virtanen", "note": "late approval"},
    )
    _assert_error_envelope(expired_decide, 409, "QUOTE_EXPIRED")

    base_mandate = parity_client.get("/v1/mandate?customer=hotel-aurora", headers=admin_headers).json()
    zero_limit = parity_client.put(
        "/v1/mandate",
        headers=admin_headers,
        json={**base_mandate, "per_order_eur": 0.0},
    )
    _assert_error_envelope(zero_limit, 409, "INVALID_MANDATE")

    inverted_limit = parity_client.put(
        "/v1/mandate",
        headers=admin_headers,
        json={**base_mandate, "per_order_eur": 20000.0, "per_week_eur": 15000.0},
    )
    _assert_error_envelope(inverted_limit, 409, "INVALID_MANDATE")


def test_error_envelope_503_upstream_unavailable_and_shell_js_parity(
    parity_client: TestClient, monkeypatch
):
    aurora_headers = _agent_headers("hotel-aurora")
    admin_headers = _admin_headers()
    delivery = (date.today() + timedelta(days=3)).isoformat()

    q_ok = parity_client.post(
        "/v1/quotes",
        headers=aurora_headers,
        json={"offers": [{"product_id": "1001", "quantity": 40}], "delivery_date": delivery},
    ).json()
    ord_ok = parity_client.post(
        "/v1/orders",
        headers=aurora_headers,
        json={"quote_id": q_ok["quote_id"], "jws": q_ok["jws"]},
    ).json()
    assert ord_ok["status"] == "confirmed"

    def raise_upstream(*_args, **_kwargs):
        raise valio.UpstreamUnavailable("simulated Valio timeout")

    monkeypatch.setattr(valio, "price", raise_upstream)
    monkeypatch.setattr(valio, "availability", raise_upstream)

    offers_503 = parity_client.post(
        "/v1/offers",
        headers=aurora_headers,
        json={
            "customer": "hotel-aurora",
            "deliver_by": delivery,
            "items": [{"query": "milk", "quantity": 100, "unit": "l", "constraints": {"lactose_free": True}}],
        },
    )
    env_503_offers = _assert_error_envelope(offers_503, 503, "UPSTREAM_UNAVAILABLE")

    quotes_503 = parity_client.post(
        "/v1/quotes",
        headers=aurora_headers,
        json={"offers": [{"product_id": "1001", "quantity": 40}], "delivery_date": delivery},
    )
    _assert_error_envelope(quotes_503, 503, "UPSTREAM_UNAVAILABLE")

    sim_503 = parity_client.post(
        "/v1/admin/simulate",
        headers=admin_headers,
        json={"product_id": "1001", "available_amount": 0},
    )
    _assert_error_envelope(sim_503, 503, "UPSTREAM_UNAVAILABLE")

    env_401 = parity_client.get("/v1/mandate").json()
    env_403 = parity_client.get("/v1/mandate?customer=hospital-tays", headers=aurora_headers).json()
    env_404 = parity_client.get("/v1/products/NO_SUCH_PRODUCT_404").json()
    env_409 = parity_client.put(
        "/v1/mandate",
        headers=admin_headers,
        json={**mandates._MANDATES[0].model_dump(mode="json"), "per_order_eur": 0.0},
    ).json()

    cases = [
        {"path": "/v1/mandate", "status": 401, "body": env_401, "admin": False},
        {"path": "/v1/mandate?customer=hospital-tays", "status": 403, "body": env_403, "admin": False},
        {"path": "/v1/products/NO_SUCH_PRODUCT_404", "status": 404, "body": env_404, "admin": False},
        {"path": "/v1/mandate", "status": 409, "body": env_409, "admin": True},
        {"path": "/v1/offers", "status": 503, "body": env_503_offers, "admin": False},
    ]
    node = _node_bin()
    proc = subprocess.run(
        [node, "-e", SHELL_ERROR_HARNESS],
        input=json.dumps({"webDir": str(WEB), "cases": cases}),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    assert len(results) == len(cases)
    for expected, actual in zip(cases, results):
        assert actual["res"] is None
        assert actual["lastError"]["status"] == expected["status"]
        assert actual["lastError"]["detail"] == expected["body"]
        assert actual["banner"] == f"Unavailable ({expected['status']})"
