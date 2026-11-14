import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
FILES = ["audit.html", "audit.js", "audit.css"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27bf\u2b50\u2705\u274c]")


def _node_bin():
    preferred = Path("/home/silas/tools/node/bin/node")
    if preferred.is_file():
        return str(preferred)
    found = shutil.which("node")
    if found:
        return found
    pytest.skip("node binary unavailable")


NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const auditCode = fs.readFileSync(payload.auditJsPath, "utf8");

function mountAudit(opts) {
  const urlStr = opts.url || "http://127.0.0.1:8000/audit.html?mock=1";
  const loc = new URL(urlStr);
  const els = {};
  function makeEl(id) {
    return {
      id: id,
      value: "",
      textContent: "",
      innerHTML: "",
      className: "",
      title: "",
      hidden: false,
      dataset: {},
      attrs: {},
      listeners: {},
      parentNode: { dataset: {} },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] || null; },
      addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
      trigger(t, ev) {
        const e = Object.assign({ type: t, target: this, preventDefault() {}, closest() { return null; } }, ev || {});
        (this.listeners[t] || []).forEach(fn => fn.call(this, e));
      },
      scrollIntoView(arg) { this.scrolled = arg || true; },
      click() {},
      remove() {}
    };
  }
  function getEl(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  }
  [
    "f-order", "filters", "f-actor", "f-action", "f-customer", "f-reset", "legend",
    "v-ledger", "v-text", "export", "print", "ledger", "how-n", "badge",
    "plain", "receipt", "scope", "cp-box", "cp-seq", "cp-jws", "cp-copy", "jump"
  ].forEach(getEl);

  const winListeners = {};
  const aimo = {
    CUSTOMERS: ["hotel-aurora", "hospital-tays", "school-hervanta"],
    lastError: null,
    mockFlag: false,
    customer() { return opts.defaultCustomer || "hotel-aurora"; },
    mock(on) { this.mockFlag = !!on; },
    offline(el) { if (el) el.innerHTML = '<p class="empty muted">Gateway offline</p>'; return "Gateway offline"; },
    async api(path, reqOpts) {
      if (opts.api) return await opts.api(path, reqOpts);
      return null;
    }
  };
  const win = {
    aimo: aimo,
    addEventListener(t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); },
    dispatchCustomer(c) { (winListeners.customer || []).forEach(fn => fn({ detail: c })); },
    dispatchHash(h) {
      loc.hash = h;
      loc.href = loc.origin + loc.pathname + loc.search + h;
      (winListeners.hashchange || []).forEach(fn => fn({ type: "hashchange" }));
    },
    print() {}
  };
  const doc = {
    getElementById: getEl,
    createElement(tag) { return makeEl(tag); },
    body: { appendChild() {} }
  };
  const ctx = vm.createContext({
    window: win,
    document: doc,
    location: loc,
    history: {
      replaceState(_s, _t, u) {
        const nu = new URL(String(u));
        loc.href = nu.href;
        loc.search = nu.search;
        loc.hash = nu.hash;
      }
    },
    navigator: { clipboard: { writeText: async () => {} } },
    aimo: aimo,
    crypto: opts.crypto || globalThis.crypto,
    TextEncoder: globalThis.TextEncoder,
    TextDecoder: globalThis.TextDecoder,
    URL: globalThis.URL,
    URLSearchParams: globalThis.URLSearchParams,
    Blob: globalThis.Blob,
    Intl: globalThis.Intl,
    atob: globalThis.atob,
    btoa: globalThis.btoa,
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    Date: globalThis.Date,
    Math: globalThis.Math,
    JSON: globalThis.JSON,
    Object: globalThis.Object,
    Array: globalThis.Array,
    String: globalThis.String,
    Number: globalThis.Number,
    Boolean: globalThis.Boolean,
    RegExp: globalThis.RegExp,
    Set: globalThis.Set,
    Uint8Array: globalThis.Uint8Array,
    Error: globalThis.Error,
    isNaN: globalThis.isNaN,
    encodeURIComponent: globalThis.encodeURIComponent
  });
  vm.runInContext(auditCode, ctx, { filename: "audit.js" });
  return { win, els, loc, aimo };
}

function flush() {
  return new Promise(r => setTimeout(r, 25));
}

(async () => {
  const m = mountAudit({ url: "http://127.0.0.1:8000/audit.html?mock=1" });
  await flush();
  const api = m.win.aimoAudit;
  assert.ok(api && typeof api.hashOf === "function");
  assert.ok(typeof api.verify === "function");
  assert.ok(typeof api.verifyCheckpoint === "function");

  const evs = payload.trail.events;
  assert.ok(evs.length >= 6);
  for (const e of evs) {
    const h = await api.hashOf(e);
    assert.strictEqual(h, e.hash, "hash mismatch at seq " + e.seq);
  }
  assert.strictEqual(await api.verify(evs), null);

  const tamperedWhy = JSON.parse(JSON.stringify(evs));
  tamperedWhy[1].why = "edited why";
  const bad1 = await api.verify(tamperedWhy);
  assert.strictEqual(bad1.at, 2);
  assert.strictEqual(bad1.why, "hash mismatch");

  const tamperedPrev = JSON.parse(JSON.stringify(evs));
  tamperedPrev[2].prev_hash = "f".repeat(64);
  tamperedPrev[2].hash = await api.hashOf(tamperedPrev[2]);
  const bad2 = await api.verify(tamperedPrev);
  assert.strictEqual(bad2.at, 3);
  assert.ok(bad2.why.includes("prev_hash"));

  const tamperedGen = JSON.parse(JSON.stringify(evs));
  tamperedGen[0].prev_hash = "1".repeat(64);
  tamperedGen[0].hash = await api.hashOf(tamperedGen[0]);
  const badGen = await api.verify([tamperedGen[0]]);
  assert.strictEqual(badGen.at, 1);
  assert.strictEqual(badGen.why, "genesis prev_hash");

  const cp0Res = await api.verifyCheckpoint(payload.cp0, payload.jwks, []);
  assert.strictEqual(cp0Res.ok, true);
  assert.strictEqual(cp0Res.head.seq, 0);

  const cp1Res = await api.verifyCheckpoint(payload.cp1, payload.jwks, evs);
  assert.strictEqual(cp1Res.ok, true);
  assert.strictEqual(cp1Res.head.seq, payload.cp1.seq);

  const cp2Res = await api.verifyCheckpoint(payload.cp2, payload.jwks, evs);
  assert.strictEqual(cp2Res.ok, true);
  assert.strictEqual(cp2Res.head.hash, payload.cp2.hash);

  const cp2StrRes = await api.verifyCheckpoint(payload.cp2.jws, payload.jwks);
  assert.strictEqual(cp2StrRes.ok, true);
  assert.strictEqual(cp2StrRes.head.seq, payload.cp2.seq);

  const parts = payload.cp2.jws.split(".");
  const flippedLast = parts[2].slice(0, -2) + (parts[2].slice(-2) === "AA" ? "BB" : "AA");
  const badSig = await api.verifyCheckpoint(parts[0] + "." + parts[1] + "." + flippedLast, payload.jwks);
  assert.strictEqual(badSig.ok, false);

  const truncSig = await api.verifyCheckpoint(parts[0] + "." + parts[1] + "." + parts[2].slice(0, 12), payload.jwks);
  assert.strictEqual(truncSig.ok, false);

  const fakePayload = Buffer.from(JSON.stringify({ seq: 999, hash: payload.cp2.hash, ts: payload.cp2.ts })).toString("base64url");
  const badPay = await api.verifyCheckpoint(parts[0] + "." + fakePayload + "." + parts[2], payload.jwks);
  assert.strictEqual(badPay.ok, false);

  const badMismatch = await api.verifyCheckpoint(Object.assign({}, payload.cp2, { hash: "0".repeat(64) }), payload.jwks);
  assert.strictEqual(badMismatch.ok, false);

  const badKid = await api.verifyCheckpoint(payload.cp2, { keys: [{ kty: "OKP", crv: "Ed25519", kid: "other-kid", x: payload.jwks.keys[0].x }] });
  assert.strictEqual(badKid.ok, false);

  const noEdCrypto = {
    subtle: {
      digest: globalThis.crypto.subtle.digest.bind(globalThis.crypto.subtle),
      async importKey() {
        const err = new Error("Ed25519 not supported");
        err.name = "NotSupportedError";
        throw err;
      },
      verify: globalThis.crypto.subtle.verify.bind(globalThis.crypto.subtle)
    }
  };
  const mFallback = mountAudit({ url: "http://127.0.0.1:8000/audit.html?mock=1", crypto: noEdCrypto });
  await flush();
  const fbRes = await mFallback.win.aimoAudit.verifyCheckpoint(payload.cp2, payload.jwks, evs);
  assert.strictEqual(fbRes.ok, null);
  assert.strictEqual(fbRes.unsupported, true);

  api.indexQuotes(evs);
  assert.strictEqual(api.orderOf(evs[0]), "ord_7f3a");
  assert.strictEqual(api.orderOf(evs[1]), "ord_7f3a");
  assert.strictEqual(api.orderOf(evs[2]), "ord_7f3a");
  assert.strictEqual(api.orderOf(evs[4]), "ord_8b21");
  assert.strictEqual(api.orderOf({ subject: "order:ord_legacy", detail: {} }), "ord_legacy");
  assert.strictEqual(api.orderOf(evs[5]), null);

  assert.strictEqual(m.els["badge"].className, "badge ok");
  assert.strictEqual(m.els["f-customer"].value, "hotel-aurora");
  m.els["f-order"].value = "ord_7f3a";
  m.els["f-order"].trigger("input");
  await flush();
  assert.strictEqual(m.els["receipt"].hidden, false);
  assert.ok(m.els["receipt"].innerHTML.includes("Laura Virtanen"));
  assert.ok(m.els["receipt"].innerHTML.includes("signature verified at placement"));
  m.els["f-order"].value = "";
  m.els["f-order"].trigger("input");
  await flush();
  assert.strictEqual(m.els["receipt"].hidden, true);

  const mBroken = mountAudit({ url: "http://127.0.0.1:8000/audit.html?mock=broken" });
  await flush();
  assert.strictEqual(mBroken.els["badge"].className, "badge stop");
  assert.ok(mBroken.els["badge"].innerHTML.includes("Chain broken at #5"));

  const mDeep = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?mock=1&order_id=ord_8b21",
    defaultCustomer: "hotel-aurora"
  });
  await flush();
  assert.strictEqual(mDeep.els["f-customer"].value, "");
  assert.strictEqual(mDeep.els["receipt"].hidden, false);
  assert.ok(mDeep.els["receipt"].innerHTML.includes("hospital-tays"));
  assert.ok(mDeep.els["ledger"].innerHTML.includes("q_8b20"));
  assert.ok(mDeep.els["ledger"].innerHTML.includes("ord_8b21"));

  const mCustParam = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?mock=1&customer=hospital-tays&order_id=ord_8b21",
    defaultCustomer: "hotel-aurora"
  });
  await flush();
  assert.strictEqual(mCustParam.els["f-customer"].value, "hospital-tays");

  const mLive = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?order_id=ord_8b21",
    defaultCustomer: "hotel-aurora",
    async api(path) {
      if (path.startsWith("/v1/audit")) return payload.trail;
      if (path === "/.well-known/jwks.json") return payload.jwks;
      if (path === "/v1/orders/ord_8b21") return {
        order_id: "ord_8b21",
        quote_id: "q_8b20",
        customer: "hospital-tays",
        agent_id: "buyer-tays",
        total_net: 450,
        status: "expired",
        delivery_date: "2026-11-16",
        decision: { outcome: "needs_approval", reasons: [{ code: "SUBSTITUTION", text: "Needs dietary check." }] },
        substitutions: [],
        lines: []
      };
      throw new Error("unexpected path: " + path);
    }
  });
  await flush();
  assert.strictEqual(mLive.els["f-customer"].value, "");
  assert.strictEqual(mLive.els["badge"].className, "badge ok");
  assert.ok(mLive.els["badge"].innerHTML.includes("Ed25519 verified"));
  assert.strictEqual(mLive.els["cp-box"].hidden, false);
  assert.strictEqual(mLive.els["cp-jws"].title, payload.cp2.jws);
  assert.strictEqual(mLive.els["receipt"].hidden, false);
  assert.ok(mLive.els["ledger"].innerHTML.includes("order.expired"));
  console.log("ALL_NODE_AUDIT_CHECKS_OK");
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_files_exist():
    for f in FILES:
        assert (WEB / f).stat().st_size > 0


def test_no_emoji_or_em_dash():
    for f in FILES:
        t = (WEB / f).read_text(encoding="utf-8")
        assert not EMOJI.search(t), f
        assert "\u2014" not in t, f
        assert "\u2013" not in t, f
        assert "gradient" not in t.lower(), f


def test_page_wiring():
    h = (WEB / "audit.html").read_text(encoding="utf-8")
    assert "/style.css" in h and "/shell.js" in h and 'aria-current="page">Audit' in h
    assert '<link rel="icon" href="data:image/svg+xml,' in h
    assert "/.well-known/jwks.json" in h
    assert "python -m aimo.audit verify-checkpoint" in h
    assert 'id="cp-box"' in h and 'id="cp-jws"' in h and 'id="cp-copy"' in h
    assert 'id="f-reset"' in h
    for src in re.findall(r'<script[^>]+src="([^"]+)"', h):
        assert src.startswith("/"), src
    for href in re.findall(r'<link[^>]+href="([^"]+)"', h):
        assert href.startswith("/") or href.startswith("data:image/svg+xml,"), href


def test_filter_reset_and_en_gb_currency_wiring():
    h = (WEB / "audit.html").read_text(encoding="utf-8")
    j = (WEB / "audit.js").read_text(encoding="utf-8")
    assert re.search(
        r'<button[^>]+type="button"[^>]+class="btn btn-quiet"[^>]+id="f-reset"[^>]*>Reset</button>',
        h,
    )
    assert 'Intl.NumberFormat("en-GB"' in j
    assert '"en-US"' not in j
    assert "resetFilters" in j
    assert "data-reset" in j
    assert "order.stockout" in j
    assert "order.expired" in j


def test_zero_comments_in_audit_owned_files():
    import io
    import tokenize

    h = (WEB / "audit.html").read_text(encoding="utf-8")
    assert "<" + "!--" not in h

    py_src = Path(__file__).read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(py_src).readline):
        assert tok.type != tokenize.COMMENT, f"Python comment at line {tok.start[0]}: {tok.string}"
    assert "\u2014" not in py_src
    assert not EMOJI.search(py_src)


def test_agents_legend_wired():
    h = (WEB / "audit.html").read_text(encoding="utf-8")
    j = (WEB / "audit.js").read_text(encoding="utf-8")
    assert 'id="legend"' in h and "function legend" in j and 'class="chip"' in j


def test_mock_v4_alignment_and_no_dead_verify_route():
    j = (WEB / "audit.js").read_text(encoding="utf-8")
    assert "/v1/audit/verify" not in j
    for token in [
        "parse.request",
        "offers.compared",
        "quote.issued",
        "quote.redeemed",
        "order.placed",
        "order.pending_approval",
        "approval.approved",
        "order.confirmed",
        "mandate_updated",
        "human:Laura Virtanen",
        "ord_7f3a",
        "q_91c2",
        "verifyCheckpoint",
    ]:
        assert token in j, token


def test_node_audit_parity_checkpoint_and_ui_flows(tmp_path, monkeypatch):
    import sys
    import warnings

    warnings.filterwarnings("ignore", message="EdDSA is deprecated.*")
    from fastapi.testclient import TestClient
    from aimo import audit, keys
    from aimo.app import app

    monkeypatch.setenv("AIMO_DB", str(tmp_path / "audit_parity.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    keys._cache.clear()

    cp0 = audit.checkpoint()
    audit.record(
        "hotel-aurora",
        "agent:buyer-aurora",
        "quote.issued",
        "q_91c2",
        "Laktoositon maito, kevytmaito 1 l.",
        {"quote_id": "q_91c2", "item": "Valio Eila\u00e4 \u00e4\u00e4rip\u00e4\u00e4 j\u00e4lkiruoka \u00f6\u00e5\u00c4\u00d6\u00c5", "total_gross": 1240.0, "unit": 12.5},
    )
    audit.record(
        "hotel-aurora",
        "agent:buyer-aurora",
        "quote.redeemed",
        "q_91c2",
        "Quote q_91c2 redeemed.",
        {"quote_id": "q_91c2", "total_net": 1087.72},
    )
    audit.record(
        "hotel-aurora",
        "agent:buyer-aurora",
        "order.placed",
        "ord_7f3a",
        "Under the 2,500 EUR per-order limit.",
        {"order_id": "ord_7f3a", "quote_id": "q_91c2", "n": {"b": [1, 2, 3.0, 3.75], "a": None, "flag": True}},
    )
    audit.record(
        "hospital-tays",
        "human:P\u00e4ivi Korhonen",
        "approval.approved",
        "ord_8b21",
        "Checked allergens, ok.",
        {"order_id": "ord_8b21", "note": "gluteeniton ja p\u00e4hkin\u00e4t\u00f6n"},
    )
    audit.record(
        "hospital-tays",
        "system",
        "order.expired",
        "ord_8b21",
        "The quote validity ended before the order was decided.",
        {"valid_until": "2026-11-14T14:00:00Z"},
    )
    cp1 = audit.checkpoint()
    keys.rotate()
    audit.record(
        "hotel-aurora",
        "human:Laura Virtanen",
        "mandate_updated",
        "hotel-aurora",
        "Mandate updated.",
        {"agent_id": "buyer-aurora", "per_order_eur": 2500},
    )

    with TestClient(app) as client:
        r_audit = client.get("/v1/audit", headers={"X-Admin-Key": "demo-admin"})
        assert r_audit.status_code == 200
        trail = r_audit.json()
        r_jwks = client.get("/.well-known/jwks.json")
        assert r_jwks.status_code == 200
        jwks = r_jwks.json()

    cp2 = trail["checkpoint"]
    assert trail["verified"] == {"ok": True, "bad_seq": None}
    assert audit.verify_checkpoint(cp1["jws"], jwks)["seq"] == 5
    assert audit.verify_checkpoint(cp2["jws"], jwks)["seq"] == 6

    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps(jwks), encoding="utf-8")
    cli_ok = subprocess.run(
        [sys.executable, "-m", "aimo.audit", "verify-checkpoint", cp2["jws"], str(jwks_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert cli_ok.returncode == 0
    assert f"OK seq={cp2['seq']} hash={cp2['hash']}" in cli_ok.stdout

    node_input = json.dumps(
        {
            "auditJsPath": str(WEB / "audit.js"),
            "trail": trail,
            "cp0": cp0,
            "cp1": cp1,
            "cp2": cp2,
            "jwks": jwks,
        }
    )
    res = subprocess.run(
        [_node_bin(), "-e", NODE_HARNESS],
        input=node_input,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert res.returncode == 0, f"Node parity test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "ALL_NODE_AUDIT_CHECKS_OK" in res.stdout


def test_hash_recipe_matches_python(tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    from aimo import audit

    monkeypatch.setenv("AIMO_DB", str(tmp_path / "a.db"))
    audit.record(
        "hotel-aurora",
        "agent:buyer",
        "quote.issued",
        "q1",
        "Laktoositon maito, kevytmaito 1 l.",
        {"quote_id": "q1", "item": "Valio Eila\u00e4 \u00e4\u00e4rip\u00e4\u00e4 j\u00e4lkiruoka", "total_gross": 1240.0, "unit": 12.5},
    )
    audit.record(
        "hotel-aurora",
        "agent:buyer",
        "order.placed",
        "o1",
        "Under the 2,000 EUR per-order limit.",
        {"order_id": "o1", "quote_id": "q1", "n": {"b": [1, 2], "a": None}},
    )
    audit.record("hospital-tays", "human:p\u00e4ivi", "approval.approved", "o2", "Checked allergens, ok.")
    evs = json.loads(json.dumps([e.model_dump(mode="json") for e in audit.events()]))
    assert audit.verify() == (True, None)

    import functools
    import http.server
    import threading

    cm = sync_api.sync_playwright()
    try:
        p = cm.__enter__()
    except Exception as exc:
        pytest.skip(f"playwright driver unavailable: {exc}")

    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB))
    h.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium not installed: {exc}")
        try:
            pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{srv.server_port}/audit.html?mock=1")
            pg.wait_for_function("window.aimoAudit")
            assert pg.evaluate("e => window.aimoAudit.verify(e)", evs) is None
            evs[1]["why"] = "edited"
            bad = pg.evaluate("e => window.aimoAudit.verify(e)", evs)
        finally:
            b.close()
    finally:
        srv.shutdown()
        srv.server_close()
        cm.__exit__(None, None, None)
    assert bad["at"] == 2


NODE_WAVE2_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const auditCode = fs.readFileSync(payload.auditJsPath, "utf8");

function mountAudit(opts) {
  const urlStr = opts.url || "http://127.0.0.1:8000/audit.html";
  const loc = new URL(urlStr);
  const els = {};
  function makeEl(id) {
    return {
      id: id,
      value: "",
      textContent: "",
      innerHTML: "",
      className: "",
      title: "",
      hidden: false,
      scrolled: null,
      dataset: {},
      attrs: {},
      listeners: {},
      parentNode: { dataset: {} },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] || null; },
      addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
      trigger(t, ev) {
        const e = Object.assign({ type: t, target: this, preventDefault() {}, closest() { return null; } }, ev || {});
        (this.listeners[t] || []).forEach(fn => fn.call(this, e));
      },
      scrollIntoView(arg) { this.scrolled = arg || true; },
      click() {},
      remove() {}
    };
  }
  function getEl(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  }
  [
    "f-order", "filters", "f-actor", "f-action", "f-customer", "f-reset", "legend",
    "v-ledger", "v-text", "export", "print", "ledger", "how-n", "badge",
    "plain", "receipt", "scope", "cp-box", "cp-seq", "cp-jws", "cp-copy", "jump"
  ].forEach(getEl);

  const winListeners = {};
  const aimo = {
    CUSTOMERS: ["hotel-aurora", "hospital-tays", "school-hervanta"],
    lastError: null,
    mockFlag: false,
    customer() { return opts.defaultCustomer || "hotel-aurora"; },
    mock(on) { this.mockFlag = !!on; },
    offline(el) { if (el) el.innerHTML = '<p class="empty muted">Gateway offline</p>'; return "Gateway offline"; },
    async api(path, reqOpts) {
      if (opts.api) return await opts.api(path, reqOpts);
      return null;
    }
  };
  const win = {
    aimo: aimo,
    addEventListener(t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); },
    dispatchCustomer(c) { (winListeners.customer || []).forEach(fn => fn({ detail: c })); },
    dispatchHash(h) {
      loc.hash = h;
      loc.href = loc.origin + loc.pathname + loc.search + h;
      (winListeners.hashchange || []).forEach(fn => fn({ type: "hashchange" }));
    },
    print() {}
  };
  const doc = {
    getElementById: getEl,
    createElement(tag) { return makeEl(tag); },
    body: { appendChild() {} }
  };
  const ctx = vm.createContext({
    window: win,
    document: doc,
    location: loc,
    history: {
      replaceState(_s, _t, u) {
        const nu = new URL(String(u));
        loc.href = nu.href;
        loc.search = nu.search;
        loc.hash = nu.hash;
      }
    },
    navigator: { clipboard: { writeText: async () => {} } },
    aimo: aimo,
    crypto: globalThis.crypto,
    TextEncoder: globalThis.TextEncoder,
    TextDecoder: globalThis.TextDecoder,
    URL: globalThis.URL,
    URLSearchParams: globalThis.URLSearchParams,
    Blob: globalThis.Blob,
    Intl: globalThis.Intl,
    atob: globalThis.atob,
    btoa: globalThis.btoa,
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    Date: globalThis.Date,
    Math: globalThis.Math,
    JSON: globalThis.JSON,
    Object: globalThis.Object,
    Array: globalThis.Array,
    String: globalThis.String,
    Number: globalThis.Number,
    Boolean: globalThis.Boolean,
    RegExp: globalThis.RegExp,
    Set: globalThis.Set,
    Uint8Array: globalThis.Uint8Array,
    Error: globalThis.Error,
    isNaN: globalThis.isNaN,
    encodeURIComponent: globalThis.encodeURIComponent
  });
  vm.runInContext(auditCode, ctx, { filename: "audit.js" });
  return { win, els, loc, aimo };
}

function flush() {
  return new Promise(r => setTimeout(r, 25));
}

function makeLiveApi(trailObj, ordersMap) {
  return async function(path) {
    if (path.startsWith("/v1/audit")) return trailObj;
    if (path === "/.well-known/jwks.json") return payload.jwks;
    if (path.startsWith("/v1/orders/")) {
      const id = decodeURIComponent(path.slice("/v1/orders/".length));
      return ordersMap[id] || null;
    }
    throw new Error("unexpected path: " + path);
  };
}

(async () => {
  const stockoutEv = payload.trail.events.find(e => e.action === "order.stockout");
  const expiredEv = payload.trail.events.find(e => e.action === "order.expired");
  const mandateEv = payload.trail.events.find(e => e.action === "mandate_updated" && e.customer === "school-hervanta");
  assert.ok(stockoutEv && expiredEv && mandateEv);

  const mSameCust = mountAudit({
    url: "http://127.0.0.1:8000/audit.html#e" + stockoutEv.seq,
    defaultCustomer: "hotel-aurora",
    api: makeLiveApi(payload.trail, {})
  });
  await flush();
  assert.strictEqual(mSameCust.els["f-customer"].value, "hotel-aurora");
  assert.ok(mSameCust.els["ledger"].innerHTML.includes('id="e' + stockoutEv.seq + '" data-open="true"'));
  assert.ok(mSameCust.els["ledger"].innerHTML.includes('aria-expanded="true" data-seq="' + stockoutEv.seq + '"'));
  assert.ok(mSameCust.els["e" + stockoutEv.seq].scrolled);

  assert.strictEqual(mSameCust.win.aimoAudit.eur(1250), "1,250.00 EUR");
  assert.strictEqual(mSameCust.win.aimoAudit.eur(0), "0.00 EUR");
  assert.strictEqual(mSameCust.win.aimoAudit.eur(null), "-");
  assert.strictEqual(mSameCust.win.aimoAudit.eur(undefined), "-");
  assert.strictEqual(mSameCust.win.aimoAudit.eur(""), "-");
  assert.strictEqual(mSameCust.win.aimoAudit.eur(NaN), "-");

  const mCrossCust = mountAudit({
    url: "http://127.0.0.1:8000/audit.html#e" + mandateEv.seq,
    defaultCustomer: "hotel-aurora",
    api: makeLiveApi(payload.trail, {})
  });
  await flush();
  assert.strictEqual(mCrossCust.els["f-customer"].value, "");
  assert.ok(mCrossCust.els["ledger"].innerHTML.includes('id="e' + mandateEv.seq + '" data-open="true"'));
  assert.ok(mCrossCust.els["e" + mandateEv.seq].scrolled);

  const mCrossExplicit = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?customer=hotel-aurora#e" + expiredEv.seq,
    defaultCustomer: "hotel-aurora",
    api: makeLiveApi(payload.trail, {})
  });
  await flush();
  assert.strictEqual(mCrossExplicit.els["f-customer"].value, "");
  assert.ok(mCrossExplicit.els["ledger"].innerHTML.includes('id="e' + expiredEv.seq + '" data-open="true"'));
  assert.ok(mCrossExplicit.els["e" + expiredEv.seq].scrolled);

  mCrossExplicit.els["f-customer"].value = "school-hervanta";
  mCrossExplicit.els["f-customer"].trigger("change");
  mCrossExplicit.win.dispatchHash("#e" + stockoutEv.seq);
  assert.strictEqual(mCrossExplicit.els["f-customer"].value, "");
  assert.ok(mCrossExplicit.els["ledger"].innerHTML.includes('id="e' + stockoutEv.seq + '" data-open="true"'));
  assert.ok(mCrossExplicit.els["e" + stockoutEv.seq].scrolled);

  const oidExp = payload.orderExpired.order_id;
  const oidSo = payload.orderStockoutPending.order_id;
  const mReset = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?customer=hospital-tays&order_id=" + oidExp + "#e" + expiredEv.seq,
    defaultCustomer: "hotel-aurora",
    api: makeLiveApi(payload.trail, { [oidExp]: payload.orderExpired })
  });
  await flush();
  assert.strictEqual(mReset.els["receipt"].hidden, false);
  const chipStub = {
    dataset: { id: "buyer-tays" },
    getAttribute(k) { return k === "aria-pressed" ? "false" : null; }
  };
  mReset.els["legend"].trigger("click", { target: { closest(sel) { return sel === ".chip" ? chipStub : null; } } });
  mReset.els["f-action"].value = "quote.issued";
  mReset.els["f-action"].trigger("change");
  assert.strictEqual(mReset.els["f-actor"].value, "agent");
  assert.ok(mReset.els["legend"].innerHTML.includes('data-id="buyer-tays" aria-pressed="true"'));

  mReset.els["f-reset"].trigger("click");
  await flush();
  assert.strictEqual(mReset.els["f-customer"].value, "");
  assert.strictEqual(mReset.els["f-order"].value, "");
  assert.strictEqual(mReset.els["f-actor"].value, "");
  assert.strictEqual(mReset.els["f-action"].value, "");
  assert.strictEqual(mReset.els["receipt"].hidden, true);
  assert.strictEqual(mReset.loc.search, "");
  assert.strictEqual(mReset.loc.hash, "");
  assert.ok(mReset.els["legend"].innerHTML.includes('data-id="buyer-tays" aria-pressed="false"'));
  assert.ok(mReset.els["scope"].textContent.includes("all customers"));
  assert.ok(mReset.els["scope"].textContent.includes("showing " + payload.trail.events.length + " of " + payload.trail.events.length + " events"));

  mReset.els["f-order"].value = "ord_does_not_exist";
  mReset.els["f-order"].trigger("input");
  await flush();
  assert.ok(mReset.els["ledger"].innerHTML.includes("No audit events match these filters."));
  assert.ok(mReset.els["ledger"].innerHTML.includes('data-reset="1"'));
  mReset.els["ledger"].trigger("click", {
    target: { closest(sel) { return sel === "[data-reset]" ? { dataset: { reset: "1" } } : null; } }
  });
  await flush();
  assert.strictEqual(mReset.els["f-order"].value, "");
  assert.strictEqual(mReset.els["receipt"].hidden, true);
  assert.strictEqual(mReset.loc.search, "");
  assert.ok(mReset.els["scope"].textContent.includes("showing " + payload.trail.events.length + " of " + payload.trail.events.length + " events"));

  const mSoPending = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?order_id=" + oidSo,
    defaultCustomer: "school-hervanta",
    api: makeLiveApi(payload.trailMid, { [oidSo]: payload.orderStockoutPending })
  });
  await flush();
  const rcSoPending = mSoPending.els["receipt"].innerHTML;
  assert.strictEqual(mSoPending.els["receipt"].hidden, false);
  assert.ok(rcSoPending.includes("Stock-out"));
  assert.ok(rcSoPending.includes("stock-out recovery"));
  assert.ok(rcSoPending.includes("1001"));
  assert.ok(rcSoPending.includes("1002"));
  assert.ok(rcSoPending.includes("1,250.00 EUR"));
  assert.ok(rcSoPending.includes("<code>STOCKOUT</code>"));
  assert.ok(rcSoPending.includes("Stock dropped after the order"));
  assert.ok(rcSoPending.includes("Awaiting a human decision"));
  const soMidEv = payload.trailMid.events.find(e => e.action === "order.stockout");
  assert.ok(rcSoPending.includes('href="#e' + soMidEv.seq + '"'));
  mSoPending.els["receipt"].trigger("click", {
    target: {
      closest(sel) {
        return sel === "[data-seq-jump]" ? { dataset: { seqJump: String(soMidEv.seq) }, getAttribute() { return String(soMidEv.seq); } } : null;
      }
    }
  });
  assert.ok(mSoPending.els["ledger"].innerHTML.includes('id="e' + soMidEv.seq + '" data-open="true"'));
  assert.ok(mSoPending.els["e" + soMidEv.seq].scrolled);

  const mSoRecovered = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?order_id=" + oidSo,
    defaultCustomer: "hotel-aurora",
    api: makeLiveApi(payload.trail, { [oidSo]: payload.orderStockoutRecovered })
  });
  await flush();
  const rcSoRec = mSoRecovered.els["receipt"].innerHTML;
  assert.ok(rcSoRec.includes("Stock-out"));
  assert.ok(rcSoRec.includes("recovered"));
  assert.ok(rcSoRec.includes("stock-out recovery"));
  assert.ok(rcSoRec.includes("Laura Virtanen"));
  assert.ok(rcSoRec.includes("Approved Eila substitute after stock-out"));
  assert.ok(rcSoRec.includes("1,250.00 EUR"));

  const mExp = mountAudit({
    url: "http://127.0.0.1:8000/audit.html?order_id=" + oidExp,
    defaultCustomer: "hotel-aurora",
    api: makeLiveApi(payload.trail, { [oidExp]: payload.orderExpired })
  });
  await flush();
  const rcExp = mExp.els["receipt"].innerHTML;
  assert.strictEqual(mExp.els["receipt"].hidden, false);
  assert.ok(rcExp.includes("Expiry"));
  assert.ok(rcExp.includes("order.expired"));
  assert.ok(rcExp.includes("valid until"));
  assert.ok(rcExp.includes("The quote validity ended before the order was decided."));
  assert.ok(rcExp.includes("Expired before human decision"));
  assert.ok(!rcExp.includes("Awaiting a human decision"));
  assert.ok(rcExp.includes("validity expired"));
  assert.ok(rcExp.includes("2,500.00 EUR"));
  assert.ok(rcExp.includes('href="#e' + expiredEv.seq + '"'));

  console.log("ALL_WAVE2_NODE_AUDIT_CHECKS_OK");
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_node_hash_deeplink_filter_reset_and_stockout_expired_receipts(tmp_path, monkeypatch):
    from datetime import date, datetime, timedelta, timezone
    import warnings

    warnings.filterwarnings("ignore", message="EdDSA is deprecated.*")
    from fastapi.testclient import TestClient
    from aimo import allocate, catalog, keys, offers as offers_mod, orders as orders_mod, substitute, valio
    from aimo.app import app
    from aimo.models import Constraint, NeedItem, Product, UnitSize

    empty_data = tmp_path / "empty_data"
    empty_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "wave2_audit.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(empty_data))
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(keys, "_cache", {})
    monkeypatch.setattr(offers_mod, "_overrides", {})
    monkeypatch.setattr(allocate, "_data", lambda: {})

    items = {
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
            "stock": 3000.0,
            "reliability": 0.99,
            "co2": 1.15,
        },
        "1002": {
            "product": Product(
                product_id="1002",
                name="Valio Eila Kevytmaito laktoositon 1 L",
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
            "stock": 3000.0,
            "reliability": 0.97,
            "co2": 1.20,
        },
    }

    def fake_search(q: str = "", constraints: Constraint | None = None, limit: int = 50):
        c = constraints or Constraint()
        probe = NeedItem(query=q or "", quantity=1.0, unit="pcs", constraints=c)
        scored = []
        for pid, entry in items.items():
            prod = entry["product"]
            score = substitute.match_score(probe, prod) if (q or "").strip() else 1.0
            if score <= 0:
                score = 0.6
            scored.append((-score, pid, prod))
        scored.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in scored[:limit]]

    monkeypatch.setattr(catalog, "search", fake_search)
    monkeypatch.setattr(catalog, "get", lambda pid: items[str(pid)]["product"] if str(pid) in items else None)
    monkeypatch.setattr(catalog, "reliability", lambda pid: items[str(pid)]["reliability"] if str(pid) in items else None)
    monkeypatch.setattr(catalog, "co2_per_kg", lambda pid: items[str(pid)]["co2"] if str(pid) in items else None)
    monkeypatch.setattr(catalog, "raw", lambda pid: None)
    monkeypatch.setattr(catalog, "names", lambda pid: {})
    monkeypatch.setattr(
        valio,
        "price",
        lambda customer, day, ids: {
            str(pid): {
                "productId": str(pid),
                "currency": "EUR",
                "catalogPrice": {"value": items[str(pid)]["net"]},
                "priceWithTax": {"value": items[str(pid)]["gross"]},
            }
            for pid in ids
            if str(pid) in items
        },
    )
    monkeypatch.setattr(
        valio,
        "availability",
        lambda customer, day, ids: {
            str(pid): {
                "productId": str(pid),
                "closingTime": "2026-11-12T23:59:59+02:00",
                "salesBlock": False,
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": items[str(pid)]["stock"],
                        "availableDate": day.isoformat(),
                        "salesUnit": items[str(pid)]["product"].sales_unit,
                    }
                ],
            }
            for pid in ids
            if str(pid) in items
        },
    )

    delivery = (date.today() + timedelta(days=3)).isoformat()
    hotel_h = {"X-Agent-Key": "demo-hotel-aurora"}
    tays_h = {"X-Agent-Key": "demo-hospital-tays"}
    admin_h = {"X-Admin-Key": "demo-admin"}

    with TestClient(app) as client:
        q1 = client.post(
            "/v1/quotes",
            headers=hotel_h,
            json={"offers": [{"product_id": "1001", "quantity": 1000.0}], "delivery_date": delivery},
        ).json()
        o1 = client.post("/v1/orders", headers=hotel_h, json={"quote_id": q1["quote_id"], "jws": q1["jws"]}).json()
        assert o1["status"] == "confirmed"
        oid_so = o1["order_id"]

        sim = client.post(
            "/v1/admin/simulate",
            headers=admin_h,
            json={"product_id": "1001", "available_amount": 0},
        )
        assert sim.status_code == 200
        order_so_pending = client.get(f"/v1/orders/{oid_so}", headers=admin_h).json()
        assert order_so_pending["status"] == "pending_approval"
        assert order_so_pending["total_net"] == 1250.0
        trail_mid = client.get("/v1/audit", headers=admin_h).json()

        appr = client.post(
            f"/v1/approvals/{oid_so}",
            headers=admin_h,
            json={"approve": True, "by": "Laura Virtanen", "note": "Approved Eila substitute after stock-out"},
        )
        assert appr.status_code == 200
        order_so_recovered = client.get(f"/v1/orders/{oid_so}", headers=admin_h).json()
        assert order_so_recovered["status"] == "confirmed"

        client.delete("/v1/admin/simulate", headers=admin_h)

        q2 = client.post(
            "/v1/quotes",
            headers=tays_h,
            json={"offers": [{"product_id": "1002", "quantity": 2000.0}], "delivery_date": delivery},
        ).json()
        o2 = client.post("/v1/orders", headers=tays_h, json={"quote_id": q2["quote_id"], "jws": q2["jws"]}).json()
        assert o2["status"] == "pending_approval"
        oid_exp = o2["order_id"]

        past_valid = datetime.fromisoformat(o2["valid_until"]) + timedelta(hours=2)
        if past_valid.tzinfo is None:
            past_valid = past_valid.replace(tzinfo=timezone.utc)
        monkeypatch.setattr(orders_mod, "_now", lambda: past_valid)
        order_expired = client.get(f"/v1/orders/{oid_exp}", headers=admin_h).json()
        assert order_expired["status"] == "expired"
        monkeypatch.setattr(orders_mod, "_now", lambda: datetime.now(timezone.utc))

        school_mand = client.get("/v1/mandate?customer=school-hervanta", headers=admin_h).json()
        put_mand = client.put("/v1/mandate", headers=admin_h, json={**school_mand, "per_order_eur": 750.0})
        assert put_mand.status_code == 200

        trail = client.get("/v1/audit", headers=admin_h).json()
        assert trail["verified"] == {"ok": True, "bad_seq": None}
        jwks = client.get("/.well-known/jwks.json").json()

    node_input = json.dumps(
        {
            "auditJsPath": str(WEB / "audit.js"),
            "trailMid": trail_mid,
            "trail": trail,
            "jwks": jwks,
            "orderStockoutPending": order_so_pending,
            "orderStockoutRecovered": order_so_recovered,
            "orderExpired": order_expired,
        }
    )
    res = subprocess.run(
        [_node_bin(), "-e", NODE_WAVE2_HARNESS],
        input=node_input,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert res.returncode == 0, f"Wave 2 Node audit test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "ALL_WAVE2_NODE_AUDIT_CHECKS_OK" in res.stdout
