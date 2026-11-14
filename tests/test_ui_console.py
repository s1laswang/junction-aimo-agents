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
from aimo import policy, quotes
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
IDS = ["text", "run", "scenarios", "result", "trace", "customer", "live", "trace-empty", "req"]
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
