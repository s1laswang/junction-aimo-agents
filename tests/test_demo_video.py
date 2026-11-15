from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

SHARED_PW = Path("/tmp/fleet-root/shared/playwright")
if SHARED_PW.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SHARED_PW))

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"


def _synthetic_catalog_products() -> list[dict[str, Any]]:
    def _item(
        pid: str,
        sales: str,
        name_en: str,
        brand: str,
        category: str,
        units: list[tuple[str, int]],
        conv: list[dict[str, Any]],
        claims: list[str],
        allergens: list[tuple[str, str]],
        fat: float,
        marketing: str,
        ingredients: str,
    ) -> dict[str, Any]:
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": "ST",
            "category": category,
            "vendorName": brand,
            "allowedLotSize": 0.01,
            "countryOfOrigin": "FI",
            "units": [{"unitId": u, "sizeInBaseUnits": s} for u, s in units],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": [
                    {
                        "name": "allergen",
                        "type": "ENUM",
                        "values": [{"id": code, "unit": st} for code, st in allergens],
                    },
                    {
                        "name": "nutritionalClaim",
                        "type": "ENUM",
                        "values": [{"id": cl, "unit": "FREE_FROM"} for cl in claims],
                    },
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": fat}]},
                ],
                "keyIngredients": [{"language": "en", "value": ingredients}],
                "marketingTexts": [{"language": "en", "value": marketing}],
            },
        }

    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    base_10l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 10.0}}]
    return [
        _item(
            "463038",
            "KI",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            "10400",
            [("KI", 2), ("ST", 1)],
            base_10l,
            ["FREE_FROM_LACTOSE"],
            [("AM", "CONTAINS")],
            1.5,
            "Valio Eila lactose free dispenser milk drink for hotels and restaurants.",
            "milk, vitamin D",
        ),
        _item(
            "464306",
            "ST",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            "10400",
            [("ST", 1)],
            base_1l,
            ["FREE_FROM_LACTOSE"],
            [("AM", "CONTAINS")],
            1.5,
            "Valio Eila semi skimmed milk drink is lactose free and suitable for professional kitchens.",
            "milk, vitamin D",
        ),
        _item(
            "469363",
            "ST",
            "Arla Semi skimmed milkdrink 1 L UHT lactose free",
            "Arla",
            "10400",
            [("ST", 1)],
            base_1l,
            ["FREE_FROM_LACTOSE"],
            [("AM", "CONTAINS")],
            1.5,
            "Arla lactose free semi skimmed milk drink 1 L UHT.",
            "milk, vitamin D",
        ),
        _item(
            "462775",
            "ST",
            "Valio Eila whole milk drink 1 l lactose free",
            "Valio",
            "10400",
            [("ST", 1)],
            base_1l,
            ["FREE_FROM_LACTOSE"],
            [("AM", "CONTAINS")],
            3.5,
            "Valio Eila lactose free whole milk drink 1 L.",
            "milk, vitamin D",
        ),
    ]


class DemoFixtureClient:
    base_url = "fixture://valio-demo-live"
    ttl_s = 300

    def __init__(self) -> None:
        self.price_raw = json.loads((FIX / "price_20261116.json").read_text(encoding="utf-8"))
        self.avail_raw = json.loads((FIX / "availability_20261116.json").read_text(encoding="utf-8"))
        self.stats = {"upstream_calls": 0}
        self._lock = threading.Lock()
        self._price_overrides: dict[str, float] = {}

    def set_price(self, product_id: str, unit_price: float) -> None:
        with self._lock:
            self._price_overrides[str(product_id)] = float(unit_price)

    def price(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        req_str = requested_date.isoformat() if isinstance(requested_date, date) else str(requested_date)[:10]
        by_id = {str(p["productId"]): p for p in self.price_raw["products"]}
        with self._lock:
            overrides = dict(self._price_overrides)
        products: list[dict[str, Any]] = []
        for pid in ids:
            spid = str(pid)
            if spid in by_id:
                item = copy.deepcopy(by_id[spid])
            else:
                item = {
                    "productId": spid,
                    "quantity": 1.0,
                    "catalogPrice": {
                        "value": 4.50,
                        "valueWithTax": 5.13,
                        "validFrom": "2026-01-01T00:00:00+0200",
                        "validTo": "2099-12-31T00:00:00+0200",
                        "currency": "EUR",
                    },
                    "priceWithTax": {
                        "value": 5.13,
                        "validFrom": "2026-01-01T00:00:00+0200",
                        "validTo": "2099-12-31T00:00:00+0200",
                        "currency": "EUR",
                    },
                    "comparisonPrice": {
                        "value": 4.50,
                        "valueWithTax": 5.13,
                        "validFrom": "2026-01-01T00:00:00+0200",
                        "validTo": "2099-12-31T00:00:00+0200",
                        "currency": "EUR",
                    },
                    "comparisonUnit": "L",
                    "tax": {"taxRate": 14.0, "taxValue": 0.63},
                    "netPriceFound": True,
                    "error": False,
                }
            for key in ("catalogPrice", "priceWithTax", "comparisonPrice", "listPrice"):
                if isinstance(item.get(key), dict):
                    item[key]["validTo"] = "2099-12-31T00:00:00+0200"
            if spid in overrides:
                new_val = overrides[spid]
                gross = round(new_val * 1.135, 2)
                item["catalogPrice"]["value"] = new_val
                item["catalogPrice"]["valueWithTax"] = gross
                if isinstance(item.get("priceWithTax"), dict):
                    item["priceWithTax"]["value"] = gross
                if isinstance(item.get("comparisonPrice"), dict):
                    item["comparisonPrice"]["value"] = new_val
                    item["comparisonPrice"]["valueWithTax"] = gross
            meta = {"customerId": customer or "123", "currency": "EUR", "pricingDate": f"{req_str}T00:00:00+0200"}
            products.append({**item, "_fetched_at": now, "_meta": meta})
        self.stats["upstream_calls"] += 1
        return {
            "customerId": customer or "123",
            "currency": "EUR",
            "pricingDate": f"{req_str}T00:00:00+0200",
            "products": products,
            "_oldest_fetch": now,
        }

    def availability(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        now_dt = datetime.now(timezone.utc)
        req_str = requested_date.isoformat() if isinstance(requested_date, date) else str(requested_date)[:10]
        by_id = {str(p["productId"]): p for p in self.avail_raw["products"]}
        meta = {"timeNow": now_dt.isoformat(), "requestedDate": f"{req_str}T00:00:00+0200"}
        products: list[dict[str, Any]] = []
        for pid in ids:
            spid = str(pid)
            if spid in by_id:
                item = copy.deepcopy(by_id[spid])
            else:
                item = {
                    "productId": spid,
                    "closingTime": (now_dt + timedelta(hours=48)).isoformat(),
                    "salesBlock": False,
                    "logisticModel": "Stock",
                    "scheduleLines": [
                        {
                            "salesBlock": False,
                            "availableAmount": 5000.0,
                            "availableDate": req_str,
                            "salesUnit": "ST",
                        }
                    ],
                }
            if spid == "463038":
                item["closingTime"] = (now_dt - timedelta(days=2)).isoformat()
            else:
                item["closingTime"] = (now_dt + timedelta(hours=48)).isoformat()
            for sl in item.get("scheduleLines") or []:
                sl["availableDate"] = req_str
            products.append({**item, "_fetched_at": now, "_meta": meta})
        self.stats["upstream_calls"] += 1
        return {**meta, "products": products, "_oldest_fetch": now}


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _live_demo_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from aimo import config
    from aimo.backend import catalog_store
    from aimo.backend import client as bc
    from aimo.backend import orders as store
    from aimo.buyer import runs
    from aimo.catalog import taxonomy, textsearch
    from aimo.gateway import human_auth, resilience
    from aimo.gateway.app import create_app

    data_dir = tmp_path / "aimo_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pjson = data_dir / "valio_aimo_junction26_product_data.json"
    rel_csv = data_dir / "valio_aimo_junction26_delivery_reliability.csv"
    co2_csv = data_dir / "valio_aimo_junction26_carbon_footprint.csv"
    cdb = data_dir / "catalog.sqlite"
    sdb = data_dir / "state.sqlite"
    kdir = data_dir / "keys"

    pjson.write_text(json.dumps(_synthetic_catalog_products()), encoding="utf-8")
    rel_csv.write_text("product_id,reliability\n463038,0.99\n464306,0.98\n469363,0.95\n462775,0.97\n", encoding="utf-8")
    co2_csv.write_text("product_id,co2\n463038,1.02\n464306,1.05\n469363,1.12\n462775,1.18\n", encoding="utf-8")

    for var in (
        "AIMO_APPROVER_PIN",
        "AIMO_APPROVER_PINS",
        "AIMO_FAULTS",
        "AIMO_ENV",
        "AIMO_NOW",
        "AIMO_NOW_FROZEN",
        "AIMO_REPLAY",
        "AIMO_REPLAY_FALLBACK",
        "AIMO_MCP_URL",
        "AIMO_BUYER_TRANSPORT",
        "AIMO_MCP_ALLOWED_HOSTS",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AIMO_LLM_MODE",
        "AIMO_SCREEN_GEMINI",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("AIMO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIMO_STATE_DB", str(sdb))
    monkeypatch.setenv("AIMO_KEY_DIR", str(kdir))
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(config, "CO2_CSV", co2_csv)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "STATE_DB", sdb)
    monkeypatch.setattr(config, "KEY_DIR", kdir)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", True)

    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    monkeypatch.setattr(catalog_store, "CO2_CSV", co2_csv)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(taxonomy, "TAX_JSON", data_dir / "cache" / "taxonomy.json")
    monkeypatch.setattr(store, "DB_PATH", sdb)

    gemini_calls: list[Any] = []

    def _forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        gemini_calls.append((args, kwargs))
        raise AssertionError("Gemini must not be called in keyless E2E UI test")

    monkeypatch.setattr("aimo.gemini.generate", _forbid_gemini)
    monkeypatch.setattr("aimo.gemini.client", _forbid_gemini)

    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    taxonomy.labels_for.cache_clear()
    human_auth.reset_for_tests()
    for brk in list(resilience._breakers.values()):
        brk.reset()

    fresh_registry = runs.RunRegistry()
    monkeypatch.setattr(runs, "registry", fresh_registry)

    fixture_client = DemoFixtureClient()
    bc.set_client(fixture_client)
    catalog_store.load_catalog(cdb, force=True)
    human_auth.approver_pins()

    port = _allocate_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(with_mcp=True)
    uv_cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(uv_cfg)
    thread = threading.Thread(target=server.run, daemon=True, name=f"uvicorn-demo-{port}")
    thread.start()

    deadline = time.time() + 15.0
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    assert ready, "uvicorn server failed to start on ephemeral port"

    try:
        yield {
            "base_url": base_url,
            "fixture_client": fixture_client,
            "gemini_calls": gemini_calls,
            "registry": fresh_registry,
            "human_auth": human_auth,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=8.0)
        bc.set_client(None)
        human_auth.reset_for_tests()
        with store._lock:
            con = store._conns.pop(str(sdb), None)
            if con is not None:
                with contextlib.suppress(Exception):
                    con.close()
        if textsearch._con.cache_info().currsize:
            with contextlib.suppress(Exception):
                textsearch._con().close()
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()
        taxonomy.labels_for.cache_clear()


def _attach_offline_font_routes(page: Any, base_url: str) -> None:
    def _handle(route: Any) -> None:
        url = route.request.url
        if url.startswith(base_url) or url.startswith("data:") or url.startswith("blob:"):
            route.continue_()
        elif "fonts.googleapis.com" in url:
            route.fulfill(status=200, content_type="text/css", body="")
        else:
            route.abort()

    page.route("**/*", _handle)


def _probe_mp4(mp4_path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(mp4_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_live_playwright_e2e_demo_beats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import expect, sync_playwright

    with _live_demo_env(tmp_path, monkeypatch) as env:
        base_url = env["base_url"]
        fixture_client: DemoFixtureClient = env["fixture_client"]
        gemini_calls: list[Any] = env["gemini_calls"]
        human_auth = env["human_auth"]

        delivery_monday = (date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)).isoformat()
        inj_plan = httpx.post(
            f"{base_url}/v1/plan",
            headers={
                "X-Customer-Id": "hotel-aurora",
                "X-Agent-Id": "buyer-1",
                "X-Aimo-Faults": "inject_text:464306",
            },
            json={
                "customer_id": "hotel-aurora",
                "product_query": "lactose-free milk",
                "required_claims": ["FREE_FROM_LACTOSE"],
                "quantity": 1000,
                "unit": "L",
                "delivery_by": delivery_monday,
                "preferred_product_ids": ["463038"],
            },
            timeout=10.0,
        )
        assert inj_plan.status_code == 200, inj_plan.text
        inj_json = inj_plan.json()
        inj_line = next((ln for ln in inj_json["lines"] if ln["product_id"] == "464306"), None)
        assert inj_line is not None
        assert inj_line["injection_screen"]["suspicious"] is True
        assert "supplier_text_injection_suspected" in inj_line["flags"]

        page_errors: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            _attach_offline_font_routes(page, base_url)

            page.goto(f"{base_url}/ui/?present=1", wait_until="domcontentloaded")

            expect(page.locator("#mode-pill")).to_contain_text("LIVE", timeout=10000)
            expect(page.locator("#customer")).to_have_value("hotel-aurora", timeout=10000)
            expect(page.locator("#btn-run")).to_be_enabled(timeout=10000)

            page.locator("#btn-run").click()
            expect(page.locator("#timeline .tl-item.k-wait")).to_be_visible(timeout=20000)
            expect(page.locator("#request-card")).to_contain_text("Awaiting approval", timeout=10000)

            page.wait_for_timeout(1100)
            page.locator("#tab-plan").click()
            expect(page.locator("#view-plan")).to_be_visible()
            expect(page.locator("#view-plan")).to_contain_text("Order lines the agent proposes", timeout=10000)
            expect(page.locator("#view-plan table.t")).to_contain_text("464306")
            expect(page.locator("#view-plan table.t .plan-screen.ok").first).to_contain_text(
                "Prompt-injection screen: clean",
                timeout=10000,
            )
            expect(page.locator("#view-plan article.cand.in-plan")).to_contain_text("Valio Eila")

            agent_headers = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
            pending_resp = httpx.get(f"{base_url}/v1/approvals?status=pending", headers=agent_headers, timeout=10.0)
            assert pending_resp.status_code == 200, pending_resp.text
            pending_items = pending_resp.json()
            assert len(pending_items) == 1
            order_id = pending_items[0]["order"]["order_id"]
            old_quote_hash = pending_items[0]["order"]["quote_hash"]

            attack_resp = httpx.post(
                f"{base_url}/v1/approvals/{order_id}/decision",
                headers=agent_headers,
                json={
                    "decision": "approve",
                    "approver": "anna@hotel-aurora.example",
                    "approver_role": "procurement_manager",
                    "quote_hash": old_quote_hash,
                    "note": "self-approve attempt by buyer-1",
                },
                timeout=10.0,
            )
            assert attack_resp.status_code == 403, attack_resp.text
            assert attack_resp.json().get("error") == "agent_cannot_approve"

            audit_after_attack = httpx.get(
                f"{base_url}/v1/audit",
                params={"subject_id": order_id},
                timeout=10.0,
            ).json()
            assert any(e["action"] == "approval.refused" for e in audit_after_attack)

            page.locator("#tab-ops").click()
            expect(page.locator("#blocked-pill")).to_be_visible(timeout=10000)
            expect(page.locator("#blocked-pill")).to_contain_text("1 attack blocked", timeout=10000)
            expect(page.locator("#view-ops")).to_contain_text("Blocked attacks", timeout=10000)
            expect(page.locator("#view-ops")).to_contain_text("Approval refused", timeout=10000)
            expect(page.locator("#view-ops")).to_contain_text("approval.refused", timeout=10000)

            page.locator("#tab-approvals").click()
            expect(page.locator("#view-approvals .signin-card")).to_be_visible(timeout=10000)
            page.locator("#view-approvals .signin-card button").click()
            expect(page.locator("#signin-dialog")).to_be_visible(timeout=5000)

            who_field = page.locator("#signin-who")
            if who_field.evaluate("el => el.tagName") == "SELECT":
                who_field.select_option("__other")
            anna_pin = human_auth.local_pin("anna@hotel-aurora.example")
            assert len(anna_pin) >= 12
            page.locator("#signin-who").fill("anna@hotel-aurora.example")
            page.locator("#signin-pin").fill(anna_pin)
            page.locator("#signin-dialog button[type='submit']").click()

            expect(page.locator("#approver-chip")).to_contain_text("Anna Virtanen", timeout=10000)
            expect(page.locator("#view-approvals")).to_contain_text("Signature verified", timeout=10000)
            approve_btn = page.locator("#view-approvals .decide-actions button.btn-ok")
            expect(approve_btn).to_be_enabled(timeout=10000)

            fixture_client.set_price("464306", 1.26)
            expired_clock = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
            monkeypatch.setenv("AIMO_NOW", expired_clock)
            monkeypatch.setenv("AIMO_NOW_FROZEN", "1")

            with page.expect_response(
                lambda r: f"/v1/approvals/{order_id}/decision" in r.url and r.request.method == "POST",
                timeout=10000,
            ) as requote_resp_info:
                approve_btn.click()

            requote_http = requote_resp_info.value
            assert requote_http.status == 409
            requote_body = requote_http.json()
            assert requote_body.get("error") == "requote_needed"
            detail = requote_body.get("detail") or {}
            assert detail.get("new_quote") and detail.get("new_order")
            new_order_id = detail["new_order"]["order_id"]
            assert new_order_id != order_id
            assert detail["new_quote"]["total_net"] == pytest.approx(1260.0)

            expect(page.locator("#view-approvals .card.requote")).to_be_visible(timeout=10000)
            expect(page.locator("#view-approvals .card.requote")).to_contain_text("Prices changed: re-quote needed")
            expect(page.locator("#timeline")).to_contain_text("Order re-quoted", timeout=10000)

            requote_approve_btn = page.locator(
                "#view-approvals .card.requote button.btn-ok, #view-approvals .decide-actions button.btn-ok"
            ).first
            expect(requote_approve_btn).to_be_enabled(timeout=10000)
            with page.expect_response(
                lambda r: f"/v1/approvals/{new_order_id}/decision" in r.url and r.request.method == "POST",
                timeout=10000,
            ) as placed_resp_info:
                requote_approve_btn.click()

            placed_http = placed_resp_info.value
            assert placed_http.status == 200
            placed_order = placed_http.json()
            assert placed_order["status"] == "placed"
            assert placed_order["response_code"] == "AP"

            expect(page.locator("#timeline .tl-item.k-final")).to_be_visible(timeout=10000)

            expect(page.locator("#order-response")).to_be_visible(timeout=10000)
            expect(page.locator("#order-response")).to_contain_text("Placed", timeout=10000)
            expect(page.locator("#order-response")).to_contain_text("AP", timeout=10000)

            peppol_resp = httpx.get(f"{base_url}/v1/orders/{new_order_id}/peppol-order-response.xml", timeout=10.0)
            assert peppol_resp.status_code == 200, peppol_resp.text
            assert "OrderResponse" in peppol_resp.text
            assert ">AP<" in peppol_resp.text
            assert new_order_id in peppol_resp.text

            audit_all = httpx.get(f"{base_url}/v1/audit", timeout=10.0).json()
            audit_actions = {e["action"] for e in audit_all}
            assert "order.requoted" in audit_actions
            assert "order.approved" in audit_actions
            assert "order.placed" in audit_actions

            page.locator("#tab-audit").click()
            expect(page.locator("#view-audit")).to_be_visible(timeout=10000)
            page.locator("#view-audit button", has_text="Whole ledger").click()
            expect(page.locator("#view-audit ol.chain li")).not_to_have_count(0, timeout=10000)

            page.locator("#view-audit .audit-tools button", has_text="Verify chain").click()
            expect(page.locator("#view-audit .verify-result.ok")).to_contain_text("Chain intact", timeout=10000)
            verify_ok = httpx.get(f"{base_url}/v1/audit/verify", timeout=10.0).json()
            assert verify_ok["ok"] is True
            report_md = httpx.get(f"{base_url}/v1/audit/report/{new_order_id}", timeout=10.0).text
            assert "Chain verified" in report_md

            tamper_btn = page.locator("#view-audit .audit-tools button", has_text="Simulate tampering")
            expect(tamper_btn).to_be_visible(timeout=10000)
            tamper_btn.click()
            expect(page.locator("#view-audit .verify-result.bad")).to_contain_text("Chain broken", timeout=10000)
            verify_broken = httpx.get(f"{base_url}/v1/audit/verify", timeout=10.0).json()
            assert verify_broken["ok"] is False

            restore_btn = page.locator("#view-audit .audit-tools button", has_text="Restore event")
            expect(restore_btn).to_be_visible(timeout=10000)
            restore_btn.click()
            expect(page.locator("#view-audit .verify-result.ok")).to_contain_text("Chain intact", timeout=10000)
            verify_restored = httpx.get(f"{base_url}/v1/audit/verify", timeout=10.0).json()
            assert verify_restored["ok"] is True
            report_md_restored = httpx.get(f"{base_url}/v1/audit/report/{new_order_id}", timeout=10.0).text
            assert "Chain verified" in report_md_restored

            context.close()
            browser.close()

        assert page_errors == []
        assert gemini_calls == []


def test_live_playwright_injection_screen_plan_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import expect, sync_playwright

    with _live_demo_env(tmp_path, monkeypatch) as env:
        base_url = env["base_url"]
        gemini_calls: list[Any] = env["gemini_calls"]
        human_auth = env["human_auth"]
        monkeypatch.setenv("AIMO_FAULTS", "inject_text:464306")

        page_errors: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            _attach_offline_font_routes(page, base_url)

            page.goto(f"{base_url}/ui/?present=1", wait_until="domcontentloaded")
            expect(page.locator("#mode-pill")).to_contain_text("LIVE", timeout=10000)
            expect(page.locator("#btn-run")).to_be_enabled(timeout=10000)

            page.locator("#btn-run").click()
            expect(page.locator("#timeline .tl-item.k-wait")).to_be_visible(timeout=20000)

            page.wait_for_timeout(1100)
            page.locator("#tab-plan").click()
            expect(page.locator("#view-plan table.t")).to_contain_text("Prompt injection flagged", timeout=10000)
            expect(page.locator("#view-plan table.t .plan-screen.bad")).to_contain_text(
                "Prompt-injection screen: FLAGGED",
                timeout=10000,
            )

            agent_headers = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
            pending = httpx.get(f"{base_url}/v1/approvals?status=pending", headers=agent_headers, timeout=10.0).json()
            assert len(pending) == 1
            oid = pending[0]["order"]["order_id"]
            qhash = pending[0]["order"]["quote_hash"]
            anna_pin = human_auth.local_pin("anna@hotel-aurora.example")
            rej = httpx.post(
                f"{base_url}/v1/approvals/{oid}/decision",
                headers={"X-Approver-Pin": anna_pin},
                json={"decision": "reject", "approver": "anna@hotel-aurora.example", "quote_hash": qhash},
                timeout=10.0,
            )
            assert rej.status_code == 200
            expect(page.locator("#timeline .tl-item.k-final")).to_be_visible(timeout=10000)

            context.close()
            browser.close()

        assert page_errors == []
        assert gemini_calls == []


def test_committed_demo_backup_mp4() -> None:
    mp4 = ROOT / "docs" / "demo-backup.mp4"
    if not mp4.exists():
        pytest.skip("docs/demo-backup.mp4 not yet merged")

    size = mp4.stat().st_size
    assert 50_000 <= size <= 10_000_000

    info = _probe_mp4(mp4)
    video_streams = [s for s in info.get("streams", []) if s.get("width") is not None]
    assert video_streams
    stream = video_streams[0]
    assert stream.get("codec_name") == "h264"
    assert stream.get("pix_fmt") == "yuv420p"
    assert int(stream.get("width")) == 1280
    assert int(stream.get("height")) == 800
    duration = float((info.get("format") or {}).get("duration") or 0.0)
    assert duration >= 15.0


def test_record_demo_video_script_fast(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "record_demo_video.py"
    if not script.exists():
        pytest.skip("scripts/record_demo_video.py not yet merged")

    out_mp4 = tmp_path / "test.mp4"
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(ROOT)
    if SHARED_PW.is_dir():
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SHARED_PW))
    if env.get("AIMO_APPROVER_PIN") == "test-pin":
        env.pop("AIMO_APPROVER_PIN", None)
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)

    res = subprocess.run(
        [sys.executable, str(script), "--output", str(out_mp4), "--fast"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    assert out_mp4.exists() and out_mp4.stat().st_size > 10_000

    info = _probe_mp4(out_mp4)
    video_streams = [s for s in info.get("streams", []) if s.get("width") is not None]
    assert video_streams
    stream = video_streams[0]
    assert stream.get("codec_name") == "h264"
    assert stream.get("pix_fmt") == "yuv420p"
    assert int(stream.get("width")) == 1280
    assert int(stream.get("height")) == 800
