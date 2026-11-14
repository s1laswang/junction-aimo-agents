from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from aimo import audit, orders
from aimo.app import app
from aimo.models import Decision, Order, QuoteLine, Reason

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"
NODE = Path("/home/silas/tools/node/bin/node")
EMOJI_RE = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf]")
CUSTOMERS = ["hotel-aurora", "hospital-tays", "school-hervanta"]
TILE_LINKS = ["/console.html", "/inbox.html", "/audit.html", "/admin.html"]
DISCOVERY_LINKS = ["/.well-known/agent-card.json", "/openapi.json", "/.well-known/jwks.json"]
REQUIRED_IDS = [
    "customer",
    "n-ord",
    "d-ord",
    "n-appr",
    "n-audit",
    "d-audit",
    "n-man",
    "d-man",
    "pend",
    "act",
    "act-v",
    "connect",
    "c-mcp",
    "c-stdio",
    "c-rest",
    "c-card",
    "c-curl",
]


def _node_bin():
    if NODE.is_file():
        return str(NODE)
    found = shutil.which("node")
    assert found, "node binary not found"
    return found


def _inline_scripts(html):
    out = []
    for attrs, body in re.findall(r"<script([^>]*)>([\s\S]*?)</script>", html, re.I):
        if not re.search(r"\bsrc\s*=", attrs, re.I):
            out.append(body)
    return out


def _inline_styles(html):
    return re.findall(r"<style[^>]*>([\s\S]*?)</style>", html, re.I)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "aimo.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    with TestClient(app) as c:
        yield c


def test_index_file_exists():
    assert INDEX.is_file()
    assert INDEX.stat().st_size > 0


def test_index_no_emoji_em_dash_gradient_or_comments():
    html = INDEX.read_text(encoding="utf-8")
    assert not EMOJI_RE.search(html)
    assert "\u2014" not in html
    assert ("grad" + "ient") not in html.lower()
    assert "&nbsp;" not in html
    assert ("<!" + "--") not in html
    assert ("--" + ">") not in html
    styles = _inline_styles(html)
    assert styles
    for css in styles:
        assert ("/" + "*") not in css
        assert ("*" + "/") not in css
    scripts = _inline_scripts(html)
    assert len(scripts) == 1
    for js in scripts:
        assert ("/" + "*") not in js
        assert ("*" + "/") not in js
        assert ("/" + "/") not in js


def test_self_hygiene():
    me = Path(__file__).read_text(encoding="utf-8")
    assert not EMOJI_RE.search(me)
    assert "\u2014" not in me
    assert chr(35) not in me
    assert ("/" + "/") not in me
    assert ("/" + "*") not in me
    assert ("<!" + "--") not in me


def test_index_no_external_scripts_or_links():
    html = INDEX.read_text(encoding="utf-8")
    script_srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I)
    assert script_srcs == ["/shell.js"]
    for src in script_srcs:
        assert src.startswith("/") and not src.startswith("/" + "/")
    link_hrefs = re.findall(r'<link[^>]+href=["\']([^"\']+)["\']', html, re.I)
    assert "/style.css" in link_hrefs
    for href in link_hrefs:
        assert href.startswith(("/", "data:")), href
        assert not href.startswith("/" + "/"), href
    assert 'aria-current="page"' in html
    assert 'class="brand" href="/"' in html


def test_index_links_and_ids_present():
    html = INDEX.read_text(encoding="utf-8")
    for link in TILE_LINKS + DISCOVERY_LINKS:
        assert f'href="{link}"' in html, link
    for elem_id in REQUIRED_IDS:
        assert f'id="{elem_id}"' in html, elem_id


def test_index_inline_script_node_syntax(tmp_path):
    scripts = _inline_scripts(INDEX.read_text(encoding="utf-8"))
    assert len(scripts) == 1
    js_file = tmp_path / "index_inline.js"
    js_file.write_text(scripts[0], encoding="utf-8")
    proc = subprocess.run(
        [_node_bin(), "--check", str(js_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_index_live_mock_and_offline_dom_behavior(tmp_path):
    script_body = _inline_scripts(INDEX.read_text(encoding="utf-8"))[0]
    harness = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync(process.argv[2], "utf8");

function runScenario(setupFn) {
  const els = {};
  const winListeners = {};
  const docListeners = {};
  function makeEl(id) {
    return {
      id,
      textContent: "",
      innerHTML: "",
      className: "",
      hidden: false,
      dataset: {}
    };
  }
  const ids = [
    "customer","n-ord","d-ord","n-appr","n-audit","d-audit","n-man","d-man",
    "pend","act","act-v","connect","c-mcp","c-stdio","c-rest","c-card","c-curl"
  ];
  for (const id of ids) els[id] = makeEl(id);

  const calls = [];
  const state = {
    customer: "hotel-aurora",
    mock: false,
    mockBadge: false,
    lastError: null,
    handler: null
  };

  const aimo = {
    customer() { return state.customer; },
    mockMode() { return state.mock; },
    mock(on) { state.mockBadge = Boolean(on); },
    get lastError() { return state.lastError; },
    offline(el) {
      const msg = state.lastError && state.lastError.status ? "Unavailable" : "Gateway offline";
      if (el) el.innerHTML = '<p class="empty muted">' + msg + "</p>";
      return msg;
    },
    async api(path, opts) {
      calls.push({ path, opts });
      state.lastError = null;
      return state.handler(path, opts, state);
    }
  };

  const sandbox = {
    location: { origin: "https:" + String.fromCharCode(47, 47) + "aimo.example" },
    navigator: {},
    Intl,
    Promise,
    Number,
    String,
    Boolean,
    Array,
    Date,
    encodeURIComponent,
    setTimeout(fn) { fn(); },
    aimo,
    window: {
      aimo,
      addEventListener(ev, fn) { (winListeners[ev] = winListeners[ev] || []).push(fn); },
      async dispatch(ev) {
        for (const fn of (winListeners[ev] || [])) await fn();
      }
    },
    document: {
      getElementById(id) {
        if (!els[id]) els[id] = makeEl(id);
        return els[id];
      },
      createElement() {
        let txt = "";
        return {
          set textContent(v) {
            txt = String(v)
              .replace(/&/g, "&amp;")
              .replace(/</g, "&lt;")
              .replace(/>/g, "&gt;");
          },
          get innerHTML() { return txt; }
        };
      },
      addEventListener(ev, fn) { (docListeners[ev] = docListeners[ev] || []).push(fn); }
    }
  };

  setupFn(state);
  vm.runInNewContext(code, sandbox);
  return { els, calls, state, win: sandbox.window };
}

(async () => {
  const live = runScenario((st) => {
    st.handler = (path) => {
      if (path === "/v1/approvals?customer=hotel-aurora") {
        return [{
          order_id: "ord_live/1",
          customer: "hotel-aurora",
          total_net: 2650.04,
          decision: { reasons: [{ text: "Over limit" }] }
        }];
      }
      if (path === "/v1/audit?customer=hotel-aurora") {
        return {
          verified: { ok: true, bad_seq: null },
          events: [
            { seq: 9, ts: "2026-11-14T10:05:00Z", customer: "hotel-aurora", action: "order.pending_approval", subject: "ord_subj_9", why: "Held for human", detail: {} },
            { seq: 8, ts: "2026-11-14T10:04:00Z", customer: "hotel-aurora", action: "quote.issued", subject: "q_live_8", why: "Quote signed", detail: { order_id: "ord_det_8", quote_id: "q_live_8" } },
            { seq: 7, ts: "2026-11-14T10:01:00Z", customer: "hotel-aurora", action: "parse.request", subject: "hotel-aurora", why: "Parsed milk", detail: {} }
          ]
        };
      }
      if (path === "/v1/mandate?customer=hotel-aurora") {
        return { customer: "hotel-aurora", per_order_eur: 2500, per_week_eur: 15000 };
      }
      if (path === "/v1/orders?customer=hotel-aurora") {
        return [
          { order_id: "ord_live/1", status: "pending_approval" },
          { order_id: "ord_2", status: "confirmed" },
          { order_id: "ord_3", status: "rejected" }
        ];
      }
      if (path === "/v1/approvals?customer=hospital-tays") return [];
      if (path === "/v1/audit?customer=hospital-tays") {
        return { verified: { ok: false, bad_seq: 3 }, events: [] };
      }
      if (path === "/v1/mandate?customer=hospital-tays") {
        return { customer: "hospital-tays", per_order_eur: 1500, per_week_eur: 8000 };
      }
      if (path === "/v1/orders?customer=hospital-tays") return [];
      throw new Error("Unexpected path: " + path);
    };
  });
  await new Promise((r) => setImmediate(r));

  assert.equal(live.calls.length, 4);
  for (const c of live.calls) assert.equal(c.opts.admin, true);
  assert.equal(live.els["n-ord"].textContent, "3");
  assert.match(live.els["d-ord"].textContent, /2 active/);
  assert.equal(live.els["n-appr"].textContent, "1");
  assert.match(live.els["pend"].innerHTML, /href="\/inbox\.html\x23ord_live%2F1"/);
  assert.equal(live.els["n-audit"].textContent, String.fromCharCode(35) + "9");
  assert.match(live.els["d-audit"].textContent, /^verified /);
  assert.equal(live.els["act-v"].hidden, false);
  assert.equal(live.els["act-v"].textContent, "chain verified");
  assert.match(live.els["act"].innerHTML, /href="\/audit\.html\?order_id=ord_subj_9"/);
  assert.match(live.els["act"].innerHTML, /href="\/audit\.html\?order_id=ord_det_8"/);
  assert.match(live.els["act"].innerHTML, /href="\/audit\.html"/);
  assert.equal(live.els["n-man"].textContent, "2,500");
  assert.equal(live.els["d-man"].textContent, "per order (15,000/wk) EUR");
  assert.match(live.els["c-curl"].textContent, /X-Agent-Key: demo-hotel-aurora/);
  assert.match(live.els["c-curl"].textContent, /"customer":"hotel-aurora"/);

  live.state.customer = "hospital-tays";
  await live.win.dispatch("customer");
  await new Promise((r) => setImmediate(r));
  assert.equal(live.calls.length, 8);
  assert.equal(live.els["n-ord"].textContent, "0");
  assert.equal(live.els["n-appr"].textContent, "0");
  assert.equal(live.els["n-man"].textContent, "1,500");
  assert.equal(live.els["d-man"].textContent, "per order (8,000/wk) EUR");
  assert.match(live.els["d-audit"].textContent, /^chain broken /);
  assert.equal(live.els["act-v"].textContent, "chain broken");
  assert.match(live.els["c-curl"].textContent, /X-Agent-Key: demo-hospital-tays/);
  assert.match(live.els["c-curl"].textContent, /"customer":"hospital-tays"/);

  for (const cust of ["hotel-aurora", "hospital-tays", "school-hervanta"]) {
    const mk = runScenario((st) => {
      st.mock = true;
      st.customer = cust;
      st.handler = () => { throw new Error("No network in mock mode"); };
    });
    await new Promise((r) => setImmediate(r));
    assert.equal(mk.calls.length, 0);
    assert.equal(mk.state.mockBadge, true);
    assert.notEqual(mk.els["n-ord"].textContent, "-");
    assert.equal(mk.els["n-appr"].textContent, "1");
    assert.match(mk.els["pend"].innerHTML, /href="\/inbox\.html\x23ord_/);
    assert.match(mk.els["act"].innerHTML, /href="\/audit\.html\?order_id=ord_/);
    assert.match(mk.els["d-man"].textContent, /\/wk\) EUR$/);
    assert.match(mk.els["c-curl"].textContent, new RegExp("X-Agent-Key: demo-" + cust));
    assert.match(mk.els["c-curl"].textContent, new RegExp('"customer":"' + cust + '"'));
  }

  const off = runScenario((st) => {
    st.handler = (_p, _o, s) => {
      s.lastError = { status: 0, detail: "gateway offline" };
      return null;
    };
  });
  await new Promise((r) => setImmediate(r));
  assert.equal(off.els["n-ord"].textContent, "-");
  assert.equal(off.els["n-appr"].textContent, "-");
  assert.equal(off.els["n-audit"].textContent, "-");
  assert.equal(off.els["n-man"].textContent, "-");
  assert.equal(off.els["d-audit"].textContent, "gateway offline");
  assert.equal(off.els["act-v"].hidden, true);
  assert.match(off.els["pend"].innerHTML, /Gateway offline/);
  assert.match(off.els["act"].innerHTML, /Gateway offline/);
})();
"""
    js_path = tmp_path / "index_inline.js"
    harness_path = tmp_path / "harness.js"
    js_path.write_text(script_body, encoding="utf-8")
    harness_path.write_text(harness, encoding="utf-8")
    proc = subprocess.run(
        [_node_bin(), str(harness_path), str(js_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def _seed_order(order_id, quote_id, customer, agent_id, status, total_net):
    now = datetime.now(timezone.utc)
    valid_until = now + timedelta(hours=4)
    delivery_date = date.today() + timedelta(days=2)
    order = Order(
        order_id=order_id,
        quote_id=quote_id,
        customer=customer,
        agent_id=agent_id,
        lines=[
            QuoteLine(
                product_id="P-100",
                name="Laktoositon maito 1 L",
                quantity=100,
                sales_unit="L",
                unit_price_net=1.4,
                vat_rate=14.0,
                line_total_net=total_net,
            )
        ],
        total_net=total_net,
        status=status,
        delivery_date=delivery_date,
        valid_until=valid_until,
        decision=Decision(
            outcome="needs_approval" if status == "pending_approval" else "auto",
            reasons=[Reason(code="OVER_ORDER_LIMIT", text="Over per-order limit")] if status == "pending_approval" else [],
        ),
        created_at=now,
        updated_at=now,
    )
    con = orders._con()
    try:
        con.execute(
            "insert into orders values (?,?,?,?,?,?,?,?,NULL)",
            (
                order.order_id,
                quote_id,
                customer,
                order.status,
                now.isoformat(),
                valid_until.isoformat(),
                delivery_date.isoformat(),
                order.model_dump_json(),
            ),
        )
        con.commit()
    finally:
        con.close()
    audit.record(
        customer,
        f"agent:{agent_id}",
        f"order.{status}",
        order_id,
        "Seeded order for overview test.",
        {"order_id": order_id, "quote_id": quote_id, "total_net": total_net},
    )


def test_index_gateway_endpoints_contract(client):
    root_resp = client.get("/")
    assert root_resp.status_code == 200
    assert 'id="n-ord"' in root_resp.text

    _seed_order("ord_aurora_1", "q_aurora_1", "hotel-aurora", "buyer-aurora", "pending_approval", 2650.0)
    _seed_order("ord_tays_1", "q_tays_1", "hospital-tays", "buyer-tays", "confirmed", 420.0)

    headers = {"X-Admin-Key": "demo-admin"}
    expected_limits = {
        "hotel-aurora": (2500.0, 15000.0),
        "hospital-tays": (1500.0, 8000.0),
        "school-hervanta": (600.0, 3000.0),
    }

    for cust in CUSTOMERS:
        appr = client.get(f"/v1/approvals?customer={cust}", headers=headers)
        assert appr.status_code == 200
        appr_body = appr.json()
        assert isinstance(appr_body, list)
        for item in appr_body:
            assert item["customer"] == cust
            assert {"order_id", "customer", "total_net", "decision"} <= set(item.keys())

        aud = client.get(f"/v1/audit?customer={cust}", headers=headers)
        assert aud.status_code == 200
        aud_body = aud.json()
        assert {"events", "text", "verified", "checkpoint"} <= set(aud_body.keys())
        assert aud_body["verified"] == {"ok": True, "bad_seq": None}
        assert {"seq", "hash", "ts", "jws"} <= set(aud_body["checkpoint"].keys())
        for ev in aud_body["events"]:
            assert ev["customer"] == cust
            assert {"seq", "ts", "customer", "action", "subject", "detail", "why"} <= set(ev.keys())

        man = client.get(f"/v1/mandate?customer={cust}", headers=headers)
        assert man.status_code == 200
        man_body = man.json()
        assert {"customer", "agent_id", "per_order_eur", "per_week_eur", "approvers"} <= set(man_body.keys())
        assert man_body["customer"] == cust
        assert (man_body["per_order_eur"], man_body["per_week_eur"]) == expected_limits[cust]

        ords = client.get(f"/v1/orders?customer={cust}", headers=headers)
        assert ords.status_code == 200
        ords_body = ords.json()
        assert isinstance(ords_body, list)
        for item in ords_body:
            assert item["customer"] == cust
            assert {"order_id", "customer", "status", "total_net"} <= set(item.keys())

    assert len(client.get("/v1/approvals?customer=hotel-aurora", headers=headers).json()) == 1
    assert len(client.get("/v1/approvals?customer=hospital-tays", headers=headers).json()) == 0
    assert len(client.get("/v1/orders?customer=hospital-tays", headers=headers).json()) == 1
    assert len(client.get("/v1/orders?customer=school-hervanta", headers=headers).json()) == 0

    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200
    card_body = card.json()
    assert {"name", "description", "endpoints", "auth", "tools", "policies"} <= set(card_body.keys())

    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    openapi_body = openapi.json()
    assert {"openapi", "info", "paths"} <= set(openapi_body.keys())
    for route in ["/v1/approvals", "/v1/audit", "/v1/mandate", "/v1/orders", "/v1/needs/parse"]:
        assert route in openapi_body["paths"]

    jwks = client.get("/.well-known/jwks.json")
    assert jwks.status_code == 200
    jwks_body = jwks.json()
    assert "keys" in jwks_body and len(jwks_body["keys"]) >= 1
    assert {"kty", "crv", "x", "kid"} <= set(jwks_body["keys"][0].keys())
