from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tokenize
import warnings

from fastapi.testclient import TestClient
import pytest

from aimo import catalog, db, mandates, offers
from aimo.app import app
from aimo.models import Product, QuoteLine

warnings.filterwarnings("ignore", message="EdDSA is deprecated.*")

WEB = Path(__file__).resolve().parent.parent / "web"
FILES = ["inbox.html", "inbox.js", "inbox.css"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2705\u274C]")
COMMENT_RE = re.compile(r"<!--|-->|/\*|\*/|(?:^|[^:])//", re.M)


def _node_bin():
    fixed = Path("/home/silas/tools/node/bin/node")
    if fixed.is_file():
        return str(fixed)
    found = shutil.which("node")
    if not found:
        pytest.skip("node binary not available")
    return found


def test_files_exist():
    for f in FILES:
        assert (WEB / f).stat().st_size > 0


def test_no_emoji_no_em_dash_no_gradient():
    for f in FILES:
        t = (WEB / f).read_text(encoding="utf-8")
        assert not EMOJI.search(t), f
        assert "\u2014" not in t, f
        assert "gradient" not in t.lower(), f


def test_zero_comments_and_hygiene():
    for f in FILES:
        t = (WEB / f).read_text(encoding="utf-8")
        assert not COMMENT_RE.search(t), f
    self_text = Path(__file__).read_text(encoding="utf-8")
    assert "\u2014" not in self_text
    assert not EMOJI.search(self_text)
    tokens = list(tokenize.generate_tokens(io.StringIO(self_text).readline))
    assert not [tok for tok in tokens if tok.type == tokenize.COMMENT]


def test_html_zero_external_assets_and_favicon():
    html = (WEB / "inbox.html").read_text(encoding="utf-8")
    index_html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "https://" not in html
    assert not re.search(r'(?:src|href)=["\']?(?:https?:)?//', html, re.I)
    links = re.findall(r"<link\b[^>]*>", html, re.I)
    scripts = re.findall(r"<script\b[^>]*>", html, re.I)
    assert links and scripts
    for tag in links:
        href = re.search(r'href="([^"]+)"', tag)
        assert href, tag
        val = href.group(1)
        assert val.startswith("/") or val.startswith("data:image/svg+xml,"), val
    for tag in scripts:
        src = re.search(r'src="([^"]+)"', tag)
        assert src, tag
        val = src.group(1)
        assert val.startswith("/"), val
    assert 'href="/style.css"' in html
    assert 'href="/inbox.css"' in html
    assert 'src="/shell.js"' in html
    assert 'src="/inbox.js"' in html
    fav = re.search(r'<link rel="icon" href="([^"]+)">', index_html)
    assert fav and f'<link rel="icon" href="{fav.group(1)}">' in html


def test_links_shared_shell_and_mock():
    html = (WEB / "inbox.html").read_text(encoding="utf-8")
    assert "/style.css" in html and "/shell.js" in html
    assert 'aria-current="page"' in html
    js = (WEB / "inbox.js").read_text(encoding="utf-8")
    assert "mock=1" in js and "/v1/approvals" in js and "3000" in js


def test_error_detail_and_audit_link():
    js = (WEB / "inbox.js").read_text(encoding="utf-8")
    assert "lastError" in js and "Gateway offline" in js
    assert "/audit.html?order_id=" in js
    assert "Reason for rejecting" in js


def test_mock_matches_real_system():
    js = (WEB / "inbox.js").read_text(encoding="utf-8")
    assert "UNKNOWN_PRODUCT" in js and "allergen_flag" not in js
    assert "NON_PREFERRED_BRAND" in js and "Decided today" in (WEB / "inbox.html").read_text(encoding="utf-8")


def test_node_syntax_check():
    node = _node_bin()
    proc = subprocess.run([node, "--check", str(WEB / "inbox.js")], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
def live_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "inbox.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    fake_products = {
        "P-MILK-1": Product(product_id="P-MILK-1", name="Valio Eila laktoositon kevytmaitojuoma 1 l", brand="Valio", sales_unit="L"),
        "P-OAT-ORIG": Product(product_id="P-OAT-ORIG", name="Valio kaurahiutale", brand="Valio", sales_unit="KG"),
        "P-OAT-SUB": Product(product_id="P-OAT-SUB", name="Kaurahiutale gluteeniton", brand="Valio", sales_unit="KG"),
    }
    monkeypatch.setattr(catalog, "get", lambda pid: fake_products.get(str(pid)))

    def fake_price_lines(customer, delivery_date, req_lines):
        ql = []
        for rl in req_lines:
            pr = fake_products.get(rl.product_id)
            unit_price = 3.0 if rl.product_id == "P-MILK-1" else 4.0
            ql.append(
                QuoteLine(
                    product_id=rl.product_id,
                    name=pr.name if pr else rl.product_id,
                    quantity=rl.quantity,
                    sales_unit=pr.sales_unit if pr else "PCS",
                    unit_price_net=unit_price,
                    line_total_net=round(rl.quantity * unit_price, 2),
                    substitute_for=rl.substitute_for,
                )
            )
        net = round(sum(x.line_total_net for x in ql), 2)
        return ql, round(net * 1.14, 2), datetime.now(timezone.utc) + timedelta(hours=2)

    monkeypatch.setattr(offers, "price_lines", fake_price_lines)
    with TestClient(app) as c:
        yield c


def test_live_approvals_mandates_and_audit_integration(live_client):
    admin = {"X-Admin-Key": "demo-admin"}
    aurora_agent = {"X-Agent-Key": "demo-hotel-aurora"}
    tays_agent = {"X-Agent-Key": "demo-hospital-tays"}

    for route in ("/inbox.html", "/inbox.css", "/inbox.js"):
        res = live_client.get(route)
        assert res.status_code == 200 and res.text

    expected_approvers = {m.customer: m.approvers for m in mandates._MANDATES}
    js_text = (WEB / "inbox.js").read_text(encoding="utf-8")
    for cust, names in expected_approvers.items():
        mres = live_client.get(f"/v1/mandate?customer={cust}", headers=admin)
        assert mres.status_code == 200
        assert mres.json()["approvers"] == names
        for name in names:
            assert name in js_text

    delivery = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
    q1 = live_client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "P-MILK-1", "quantity": 1000}], "delivery_date": delivery},
        headers=aurora_agent,
    ).json()
    o1 = live_client.post("/v1/orders", json={"quote_id": q1["quote_id"], "jws": q1["jws"]}, headers=aurora_agent).json()
    assert o1["status"] == "pending_approval"
    assert any(r["code"] == "OVER_ORDER_LIMIT" for r in o1["decision"]["reasons"])

    q2 = live_client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "P-OAT-SUB", "quantity": 50, "substitute_for": "P-OAT-ORIG"}], "delivery_date": delivery},
        headers=tays_agent,
    ).json()
    o2 = live_client.post("/v1/orders", json={"quote_id": q2["quote_id"], "jws": q2["jws"]}, headers=tays_agent).json()
    assert o2["status"] == "pending_approval"
    assert o2["lines"][0]["substitute_for"] == "P-OAT-ORIG"
    assert o2["substitutions"][0]["original_id"] == "P-OAT-ORIG"
    assert o2["substitutions"][0]["substitute_id"] == "P-OAT-SUB"

    pending = live_client.get("/v1/approvals", headers=admin).json()
    assert {x["order_id"] for x in pending} == {o1["order_id"], o2["order_id"]}

    bad = live_client.post(
        f"/v1/approvals/{o1['order_id']}",
        json={"approve": True, "by": "Intruder Person", "note": ""},
        headers=admin,
    )
    assert bad.status_code == 403
    bad_body = bad.json()
    assert bad_body["code"] == "NOT_APPROVER"
    assert "Intruder Person" in bad_body["text"]

    ok1 = live_client.post(
        f"/v1/approvals/{o1['order_id']}",
        json={"approve": True, "by": "Laura Virtanen", "note": "Approved for breakfast"},
        headers=admin,
    )
    assert ok1.status_code == 200
    assert ok1.json()["status"] == "confirmed"

    again = live_client.post(
        f"/v1/approvals/{o1['order_id']}",
        json={"approve": True, "by": "Laura Virtanen", "note": ""},
        headers=admin,
    )
    assert again.status_code == 409
    assert again.json()["code"] == "NOT_PENDING"

    ok2 = live_client.post(
        f"/v1/approvals/{o2['order_id']}",
        json={"approve": False, "by": "Helena Korhonen", "note": "Keep exact oat spec"},
        headers=admin,
    )
    assert ok2.status_code == 200
    assert ok2.json()["status"] == "rejected"

    assert live_client.get("/v1/approvals", headers=admin).json() == []

    aud_aurora = live_client.get("/v1/audit?customer=hotel-aurora", headers=admin).json()["events"]
    app_ev = [e for e in aud_aurora if re.match(r"^approval\.(approved|rejected)$", e["action"]) and e["actor"].startswith("human:")]
    assert len(app_ev) == 1
    assert app_ev[0]["action"] == "approval.approved"
    assert app_ev[0]["actor"] == "human:Laura Virtanen"
    assert app_ev[0]["subject"] == o1["order_id"]
    assert "Laura Virtanen approved the order" in app_ev[0]["why"].split(" Held for approval")[0]

    aud_tays = live_client.get("/v1/audit?customer=hospital-tays", headers=admin).json()["events"]
    rej_ev = [e for e in aud_tays if re.match(r"^approval\.(approved|rejected)$", e["action"]) and e["actor"].startswith("human:")]
    assert len(rej_ev) == 1
    assert rej_ev[0]["action"] == "approval.rejected"
    assert rej_ev[0]["actor"] == "human:Helena Korhonen"
    assert rej_ev[0]["subject"] == o2["order_id"]
    assert "Keep exact oat spec" in rej_ev[0]["why"].split(" Held for approval")[0]

    missing = live_client.post("/v1/approvals/ord_missing", json={"approve": True, "by": "Laura Virtanen", "note": ""}, headers=admin)
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"

    q3 = live_client.post(
        "/v1/quotes",
        json={"offers": [{"product_id": "P-MILK-1", "quantity": 900}], "delivery_date": delivery},
        headers=aurora_agent,
    ).json()
    o3 = live_client.post("/v1/orders", json={"quote_id": q3["quote_id"], "jws": q3["jws"]}, headers=aurora_agent).json()
    assert o3["status"] == "pending_approval"
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with db.connect() as con:
        row = con.execute("SELECT payload FROM quotes WHERE quote_id=?", (q3["quote_id"],)).fetchone()
        payload = json.loads(row[0])
        payload["valid_until"] = past_iso
        con.execute("UPDATE quotes SET valid_until=?, payload=? WHERE quote_id=?", (past_iso, json.dumps(payload), q3["quote_id"]))
    exp = live_client.post(
        f"/v1/approvals/{o3['order_id']}",
        json={"approve": True, "by": "Laura Virtanen", "note": ""},
        headers=admin,
    )
    assert exp.status_code == 409
    assert exp.json()["code"] == "QUOTE_EXPIRED"
    assert live_client.get("/v1/approvals", headers=admin).json() == []


def test_inbox_js_dom_deep_link_who_subs_and_decide():
    node = _node_bin()
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const webDir = process.argv[1];
const shellSrc = fs.readFileSync(path.join(webDir, "shell.js"), "utf8");
const inboxSrc = fs.readFileSync(path.join(webDir, "inbox.js"), "utf8");

function makeEnv(opts) {
  const storeData = Object.assign({}, opts.storage || {});
  const listeners = {};
  const byId = {};

  class TextNode {
    constructor(t) { this.nodeType = 3; this.textContent = String(t); }
  }

  class El {
    constructor(tag) {
      this.nodeType = 1;
      this.tagName = String(tag).toUpperCase();
      this.id = "";
      this.className = "";
      this.value = "";
      this.hidden = false;
      this.disabled = false;
      this.open = false;
      this.returnValue = "";
      this.dataset = {};
      this.attrs = {};
      this.children = [];
      this.ev = {};
      this._html = "";
      const self = this;
      this.classList = {
        toggle(c, force) {
          const set = new Set(self.className.split(/\s+/).filter(Boolean));
          const add = force === undefined ? !set.has(c) : !!force;
          if (add) set.add(c); else set.delete(c);
          self.className = Array.from(set).join(" ");
          return add;
        },
        contains(c) { return self.className.split(/\s+/).includes(c); }
      };
    }
    setAttribute(k, v) {
      const s = String(v);
      this.attrs[k] = s;
      if (k === "id") { this.id = s; byId[s] = this; }
      else if (k === "class") this.className = s;
      else if (k === "value") this.value = s;
      else if (k === "hidden") this.hidden = true;
      else if (k.startsWith("data-")) {
        const camel = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        this.dataset[camel] = s;
      }
    }
    getAttribute(k) { return this.attrs[k] ?? null; }
    set innerHTML(v) {
      this._html = String(v);
      this.children = [];
      const re = /<option value="([^"]*)">([^<]*)<\/option>/g;
      let m;
      while ((m = re.exec(this._html))) {
        const opt = new El("option");
        opt.setAttribute("value", m[1]);
        opt.append(m[2]);
        this.children.push(opt);
      }
    }
    get innerHTML() { return this._html; }
    get textContent() {
      return this.children.map(c => c.textContent).join("");
    }
    set textContent(v) {
      this.children = v === "" || v == null ? [] : [new TextNode(v)];
    }
    append(...items) {
      for (const x of items) {
        if (x == null || x === false) continue;
        const n = x.nodeType ? x : new TextNode(x);
        this.children.push(n);
        indexTree(n);
      }
    }
    replaceChildren(...items) {
      this.children = [];
      this.append(...items);
    }
    addEventListener(type, fn, o) {
      (this.ev[type] = this.ev[type] || []).push({ fn, once: !!(o && o.once) });
    }
    dispatch(type, arg) {
      const list = (this.ev[type] || []).slice();
      this.ev[type] = (this.ev[type] || []).filter(x => !x.once);
      for (const item of list) item.fn(arg || { type, target: this });
    }
    click() { this.dispatch("click", { type: "click", target: this }); }
    focus() {}
    showModal() {
      this.open = true;
      if (opts.onDialog) opts.onDialog(this);
    }
  }

  function indexTree(n) {
    if (!n || n.nodeType !== 1) return;
    if (n.id) byId[n.id] = n;
    for (const c of n.children) indexTree(c);
  }

  const root = new El("body");
  root.dataset.view = "list";
  function mk(tag, id, cls) {
    const e = new El(tag);
    if (id) e.setAttribute("id", id);
    if (cls) e.className = cls;
    root.append(e);
    return e;
  }
  const top = mk("header", "", "top");
  const menuBtn = new El("button"); menuBtn.className = "menu-btn"; top.append(menuBtn);
  const mockTag = new El("span"); mockTag.className = "tag warn mocktag"; top.append(mockTag);
  const custSel = new El("select"); custSel.setAttribute("id", "customer"); top.append(custSel);
  mk("span", "count");
  const offTag = mk("span", "offTag"); offTag.hidden = true;
  mk("button", "who");
  mk("ol", "cards");
  const dec = mk("details", "decided"); dec.hidden = true;
  const dcount = new El("span"); dcount.setAttribute("id", "dcount"); dec.append(dcount);
  const dlist = new El("ul"); dlist.setAttribute("id", "dlist"); dec.append(dlist);
  mk("section", "detailPane");
  const toast = mk("div", "toast"); toast.hidden = true;
  const dlg = mk("dialog", "whoDlg");
  const wf = new El("div"); wf.setAttribute("id", "whoField"); dlg.append(wf);

  function walk(node, pred, out) {
    for (const c of node.children || []) {
      if (c.nodeType === 1) {
        if (pred(c)) out.push(c);
        walk(c, pred, out);
      }
    }
    return out;
  }
  function matchSel(el, sel) {
    if (sel.startsWith("#")) return el.id === sel.slice(1);
    if (sel.startsWith(".")) return el.classList.contains(sel.slice(1));
    if (sel === "[data-until]") return el.dataset.until !== undefined;
    return el.tagName.toLowerCase() === sel.toLowerCase();
  }

  const loc = { pathname: "/inbox.html", search: opts.search || "", hash: opts.hash || "" };
  const doc = {
    readyState: "complete",
    title: "",
    body: root,
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) { return walk(root, e => matchSel(e, sel), [])[0] || null; },
    querySelectorAll(sel) { return walk(root, e => matchSel(e, sel), []); },
    createElement(t) { return new El(t); },
    createElementNS(_, t) { return new El(t); },
    createTextNode(t) { return new TextNode(t); },
    addEventListener() {}
  };

  const win = {
    document: doc,
    location: loc,
    URLSearchParams,
    Intl,
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
    localStorage: {
      getItem(k) { return Object.prototype.hasOwnProperty.call(storeData, k) ? storeData[k] : null; },
      setItem(k, v) { storeData[k] = String(v); }
    },
    history: {
      replaceState(_, __, url) {
        const u = new URL(url, "http://localhost");
        loc.pathname = u.pathname;
        loc.search = u.search;
        loc.hash = u.hash;
      }
    },
    matchMedia() { return { matches: !!opts.wide, addEventListener() {} }; },
    addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
    dispatchEvent(ev) { for (const fn of listeners[ev.type] || []) fn(ev); },
    scrollTo() {},
    fetch: opts.fetch || (async () => ({ ok: false, status: 503, json: async () => ({}) })),
    setInterval() { return 1; },
    setTimeout(fn, ms) { if (ms <= 300) Promise.resolve().then(fn); return 1; },
    clearTimeout() {}
  };
  win.window = win;
  const ctx = vm.createContext(win);
  vm.runInContext(shellSrc, ctx);
  vm.runInContext(inboxSrc, ctx);
  return { win, doc, byId, storeData };
}

async function flush() {
  for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r));
}

(async () => {
  const m1 = makeEnv({ search: "?mock" });
  await flush();
  if (!m1.win.__inbox.mock) throw new Error("?mock did not enable mock mode");
  if (!m1.doc.querySelector(".mocktag").classList.contains("on")) throw new Error("mocktag not shown for ?mock");

  const m2 = makeEnv({ search: "?mock=1&order_id=ord_2b8c10", storage: { "aimo.customer": "hotel-aurora" } });
  await flush();
  if (m2.win.aimo.customer() !== "school-hervanta") throw new Error("?order_id did not sync customer: " + m2.win.aimo.customer());
  if (m2.byId.customer.value !== "school-hervanta") throw new Error("#customer select not synced");
  if (m2.doc.body.dataset.view !== "detail") throw new Error("detail view not opened for ?order_id");
  const detailText = m2.byId.detailPane.textContent;
  if (!detailText.includes("ord_2b8c10") || !detailText.includes("Valio Voipavut 500g") || !detailText.includes("Substitute for P-300077")) {
    throw new Error("missing substitution diff or line badge in detailPane: " + detailText);
  }
  const backBtn = m2.doc.querySelector(".back");
  backBtn.click();
  if (m2.win.location.search !== "?mock=1" || m2.win.location.hash !== "") {
    throw new Error("back button did not strip order_id cleanly: " + m2.win.location.search + m2.win.location.hash);
  }

  const m3 = makeEnv({ search: "?mock", hash: "#ord_91d4e7", storage: { "aimo.customer": "hotel-aurora" } });
  await flush();
  if (m3.win.aimo.customer() !== "hospital-tays") throw new Error("#hash did not sync customer");
  if (!m3.byId.detailPane.textContent.includes("Substitute for P-410455")) throw new Error("#hash detail missing substitute badge");

  let mandateCalls = 0;
  let pendingOrders = [];
  let auditEvents = [];
  let postResult = { status: 200, body: { status: "confirmed" } };

  const live = makeEnv({
    wide: true,
    storage: { "aimo.customer": "hotel-aurora" },
    onDialog(dlg) {
      const sel = dlg.children[0].children[0];
      if (sel && sel.tagName === "SELECT") sel.value = sel.children[0].value;
      dlg.returnValue = "ok";
      dlg.dispatch("close");
    },
    fetch: async (url, opts) => {
      if (url.startsWith("/v1/approvals/")) {
        if (postResult.status === 0) throw new Error("network down");
        return { ok: postResult.status === 200, status: postResult.status, json: async () => postResult.body };
      }
      if (url === "/v1/approvals") return { ok: true, status: 200, json: async () => pendingOrders.slice() };
      if (url.startsWith("/v1/mandate")) {
        mandateCalls++;
        return { ok: true, status: 200, json: async () => ({ approvers: ["Laura Virtanen", "Mikko Heikkinen"] }) };
      }
      if (url.startsWith("/v1/audit")) {
        return { ok: true, status: 200, json: async () => ({ events: auditEvents.slice() }) };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    }
  });
  await flush();

  live.byId.who.click();
  await flush();
  if (mandateCalls < 1) throw new Error("#who on empty list did not fetch mandate approvers");
  if (!live.byId.who.textContent.includes("Laura Virtanen")) throw new Error("#who text did not update to Laura Virtanen: " + live.byId.who.textContent);

  const sample = {
    order_id: "ord_live_1",
    quote_id: "q_live_1",
    customer: "hotel-aurora",
    agent_id: "buyer-aurora",
    status: "pending_approval",
    lines: [{ product_id: "P-2", name: "Valio Eila maito", quantity: 100, sales_unit: "L", unit_price_net: 2, line_total_net: 200, substitute_for: "P-1" }],
    total_net: 200,
    substitutions: [{ original_id: "P-1", substitute_id: "P-2", reason: "Substitute for Valio kevytmaito: Valio Eila maito." }],
    decision: { outcome: "needs_approval", reasons: [{ code: "SUBSTITUTION_NEEDS_APPROVAL", text: "Needs check" }] },
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    delivery_date: "2026-11-16",
    valid_until: new Date(Date.now() + 3600e3).toISOString()
  };
  pendingOrders = [sample];
  await live.win.__inbox.poll();
  await flush();
  if (!live.byId.detailPane.textContent.includes("Valio kevytmaito")) throw new Error("parsed original name missing in subs()");

  postResult = { status: 403, body: { code: "NOT_APPROVER", text: "Ghost is not an approver for hotel-aurora." } };
  await live.win.__inbox.decide(sample, true, "");
  await flush();
  if (!live.byId.toast.textContent.includes("Ghost is not an approver for hotel-aurora. The order is still waiting.")) {
    throw new Error("unexpected 403 toast: " + live.byId.toast.textContent);
  }
  if (live.win.__inbox.name !== "") throw new Error("403 NOT_APPROVER should clear invalid S.name");
  if (live.win.__inbox.orders.length !== 1) throw new Error("403 should keep pending order in list");

  live.win.__inbox.name = "Laura Virtanen";
  postResult = { status: 0, body: null };
  await live.win.__inbox.decide(sample, true, "");
  await flush();
  if (!live.byId.toast.textContent.includes("Gateway offline. The order is still waiting.")) {
    throw new Error("unexpected offline toast: " + live.byId.toast.textContent);
  }

  pendingOrders = [];
  postResult = { status: 409, body: { code: "QUOTE_EXPIRED", text: "The quote is no longer valid, order marked expired." } };
  await live.win.__inbox.decide(sample, true, "");
  await flush();
  if (live.win.__inbox.orders.length !== 0) throw new Error("expired order should be removed after poll");
  if (!live.byId.toast.textContent.includes("The quote is no longer valid, order marked expired.") || live.byId.toast.textContent.includes("still waiting")) {
    throw new Error("unexpected expired toast: " + live.byId.toast.textContent);
  }

  pendingOrders = [sample];
  await live.win.__inbox.poll();
  pendingOrders = [];
  postResult = { status: 409, body: { code: "NOT_PENDING", text: "Order ord_live_1 is confirmed, not waiting for approval." } };
  await live.win.__inbox.decide(sample, true, "");
  await flush();
  if (live.win.__inbox.orders.length !== 0) throw new Error("already-decided order should be removed after poll");
  if (!live.byId.toast.textContent.includes("not waiting for approval.") || live.byId.toast.textContent.includes("still waiting")) {
    throw new Error("unexpected NOT_PENDING toast: " + live.byId.toast.textContent);
  }

  pendingOrders = [sample];
  await live.win.__inbox.poll();
  pendingOrders = [];
  auditEvents = [{
    seq: 10,
    ts: new Date().toISOString(),
    customer: "hotel-aurora",
    actor: "human:Laura Virtanen",
    action: "approval.approved",
    subject: "ord_live_1",
    why: "Laura Virtanen approved the order of 200.00 EUR. Note: ok. Held for approval because: Needs check."
  }];
  postResult = { status: 200, body: { status: "confirmed" } };
  await live.win.__inbox.decide(sample, true, "ok");
  await flush();
  if (live.win.__inbox.orders.length !== 0) throw new Error("approved order still in S.orders");
  if (live.byId.decided.hidden || live.byId.dcount.textContent !== "1") throw new Error("Decided today not updated");
  if (!live.byId.dlist.textContent.includes("Laura Virtanen approved the order")) throw new Error("Decided today text mismatch");

  const rejOrder = Object.assign({}, sample, { order_id: "ord_live_2" });
  pendingOrders = [rejOrder];
  await live.win.__inbox.poll();
  await flush();
  const btns = live.doc.querySelectorAll("button").filter(b => b.textContent === "Reject");
  btns[0].click();
  const noteEl = live.byId.note;
  noteEl.value = "Too expensive";
  noteEl.dispatch("input");
  pendingOrders = [];
  auditEvents.push({
    seq: 11,
    ts: new Date().toISOString(),
    customer: "hotel-aurora",
    actor: "human:Laura Virtanen",
    action: "approval.rejected",
    subject: "ord_live_2",
    why: "Laura Virtanen rejected the order of 200.00 EUR. Note: Too expensive. Held for approval because: Needs check."
  });
  postResult = { status: 200, body: { status: "rejected" } };
  btns[0].click();
  await flush();
  if (live.byId.dcount.textContent !== "2" || !live.byId.dlist.textContent.includes("Too expensive")) {
    throw new Error("Rejected order missing in Decided today: " + live.byId.dlist.textContent);
  }
})();
"""
    assert not COMMENT_RE.search(harness)
    proc = subprocess.run([node, "-e", harness, str(WEB)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
