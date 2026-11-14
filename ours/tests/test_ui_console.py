from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import io
import json
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from aimo import allocate, catalog, keys, offers as offers_mod, parse, policy, quotes, valio
from aimo.app import app
from aimo.models import (
    Constraint,
    Mandate,
    Need,
    NeedItem,
    Offer,
    OfferGroup,
    OffersResponse,
    Order,
    Product,
    QuoteLine,
    Substitution,
    UnitSize,
)

W = Path(__file__).resolve().parent.parent / "web"
THIS_FILE = Path(__file__).resolve()
FILES = ["console.html", "console.js", "console.css"]
IDS = ["text", "run", "scenarios", "result", "trace", "customer", "live", "trace-empty", "req", "ask-human"]
NODE = "/home/silas/tools/node/bin/node"
if not Path(NODE).is_file():
    NODE = shutil.which("node") or NODE

JS_BOOT = r"""
const fs = require("fs");
function mkEl(tag) {
  return {
    tagName: tag,
    className: "",
    textContent: "",
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    open: true,
    innerHTML: "",
    children: [],
    classList: { add() {}, remove() {}, toggle() {} },
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; },
    append(c) { this.children.push(c); },
    prepend(c) { this.children.unshift(c); },
    querySelector(sel) {
      if (!this._q) this._q = {};
      if (!this._q[sel]) this._q[sel] = mkEl("sub");
      return this._q[sel];
    }
  };
}
const els = {};
const getEl = (sel) => {
  if (!els[sel]) els[sel] = mkEl(sel);
  return els[sel];
};
global.location = { search: "?mock=1" };
global.navigator = { clipboard: { writeText() {} } };
global.localStorage = { getItem() { return null; }, setItem() {} };
global.performance = { now: () => 10 };
global.setTimeout = (fn, ms) => { if ((ms || 0) < 1000) Promise.resolve().then(fn); return 0; };
global.window = { addEventListener() {} };
global.document = {
  querySelector(sel) {
    if (sel === "#out") return getEl("#result");
    return getEl(sel);
  },
  querySelectorAll() { return []; },
  getElementById(id) { return getEl("#" + id); },
  createElement(tag) { return mkEl(tag); }
};
let curCust = "hotel-aurora";
global.aimo = {
  customer: () => curCust,
  setCustomer: (c) => { curCust = c; },
  mock: () => {},
  api: async () => null
};
eval(fs.readFileSync(process.argv[1], "utf8"));
const C = global.window.aimoConsole;
"""


@lru_cache(maxsize=1)
def _eval_console_js():
    harness = JS_BOOT + r"""
(async () => {
  const sampleNeed = {
    customer: "hotel-aurora",
    deliver_by: "2026-11-16",
    items: [{ query: "lactose-free milk", quantity: 1000, unit: "l", constraints: { lactose_free: true } }]
  };
  const offersWithAlts = C.offersHtml(sampleNeed, {
    items: [{
      need_item: sampleNeed.items[0],
      offers: C.MOCKS.milk.offers[0].offers,
      alternatives: C.MOCKS.milk.offers[0].alternatives,
      shortfall: 0,
      options: []
    }]
  });
  const offersWithoutAlts = C.offersHtml(sampleNeed, {
    items: [{
      need_item: sampleNeed.items[0],
      offers: C.MOCKS.milk.offers[0].offers,
      alternatives: [],
      shortfall: 0,
      options: []
    }]
  });
  const schoolItem = C.MOCKS.school.need.items[1];
  const offersShortfall = C.offersHtml(
    { customer: "school-hervanta", deliver_by: "2026-11-20", items: [schoolItem] },
    {
      items: [{
        need_item: schoolItem,
        offers: C.MOCKS.school.offers[1].offers,
        alternatives: C.MOCKS.school.offers[1].alternatives,
        shortfall: C.MOCKS.school.offers[1].shortfall,
        options: C.MOCKS.school.offers[1].options
      }]
    }
  );
  const quote = C.quoteHtml({
    quote_id: "q_test_123",
    total_net: 1180,
    total_gross: 1345.2,
    valid_until: "2026-11-16T10:00:00Z",
    jws: "eyJhbGciOiJFZERTQSJ9.testpayload1234567890.testsig1234567890",
    lines: [{ product_id: "mock-400101", name: "Valio Eila lactose-free milk 1 l", quantity: 1000, sales_unit: "PK", unit_price_net: 1.18, line_total_net: 1180 }]
  });
  const orderPending = C.orderHtml({
    order_id: "ord_2b8c10",
    status: "pending_approval",
    total_net: 909.6,
    decision: {
      outcome: "needs_approval",
      reasons: [
        { code: "OVER_ORDER_LIMIT", text: "Order total is over per-order limit" },
        { code: "SUBSTITUTION_NEEDS_APPROVAL", text: "Substitution requires approval" }
      ]
    },
    substitutions: [{ original_id: "P-1", substitute_id: "P-2", reason: "Out of stock" }]
  });
  const orderAuto = C.orderHtml({
    order_id: "ord_auto_1",
    status: "placed",
    total_net: 1180,
    decision: { outcome: "auto", reasons: [{ code: "WITHIN_MANDATE", text: "Within mandate" }] },
    substitutions: []
  });
  const filledOver = C.fillOrder(
    { status: "placed", decision: { outcome: "auto", reasons: [{ code: "WITHIN_MANDATE", text: "@WM" }] } },
    { total_net: 3100 },
    "hotel-aurora"
  );

  getEl("#text").value = "I need 1,000 litres of lactose-free milk by Monday.";
  getEl("#result").innerHTML = "";
  await C.run();
  const milkRunHtml = getEl("#result").innerHTML;

  curCust = "school-hervanta";
  getEl("#text").value = "Weekly school lunch: 200 kg of gluten-free pasta and 80 litres of oat drink";
  getEl("#result").innerHTML = "";
  await C.run();
  const schoolRunHtml = getEl("#result").innerHTML;

  console.log(JSON.stringify({
    offersWithAlts,
    offersWithoutAlts,
    offersShortfall,
    quote,
    orderPending,
    orderAuto,
    filledOver,
    milkRunHtml,
    schoolRunHtml
  }));
})();
"""
    proc = subprocess.run(
        [NODE, "-e", harness, str(W / "console.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_files_exist():
    for f in FILES:
        assert (W / f).stat().st_size > 0


def test_no_emoji():
    for f in FILES:
        assert not re.search("[\U0001F000-\U0001FAFF☀-➿]", (W / f).read_text(encoding="utf-8")), f


def test_no_em_dash_or_gradient():
    for f in FILES:
        t = (W / f).read_text(encoding="utf-8")
        assert "\u2014" not in t, f
        assert "gradient" not in t.lower(), f


def test_no_comments():
    html = (W / "console.html").read_text(encoding="utf-8")
    js = (W / "console.js").read_text(encoding="utf-8")
    css = (W / "console.css").read_text(encoding="utf-8")
    assert "<!--" not in html and "-->" not in html
    assert "/*" not in js and "*/" not in js and "//" not in js
    assert "/*" not in css and "*/" not in css
    py_tokens = list(tokenize.generate_tokens(io.StringIO(THIS_FILE.read_text(encoding="utf-8")).readline))
    assert not [t for t in py_tokens if t.type == tokenize.COMMENT]


def test_ids_present():
    html = (W / "console.html").read_text(encoding="utf-8")
    for i in IDS:
        assert f'id="{i}"' in html, i


def test_shared_shell_and_nav():
    html = (W / "console.html").read_text(encoding="utf-8")
    assert "/style.css" in html
    assert "/console.css" in html
    assert "/shell.js" in html
    assert '<script src="/console.js"></script>' in html
    assert 'src="console.js"' not in html
    assert 'rel="icon"' in html and "data:image/svg+xml" in html
    assert 'aria-current="page">Console</a>' in html
    assert "fonts.googleapis.com" not in html
    for href in re.findall(r'<link[^>]+href=["\']([^"\']+)["\']', html, re.I):
        assert href.startswith(("/", "data:")), href
    for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
        assert src.startswith("/") and not src.startswith("//"), src


def test_node_syntax_check():
    res = subprocess.run(
        [NODE, "--check", str(W / "console.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0


def test_trace_tools():
    js = (W / "console.js").read_text(encoding="utf-8")
    for t in ["parse_request", "get_offers", "request_quote", "place_order", "/v1/audit"]:
        assert t in js, t


def test_mock_and_demo_wiring():
    js = (W / "console.js").read_text(encoding="utf-8")
    assert "URLSearchParams(location.search)" in js
    assert '.has("mock")' in js
    assert '.has("demo")' in js
    assert "/v1/admin/simulate" in js
    assert "Ed25519" in js and "EdDSA" in js
    assert 'id="copy"' in js and "clipboard.writeText" in js
    assert "/audit.html?order_id=" in js
    assert "/inbox.html#" in js
    assert "python -m aimo.quotes verify" in js
    assert "/.well-known/jwks.json" in js
    assert "total_gross" in js
    assert "OVER_ORDER_LIMIT" in js and "SUBSTITUTION_NEEDS_APPROVAL" in js
    assert "ORDER_OVER_LIMIT" not in js


def test_render_alternatives_and_shortfall_options():
    out = _eval_console_js()
    alts_html = out["offersWithAlts"]
    assert "Alternatives (3)" in alts_html
    assert "Arla Lempi lactose-free milk 1 l" in alts_html
    assert "Arla" in alts_html
    assert "1.12" in alts_html
    assert "93%" in alts_html
    assert "1.15" in alts_html
    assert '<span class="tag">good</span>' in alts_html
    assert '<span class="tag">better</span>' in alts_html
    assert '<span class="tag">best</span>' in alts_html

    assert "Alternatives (" not in out["offersWithoutAlts"]

    sf_html = out["offersShortfall"]
    assert "Shortfall: 32 units not available by the delivery date." in sf_html
    for kind in ["partial", "split", "late"]:
        assert f'<span class="tag warn">{kind}</span>' in sf_html
    assert "48 l of 80 l by 2026-11-20, 32 l short" in sf_html
    assert "remaining 32 l on 2026-11-21" in sf_html
    assert "all 80 l available by 2026-11-21" in sf_html
    assert sf_html.index("Shortfall: 32") < sf_html.index("partial")


def test_render_quote_gross_and_offline_verify():
    out = _eval_console_js()
    q_html = out["quote"]
    assert "q_test_123" in q_html
    assert "1,180.00 EUR" in q_html
    assert "1,345.20 EUR" in q_html
    assert "Gross (with VAT)" in q_html
    assert "1 line: Valio Eila lactose-free milk 1 l" in q_html
    assert "2026-11-16" in q_html
    assert 'id="copy"' in q_html
    assert "signed Ed25519" in q_html
    assert "Verify signature offline" in q_html
    assert "python -m aimo.quotes verify 'eyJhbGciOiJFZERTQSJ9.testpayload1234567890.testsig1234567890'" in q_html
    assert 'href="/.well-known/jwks.json"' in q_html
    assert "EdDSA" in q_html


def test_render_order_reason_codes_and_inbox_deep_link():
    out = _eval_console_js()
    p_html = out["orderPending"]
    assert 'href="/inbox.html#ord_2b8c10"' in p_html
    assert "Open in Approvals" in p_html
    assert 'href="/audit.html?order_id=ord_2b8c10"' in p_html
    assert "OVER_ORDER_LIMIT" in p_html
    assert "SUBSTITUTION_NEEDS_APPROVAL" in p_html

    a_html = out["orderAuto"]
    assert "/inbox.html#" not in a_html
    assert "WITHIN_MANDATE" in a_html

    filled = out["filledOver"]
    assert filled["status"] == "pending_approval"
    assert filled["decision"]["outcome"] == "needs_approval"
    assert filled["decision"]["reasons"][0]["code"] == "OVER_ORDER_LIMIT"


def test_mock_end_to_end_runs():
    out = _eval_console_js()
    milk = out["milkRunHtml"]
    assert "Alternatives (3)" in milk
    assert "Gross (with VAT)" in milk
    assert "1,345.20 EUR" in milk
    assert "python -m aimo.quotes verify 'eyJhbGciOiJFZERTQSJ9." in milk
    assert "WITHIN_MANDATE" in milk

    school = out["schoolRunHtml"]
    assert "Shortfall: 32 units not available by the delivery date." in school
    assert "48 l of 80 l by 2026-11-20, 32 l short" in school
    assert "Alternatives (2)" in school
    assert "Alternatives (1)" in school
    assert "/inbox.html#ord_mock_" in school
    assert "OVER_ORDER_LIMIT" in school
    assert "SUBSTITUTION_NEEDS_APPROVAL" in school


@pytest.mark.filterwarnings("ignore:EdDSA is deprecated")
def test_real_backend_models_and_offline_jws_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))

    deliver = datetime.now(timezone.utc).date() + timedelta(days=2)
    valid_until = datetime.now(timezone.utc) + timedelta(hours=12)

    p_main = Product(
        product_id="400101",
        name="Valio Eila laktoositon kevytmaitojuoma 1 l",
        brand="Valio",
        sales_unit="PK",
        unit_size=UnitSize(value=1.0, unit="l"),
    )
    p_alt = Product(
        product_id="400202",
        name="Arla laktoositon kevytmaitojuoma 1 l",
        brand="Arla",
        sales_unit="PK",
        unit_size=UnitSize(value=1.0, unit="l"),
    )
    need = Need(
        customer="school-hervanta",
        deliver_by=deliver,
        items=[NeedItem(query="lactose-free milk", quantity=600, unit="l", constraints=Constraint(lactose_free=True))],
    )
    offers_resp = OffersResponse(
        items=[
            OfferGroup(
                need_item=need.items[0],
                offers=[
                    Offer(
                        product_id="400101",
                        product=p_main,
                        units_needed=550,
                        unit_price_net=1.20,
                        total_net=660.0,
                        total_gross=752.4,
                        available_amount=550,
                        available_date=deliver,
                        reliability=0.97,
                        co2_per_kg=1.1,
                        match_score=0.96,
                        tier="best",
                        notes=["stock partial"],
                    )
                ],
                shortfall=50.0,
                alternatives=[
                    Offer(
                        product_id="400202",
                        product=p_alt,
                        units_needed=600,
                        unit_price_net=1.15,
                        total_net=690.0,
                        total_gross=786.6,
                        available_amount=600,
                        available_date=deliver,
                        reliability=0.92,
                        co2_per_kg=1.18,
                        match_score=0.91,
                        tier="good",
                        notes=["cheapest acceptable: 4% cheaper per l"],
                    )
                ],
                options=[
                    {"kind": "partial", "date": deliver.isoformat(), "why": f"550 l of 600 l by {deliver}, 50 l short"},
                    {"kind": "late", "date": (deliver + timedelta(days=1)).isoformat(), "why": f"all 600 l available by {deliver + timedelta(days=1)}, 1 day after {deliver}"},
                ],
            )
        ]
    )

    line = QuoteLine(
        product_id="400202",
        name=p_alt.name,
        quantity=600,
        sales_unit="PK",
        unit_price_net=1.15,
        line_total_net=690.0,
        substitute_for="400101",
    )
    real_quote = quotes.issue("school-hervanta", "console", [line], deliver, valid_until, 786.60)
    mandate = Mandate(
        customer="school-hervanta",
        agent_id="console",
        per_order_eur=600.0,
        per_week_eur=3000.0,
        substitutions="ask",
        preferred_brands=["Valio"],
    )
    subs = [Substitution(original_id="400101", substitute_id="400202", reason="Shortfall swap")]
    decision = policy.decide(real_quote, mandate, {"400101": p_main, "400202": p_alt}, 0.0, subs)
    now = datetime.now(timezone.utc)
    real_order = Order(
        order_id="ord_real_99",
        quote_id=real_quote.quote_id,
        customer="school-hervanta",
        agent_id="console",
        lines=[line],
        total_net=real_quote.total_net,
        status="pending_approval",
        decision=decision,
        substitutions=subs,
        delivery_date=deliver,
        valid_until=valid_until,
        created_at=now,
        updated_at=now,
    )

    payload = json.dumps({
        "need": need.model_dump(mode="json"),
        "offers": offers_resp.model_dump(mode="json"),
        "quote": real_quote.model_dump(mode="json"),
        "order": real_order.model_dump(mode="json"),
    })
    runner = JS_BOOT + """
const p = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  offersHtml: C.offersHtml(p.need, p.offers),
  quoteHtml: C.quoteHtml(p.quote),
  orderHtml: C.orderHtml(p.order)
}));
"""
    proc = subprocess.run(
        [NODE, "-e", runner, str(W / "console.js"), payload],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(proc.stdout)

    assert "Alternatives (1)" in rendered["offersHtml"]
    assert '<span class="tag">good</span>' in rendered["offersHtml"]
    assert '<span class="tag warn">partial</span>' in rendered["offersHtml"]
    assert '<span class="tag warn">late</span>' in rendered["offersHtml"]

    assert "690.00 EUR" in rendered["quoteHtml"]
    assert "786.60 EUR" in rendered["quoteHtml"]
    m = re.search(r"python -m aimo\.quotes verify '([^']+)'", rendered["quoteHtml"])
    assert m is not None
    assert m.group(1) == real_quote.jws
    assert quotes.main(["aimo.quotes", "verify", m.group(1)]) == 0

    assert 'href="/inbox.html#ord_real_99"' in rendered["orderHtml"]
    assert "OVER_ORDER_LIMIT" in rendered["orderHtml"]
    assert "SUBSTITUTION_NEEDS_APPROVAL" in rendered["orderHtml"]


JS_LIVE_BOOT = r"""
const fs = require("fs");
function mkEl(tag) {
  const set = new Set();
  return {
    tagName: tag,
    className: "",
    textContent: "",
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    open: true,
    innerHTML: "",
    children: [],
    classList: {
      add(c) { set.add(c); },
      remove(c) { set.delete(c); },
      toggle(c, force) {
        const on = force === undefined ? !set.has(c) : !!force;
        if (on) set.add(c); else set.delete(c);
        return on;
      },
      contains(c) { return set.has(c); }
    },
    addEventListener() {},
    setAttribute() {},
    focus() {},
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; },
    append(c) { this.children.push(c); },
    prepend(c) { this.children.unshift(c); },
    querySelector(sel) {
      if (!this._q) this._q = {};
      if (!this._q[sel]) this._q[sel] = mkEl("sub");
      return this._q[sel];
    }
  };
}
const els = {};
const getEl = (sel) => {
  if (!els[sel]) els[sel] = mkEl(sel);
  return els[sel];
};
global.CustomEvent = class CustomEvent {
  constructor(t, o) { this.type = t; this.detail = o && o.detail; }
};
const winListeners = {};
global.window = global;
global.addEventListener = (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); };
global.dispatchEvent = (ev) => { (winListeners[ev.type] || []).forEach(fn => fn(ev)); };
global.location = { search: "" };
global.navigator = { clipboard: { writeText() {} } };
const store = {};
global.localStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem(k, v) { store[k] = String(v); },
  removeItem(k) { delete store[k]; }
};
global.performance = { now: () => 25 };
global.setTimeout = (fn, ms) => { if ((ms || 0) < 1000) Promise.resolve().then(fn); return 0; };
global.document = {
  readyState: "complete",
  querySelector(sel) {
    if (sel === "#out") return getEl("#result");
    return getEl(sel);
  },
  querySelectorAll() { return []; },
  getElementById(id) { return getEl("#" + id); },
  createElement(tag) { return mkEl(tag); }
};
const fetchLog = [];
let responder = async () => ({ status: 200, body: {} });
global.fetch = async (url, opts) => {
  const o = opts || {};
  fetchLog.push({
    url,
    method: o.method || "GET",
    headers: o.headers || {},
    body: o.body || null
  });
  const reply = await responder(url, o);
  if (reply && reply.throwOffline) {
    throw new TypeError("fetch failed");
  }
  const status = reply.status;
  const rawBody = reply.rawText !== undefined ? reply.rawText : JSON.stringify(reply.body);
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return JSON.parse(rawBody); },
    async text() { return rawBody; }
  };
};
eval(fs.readFileSync(process.argv[1], "utf8"));
eval(fs.readFileSync(process.argv[2], "utf8"));
fetchLog.length = 0;
const C = global.window.aimoConsole;
function traceSnap() {
  const rn = C.S.runs[C.S.runs.length - 1];
  const steps = {};
  for (const [k, li] of Object.entries(rn.calls)) {
    const det = li.querySelector(".h").children[0];
    steps[k] = {
      stClass: li.querySelector(".st").className,
      status: li.querySelector(".st").textContent,
      why: li.querySelector(".why").textContent,
      detailsHtml: det ? det.innerHTML : ""
    };
  }
  return {
    out: rn.out,
    cls: rn.cls,
    summaryHtml: rn.sum.innerHTML,
    steps
  };
}
"""


@pytest.fixture
def console_live_client(tmp_path, monkeypatch):
    empty_data = tmp_path / "empty_data"
    empty_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(empty_data))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    for env_var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "AIMO_AGENT_KEYS", "AIMO_ADMIN_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    monkeypatch.setattr(keys, "_cache", {})
    monkeypatch.setattr(offers_mod, "_overrides", {})
    monkeypatch.setattr(allocate, "_data", lambda: {})
    monkeypatch.setattr(parse, "_llm_parse", lambda *a, **kw: None)

    prod = Product(
        product_id="1001",
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
    )

    monkeypatch.setattr(catalog, "search", lambda q="", constraints=None, limit=50: [prod])
    monkeypatch.setattr(catalog, "get", lambda pid: prod if str(pid) == "1001" else None)
    monkeypatch.setattr(catalog, "reliability", lambda pid: 0.98 if str(pid) == "1001" else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: 1.12 if str(pid) == "1001" else None)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})
    monkeypatch.setattr(
        valio,
        "price",
        lambda customer, day, ids: {
            "1001": {
                "productId": "1001",
                "currency": "EUR",
                "catalogPrice": {"value": 1.18},
                "priceWithTax": {"value": 1.35},
            }
        }
        if "1001" in [str(x) for x in ids]
        else {},
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda customer, day, ids: {
            "1001": {
                "productId": "1001",
                "closingTime": "2026-11-12T23:59:59+02:00",
                "salesBlock": False,
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 2000.0,
                        "availableDate": day.isoformat(),
                        "salesUnit": "LTR",
                    }
                ],
            }
        }
        if "1001" in [str(x) for x in ids]
        else {},
    )

    with TestClient(app) as client:
        yield client


def test_ask_human_toggle_and_no_duplicate_fetch_in_source():
    html = (W / "console.html").read_text(encoding="utf-8")
    js = (W / "console.js").read_text(encoding="utf-8")
    assert '<label class="ask-h"><input type="checkbox" id="ask-human"> Ask human approver</label>' in html
    assert html.index('id="scenarios"') < html.index('id="ask-human"') < html.index('id="run"')
    assert "fetch(" not in js
    assert "aimo.lastError" in js
    assert "ask_human" in js
    assert "AGENT_REQUESTED" in js


@pytest.mark.filterwarnings("ignore:EdDSA is deprecated")
def test_live_errors_single_request_and_no_mock_fallback(console_live_client, monkeypatch):
    headers = {"X-Agent-Key": "demo-hotel-aurora"}
    parse_resp = console_live_client.post(
        "/v1/needs/parse",
        headers=headers,
        json={"text": "I need 1,000 litres of lactose-free milk in 3 days.", "customer": "hotel-aurora"},
    )
    assert parse_resp.status_code == 200
    need_doc = parse_resp.json()

    mand_doc = console_live_client.get("/v1/mandate", headers=headers).json()
    offers_doc = console_live_client.post("/v1/offers", headers=headers, json=need_doc).json()
    picked = offers_doc["items"][0]["offers"][0]
    quote_doc = console_live_client.post(
        "/v1/quotes",
        headers=headers,
        json={
            "offers": [{"product_id": picked["product_id"], "quantity": picked["units_needed"]}],
            "delivery_date": need_doc["deliver_by"],
        },
    ).json()

    r401 = console_live_client.post("/v1/offers", headers={"X-Agent-Key": "bad-key"}, json=need_doc)
    assert r401.status_code == 401

    r403 = console_live_client.post(
        "/v1/offers",
        headers=headers,
        json={**need_doc, "customer": "hospital-tays"},
    )
    assert r403.status_code == 403

    first_order = console_live_client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote_doc["quote_id"], "jws": quote_doc["jws"]},
    )
    assert first_order.status_code == 200
    r409_used = console_live_client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote_doc["quote_id"], "jws": quote_doc["jws"]},
    )
    assert r409_used.status_code == 409
    assert r409_used.json()["code"] == "QUOTE_USED"

    exp_line = QuoteLine(
        product_id="1001",
        name="Valio Eila laktoositon kevytmaitojuoma 1 l",
        quantity=100,
        sales_unit="LTR",
        unit_price_net=1.18,
        line_total_net=118.0,
    )
    exp_quote = quotes.issue(
        "hotel-aurora",
        "buyer-aurora",
        [exp_line],
        date.fromisoformat(need_doc["deliver_by"]),
        datetime.now(timezone.utc) - timedelta(minutes=5),
        135.0,
    )
    r409_exp = console_live_client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": exp_quote.quote_id, "jws": exp_quote.jws},
    )
    assert r409_exp.status_code == 409
    assert r409_exp.json()["code"] == "QUOTE_EXPIRED"

    def raise_upstream(*_a, **_kw):
        raise valio.UpstreamUnavailable("timeout")

    monkeypatch.setattr(valio, "price", raise_upstream)
    r503 = console_live_client.post("/v1/offers", headers=headers, json=need_doc)
    assert r503.status_code == 503
    assert r503.json()["code"] == "UPSTREAM_UNAVAILABLE"

    fixtures = json.dumps({
        "need": need_doc,
        "mandate": mand_doc,
        "offers": offers_doc,
        "quote": quote_doc,
        "e401": {"status": 401, "body": r401.json()},
        "e403": {"status": 403, "body": r403.json()},
        "e409_exp": {"status": 409, "body": r409_exp.json()},
        "e409_used": {"status": 409, "body": r409_used.json()},
        "e503": {"status": 503, "body": r503.json()},
    })

    script = JS_LIVE_BOOT + r"""
(async () => {
  const fx = JSON.parse(process.argv[3]);
  const directCases = [
    ["e401", fx.e401, 401, fx.e401.body.code, fx.e401.body.text],
    ["e403", fx.e403, 403, fx.e403.body.code, fx.e403.body.text],
    ["e404_str", { status: 404, body: { detail: "Order ord_missing not found." } }, 404, "not_found", "Order ord_missing not found."],
    ["e409_exp", fx.e409_exp, 409, "QUOTE_EXPIRED", fx.e409_exp.body.text],
    ["e409_used", fx.e409_used, 409, "QUOTE_USED", fx.e409_used.body.text],
    ["e422", { status: 422, body: { detail: [{ msg: "Quantity must be positive" }] } }, 422, "validation_error", "Quantity must be positive"],
    ["e500_html", { status: 500, rawText: "<html>500</html>" }, 500, "server_error", "Internal gateway error."],
    ["e500_text", { status: 500, rawText: "Internal Server Error" }, 500, "server_error", "Internal Server Error"],
    ["e503", fx.e503, 503, "UPSTREAM_UNAVAILABLE", fx.e503.body.text]
  ];
  const directOut = {};
  for (const [name, reply, expSt, expCode, expText] of directCases) {
    fetchLog.length = 0;
    responder = async () => reply;
    let caught = null;
    try {
      await C.call("test_tool", "POST", "/v1/quotes", { probe: name }, () => ({}), () => "ok");
    } catch (err) {
      caught = { message: err.message, httpStatus: err.httpStatus, code: err.code, text: err.text };
    }
    directOut[name] = {
      fetchCount: fetchLog.length,
      caught,
      mockMode: C.S.mock,
      mockTagOn: getEl(".mocktag").classList.contains("on"),
      trace: traceSnap().steps.test_tool,
      expSt,
      expCode,
      expText
    };
  }

  async function runScenario(routeOverrides) {
    fetchLog.length = 0;
    getEl("#text").value = "I need 1,000 litres of lactose-free milk by Monday.";
    getEl("#result").innerHTML = "";
    responder = async (url) => {
      if (routeOverrides[url]) return routeOverrides[url];
      if (url === "/v1/needs/parse") return { status: 200, body: fx.need };
      if (url === "/v1/mandate") return { status: 200, body: fx.mandate };
      if (url === "/v1/offers") return { status: 200, body: fx.offers };
      if (url === "/v1/quotes") return { status: 200, body: fx.quote };
      return { status: 200, body: {} };
    };
    await C.run();
    return {
      calls: fetchLog.slice(),
      resultHtml: getEl("#result").innerHTML,
      trace: traceSnap(),
      mockMode: C.S.mock,
      mockTagOn: getEl(".mocktag").classList.contains("on")
    };
  }

  const run401 = await runScenario({ "/v1/offers": fx.e401 });
  const run403 = await runScenario({ "/v1/offers": fx.e403 });
  const run409Exp = await runScenario({ "/v1/orders": fx.e409_exp });
  const run409Used = await runScenario({ "/v1/orders": fx.e409_used });
  const run503 = await runScenario({ "/v1/offers": fx.e503 });
  const runOffline = await runScenario({ "/v1/needs/parse": { throwOffline: true } });

  console.log(JSON.stringify({
    directOut,
    run401,
    run403,
    run409Exp,
    run409Used,
    run503,
    runOffline
  }));
})();
"""
    proc = subprocess.run(
        [NODE, "-e", script, str(W / "shell.js"), str(W / "console.js"), fixtures],
        check=True,
        capture_output=True,
        text=True,
    )
    res = json.loads(proc.stdout)

    for name, item in res["directOut"].items():
        assert item["fetchCount"] == 1, name
        assert item["mockMode"] is False, name
        assert item["mockTagOn"] is False, name
        assert item["caught"] == {
            "message": f"{item['expCode']}: {item['expText']}",
            "httpStatus": item["expSt"],
            "code": item["expCode"],
            "text": item["expText"],
        }, name
        assert item["trace"]["stClass"] == "st err", name
        assert item["trace"]["status"] == str(item["expSt"]), name
        assert item["trace"]["why"] == f"{item['expCode']}: {item['expText']}", name

    for key, target_url, failed_step, st, code, text in [
        ("run401", "/v1/offers", "get_offers", 401, "unauthorized", r401.json()["text"]),
        ("run403", "/v1/offers", "get_offers", 403, "forbidden", r403.json()["text"]),
        ("run409Exp", "/v1/orders", "place_order", 409, "QUOTE_EXPIRED", r409_exp.json()["text"]),
        ("run409Used", "/v1/orders", "place_order", 409, "QUOTE_USED", r409_used.json()["text"]),
        ("run503", "/v1/offers", "get_offers", 503, "UPSTREAM_UNAVAILABLE", r503.json()["text"]),
    ]:
        r = res[key]
        matching_calls = [c for c in r["calls"] if c["url"] == target_url]
        assert len(matching_calls) == 1, (key, r["calls"])
        assert r["mockMode"] is False, key
        assert r["mockTagOn"] is False, key
        assert f'<span class="tag stop">{st} {code}</span>' in r["resultHtml"], key
        assert f"HTTP {st}" in r["resultHtml"], key
        assert f"<code>{code}</code>" in r["resultHtml"], key
        assert text in r["resultHtml"], key
        assert "Gateway offline" not in r["resultHtml"], key
        step = r["trace"]["steps"][failed_step]
        assert step["stClass"] == "st err", key
        assert step["status"] == str(st), key
        assert step["why"] == f"{code}: {text}", key
        assert f"{st} {code}" in r["trace"]["summaryHtml"], key

    off = res["runOffline"]
    assert len([c for c in off["calls"] if c["url"] == "/v1/needs/parse"]) == 1
    assert off["mockMode"] is False
    assert off["mockTagOn"] is False
    assert "Gateway offline. The API did not answer, so no prices are shown." in off["resultHtml"]
    assert off["trace"]["steps"]["parse_request"]["status"] == "offline"
    assert "Gateway offline: /v1/needs/parse did not answer." in off["trace"]["steps"]["parse_request"]["why"]


@pytest.mark.filterwarnings("ignore:EdDSA is deprecated")
def test_ask_human_mock_vm_and_live_testclient(console_live_client):
    mock_script = JS_BOOT + r"""
(async () => {
  curCust = "hotel-aurora";
  getEl("#text").value = "I need 1,000 litres of lactose-free milk by Monday.";

  getEl("#ask-human").checked = false;
  getEl("#result").innerHTML = "";
  await C.run();
  const autoHtml = getEl("#result").innerHTML;
  const autoOrderStep = C.S.runs[C.S.runs.length - 1].calls.place_order.querySelector(".h").children[0].innerHTML;

  getEl("#ask-human").checked = true;
  getEl("#result").innerHTML = "";
  await C.run();
  const askHtml = getEl("#result").innerHTML;
  const askRun = C.S.runs[C.S.runs.length - 1];
  const askOrderStep = askRun.calls.place_order.querySelector(".h").children[0].innerHTML;
  const askWhy = askRun.calls.place_order.querySelector(".why").textContent;

  getEl("#ask-human").checked = false;
  getEl("#result").innerHTML = "";
  await C.run();
  const afterUncheckHtml = getEl("#result").innerHTML;

  getEl("#ask-human").checked = true;
  getEl("#text").value = "Last-minute event for 120 guests on Friday: 60 kg of salmon fillet";
  getEl("#result").innerHTML = "";
  await C.run();
  const blockedHtml = getEl("#result").innerHTML;

  console.log(JSON.stringify({
    autoHtml,
    autoOrderStep,
    askHtml,
    askOid: askRun.oid,
    askOut: askRun.out,
    askCls: askRun.cls,
    askOrderStep,
    askWhy,
    afterUncheckHtml,
    blockedHtml
  }));
})();
"""
    mock_proc = subprocess.run(
        [NODE, "-e", mock_script, str(W / "console.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    m = json.loads(mock_proc.stdout)

    assert "Auto-approved" in m["autoHtml"]
    assert "WITHIN_MANDATE" in m["autoHtml"]
    assert "/inbox.html#" not in m["autoHtml"]
    assert "ask_human" not in m["autoOrderStep"]

    assert "Needs approval" in m["askHtml"]
    assert "pending_approval" in m["askHtml"]
    assert "AGENT_REQUESTED" in m["askHtml"]
    assert "The agent asked for human approval." in m["askHtml"]
    assert f'href="/inbox.html#{m["askOid"]}"' in m["askHtml"]
    assert "Open in Approvals" in m["askHtml"]
    assert m["askOut"] == "needs approval"
    assert m["askCls"] == "needs_approval"
    assert "&quot;ask_human&quot;: true" in m["askOrderStep"]
    assert "The agent asked for human approval." in m["askWhy"]

    assert "Auto-approved" in m["afterUncheckHtml"]
    assert "WITHIN_MANDATE" in m["afterUncheckHtml"]
    assert "/inbox.html#" not in m["afterUncheckHtml"]

    assert "Blocked" in m["blockedHtml"]
    assert "CATEGORY_NOT_ALLOWED" in m["blockedHtml"]
    assert "AGENT_REQUESTED" not in m["blockedHtml"]

    headers = {"X-Agent-Key": "demo-hotel-aurora"}
    need_doc = console_live_client.post(
        "/v1/needs/parse",
        headers=headers,
        json={"text": "I need 500 litres of lactose-free milk in 3 days.", "customer": "hotel-aurora"},
    ).json()
    mand_doc = console_live_client.get("/v1/mandate", headers=headers).json()
    offers_doc = console_live_client.post("/v1/offers", headers=headers, json=need_doc).json()
    picked = offers_doc["items"][0]["offers"][0]
    quote_doc = console_live_client.post(
        "/v1/quotes",
        headers=headers,
        json={
            "offers": [{"product_id": picked["product_id"], "quantity": picked["units_needed"]}],
            "delivery_date": need_doc["deliver_by"],
        },
    ).json()
    order_resp = console_live_client.post(
        "/v1/orders",
        headers=headers,
        json={"quote_id": quote_doc["quote_id"], "jws": quote_doc["jws"], "ask_human": True},
    )
    assert order_resp.status_code == 200
    order_doc = order_resp.json()
    assert order_doc["status"] == "pending_approval"
    assert order_doc["decision"]["outcome"] == "needs_approval"
    assert order_doc["decision"]["reasons"] == [
        {"code": "AGENT_REQUESTED", "text": "The agent asked for human approval."}
    ]
    pending = console_live_client.get("/v1/approvals", headers={"X-Admin-Key": "demo-admin"}).json()
    assert any(o["order_id"] == order_doc["order_id"] for o in pending)

    live_payload = json.dumps({
        "need": need_doc,
        "mandate": mand_doc,
        "offers": offers_doc,
        "quote": quote_doc,
        "order": order_doc,
    })
    live_script = JS_LIVE_BOOT + r"""
(async () => {
  const fx = JSON.parse(process.argv[3]);
  responder = async (url) => {
    if (url === "/v1/needs/parse") return { status: 200, body: fx.need };
    if (url === "/v1/mandate") return { status: 200, body: fx.mandate };
    if (url === "/v1/offers") return { status: 200, body: fx.offers };
    if (url === "/v1/quotes") return { status: 200, body: fx.quote };
    if (url === "/v1/orders") return { status: 200, body: fx.order };
    return { status: 200, body: {} };
  };
  fetchLog.length = 0;
  getEl("#text").value = "I need 500 litres of lactose-free milk in 3 days.";
  getEl("#ask-human").checked = true;
  getEl("#result").innerHTML = "";
  await C.run();
  const orderCalls = fetchLog.filter(c => c.url === "/v1/orders");
  const askResultHtml = getEl("#result").innerHTML;
  const askTrace = traceSnap();

  fetchLog.length = 0;
  getEl("#ask-human").checked = false;
  getEl("#result").innerHTML = "";
  await C.run();
  const uncheckOrderCalls = fetchLog.filter(c => c.url === "/v1/orders");

  console.log(JSON.stringify({
    orderCallCount: orderCalls.length,
    orderReqBody: orderCalls[0] ? JSON.parse(orderCalls[0].body) : null,
    uncheckOrderCallCount: uncheckOrderCalls.length,
    uncheckOrderReqBody: uncheckOrderCalls[0] ? JSON.parse(uncheckOrderCalls[0].body) : null,
    resultHtml: askResultHtml,
    trace: askTrace
  }));
})();
"""
    live_proc = subprocess.run(
        [NODE, "-e", live_script, str(W / "shell.js"), str(W / "console.js"), live_payload],
        check=True,
        capture_output=True,
        text=True,
    )
    live = json.loads(live_proc.stdout)

    assert live["orderCallCount"] == 1
    assert live["orderReqBody"] == {
        "quote_id": quote_doc["quote_id"],
        "jws": quote_doc["jws"],
        "ask_human": True,
    }
    assert live["uncheckOrderCallCount"] == 1
    assert live["uncheckOrderReqBody"] == {
        "quote_id": quote_doc["quote_id"],
        "jws": quote_doc["jws"],
    }
    assert "Needs approval" in live["resultHtml"]
    assert "pending_approval" in live["resultHtml"]
    assert "AGENT_REQUESTED" in live["resultHtml"]
    assert "The agent asked for human approval." in live["resultHtml"]
    assert f'href="/inbox.html#{order_doc["order_id"]}"' in live["resultHtml"]
    assert "Open in Approvals" in live["resultHtml"]
    assert live["trace"]["out"] == "needs approval"
    assert live["trace"]["cls"] == "needs_approval"
    assert "The agent asked for human approval." in live["trace"]["steps"]["place_order"]["why"]


def test_err_info_fallbacks_and_xss_safe_banner():
    script = JS_BOOT + r"""
const statuses = [401, 403, 404, 409, 422, 500, 503];
const fallbacks = {};
for (const st of statuses) {
  fallbacks[st] = C.errInfo({ status: st, detail: null });
}
const strDetail = C.errInfo({ status: 403, detail: "Key revoked by admin" });
const partialObj = C.errInfo({ status: 409, detail: { code: "QUOTE_EXPIRED" } });
const xssBanner = C.errBannerHtml({
  httpStatus: 409,
  code: '<img src=x onerror="alert(1)">',
  text: '<script>alert("xss")</script>'
});
console.log(JSON.stringify({ fallbacks, strDetail, partialObj, xssBanner }));
"""
    proc = subprocess.run(
        [NODE, "-e", script, str(W / "console.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    out = json.loads(proc.stdout)
    expected_codes = {
        "401": "unauthorized",
        "403": "forbidden",
        "404": "not_found",
        "409": "conflict",
        "422": "validation_error",
        "500": "server_error",
        "503": "UPSTREAM_UNAVAILABLE",
    }
    for st_key, exp_code in expected_codes.items():
        info = out["fallbacks"][st_key]
        assert info["httpStatus"] == int(st_key)
        assert info["code"] == exp_code
        assert info["text"]

    assert out["strDetail"] == {"httpStatus": 403, "code": "forbidden", "text": "Key revoked by admin"}
    assert out["partialObj"]["httpStatus"] == 409
    assert out["partialObj"]["code"] == "QUOTE_EXPIRED"
    assert out["partialObj"]["text"]

    assert "<script>" not in out["xssBanner"]
    assert "<img" not in out["xssBanner"]
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in out["xssBanner"]
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in out["xssBanner"]


