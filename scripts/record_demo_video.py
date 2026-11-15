from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIX = ROOT / "tests" / "fixtures"
SHARED_PW = Path("/tmp/fleet-root/shared/playwright")
TMP_BASE = Path("/tmp/opencode")


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


def _inject_caption_overlay(page: Any) -> None:
    page.evaluate(
        """() => {
            if (document.getElementById("aimo-demo-caption-bar")) return;
            const style = document.createElement("style");
            style.id = "aimo-demo-caption-style";
            style.textContent = `
                .aimo-cap-wrap {
                    position: fixed;
                    bottom: 12px;
                    left: 14px;
                    width: 452px;
                    background: rgba(15, 23, 42, 0.94);
                    color: rgb(248, 250, 252);
                    border: 1px solid rgba(148, 163, 184, 0.38);
                    border-radius: 10px;
                    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.34);
                    padding: 9px 14px 12px 14px;
                    z-index: 99999;
                    pointer-events: none;
                    font-family: system-ui, -apple-system, sans-serif;
                    overflow: hidden;
                }
                .aimo-cap-row {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    margin-bottom: 3px;
                }
                .aimo-cap-pill {
                    flex-shrink: 0;
                    font-size: 10.5px;
                    font-weight: 700;
                    letter-spacing: 0.05em;
                    text-transform: uppercase;
                    padding: 2px 8px;
                    border-radius: 999px;
                    background: rgb(37, 99, 235);
                    color: rgb(255, 255, 255);
                }
                .aimo-cap-pill.ok { background: rgb(22, 163, 74); }
                .aimo-cap-pill.warn { background: rgb(217, 119, 6); }
                .aimo-cap-pill.bad { background: rgb(220, 38, 38); }
                .aimo-cap-pill.info { background: rgb(37, 99, 235); }
                .aimo-cap-title {
                    font-size: 13.5px;
                    font-weight: 700;
                    color: rgb(255, 255, 255);
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }
                .aimo-cap-sub {
                    font-size: 12px;
                    line-height: 1.35;
                    color: rgb(226, 232, 240);
                }
                .aimo-cap-track {
                    position: absolute;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    height: 3px;
                    background: rgba(148, 163, 184, 0.22);
                }
                .aimo-cap-bar {
                    height: 100%;
                    width: 0%;
                    background: rgb(56, 189, 248);
                }
            `;
            document.head.appendChild(style);
            const wrap = document.createElement("div");
            wrap.id = "aimo-demo-caption-bar";
            wrap.className = "aimo-cap-wrap";
            wrap.innerHTML = `
                <div class="aimo-cap-row">
                    <span id="aimo-cap-pill" class="aimo-cap-pill info">BEAT 1 / 6</span>
                    <span id="aimo-cap-title" class="aimo-cap-title">Trusted Trade Gateway</span>
                </div>
                <div id="aimo-cap-sub" class="aimo-cap-sub">Initializing keyless replay console</div>
                <div class="aimo-cap-track"><div id="aimo-cap-bar" class="aimo-cap-bar"></div></div>
            `;
            document.body.appendChild(wrap);
            window.__aimoCapTimer = null;
            window.__setAimoCaption = (pill, tone, title, sub, durationMs) => {
                const pillEl = document.getElementById("aimo-cap-pill");
                const titleEl = document.getElementById("aimo-cap-title");
                const subEl = document.getElementById("aimo-cap-sub");
                const barEl = document.getElementById("aimo-cap-bar");
                if (!pillEl || !titleEl || !subEl || !barEl) return;
                pillEl.textContent = pill;
                pillEl.className = "aimo-cap-pill " + (tone || "info");
                titleEl.textContent = title;
                subEl.textContent = sub;
                if (window.__aimoCapTimer) clearInterval(window.__aimoCapTimer);
                const start = performance.now();
                const total = Math.max(150, durationMs || 3000);
                barEl.style.width = "2%";
                window.__aimoCapTimer = setInterval(() => {
                    const pct = Math.min(100, ((performance.now() - start) / total) * 100);
                    barEl.style.width = pct.toFixed(0) + "%";
                    if (pct >= 100) {
                        clearInterval(window.__aimoCapTimer);
                        window.__aimoCapTimer = null;
                    }
                }, 200);
            };
        }"""
    )


def _caption(
    page: Any,
    pill: str,
    tone: str,
    title: str,
    sub: str,
    hold_ms: int,
) -> None:
    _inject_caption_overlay(page)
    page.evaluate(
        "([p, tn, tt, sb, ms]) => window.__setAimoCaption && window.__setAimoCaption(p, tn, tt, sb, ms)",
        [pill, tone, title, sub, hold_ms],
    )
    page.wait_for_timeout(hold_ms)


def _safe_scroll(page: Any, selector: str) -> None:
    page.evaluate(
        "(sel) => { const el = document.querySelector(sel); if (el) el.scrollIntoView({block: 'nearest'}); }",
        selector,
    )


def _sign_in_anna(page: Any, anna_pin: str, hold_ms: int) -> None:
    from playwright.sync_api import expect

    expect(page.locator("[id='signin-dialog']")).to_be_visible(timeout=5000)
    who_field = page.locator("[id='signin-who']")
    if who_field.evaluate("el => el.tagName") == "SELECT":
        who_field.select_option("__other")
    page.locator("[id='signin-who']").fill("anna@hotel-aurora.example")
    page.locator("[id='signin-pin']").fill(anna_pin)
    if hold_ms > 0:
        page.wait_for_timeout(hold_ms)
    page.locator("[id='signin-dialog'] button[type='submit']").click()


@contextlib.contextmanager
def _boot_demo_gateway(work_dir: Path):
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
        "AIMO_API_KEY",
    ):
        os.environ.pop(var, None)

    data_dir = work_dir / "aimo_data"
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

    os.environ["AIMO_DATA_DIR"] = str(data_dir)
    os.environ["AIMO_STATE_DB"] = str(sdb)
    os.environ["AIMO_KEY_DIR"] = str(kdir)
    os.environ["AIMO_OFFLINE"] = "1"
    os.environ["AIMO_SEMANTIC"] = "0"
    os.environ["AIMO_RECIPE_CHECK"] = "0"

    import httpx
    import uvicorn

    from aimo import config, gemini
    from aimo.backend import catalog_store
    from aimo.backend import client as bc
    from aimo.backend import orders as store
    from aimo.buyer import runs
    from aimo.catalog import taxonomy, textsearch
    from aimo.gateway import human_auth, resilience
    from aimo.gateway.app import create_app

    config.DATA_DIR = data_dir
    config.PRODUCT_JSON = pjson
    config.RELIABILITY_CSV = rel_csv
    config.CO2_CSV = co2_csv
    config.CATALOG_DB = cdb
    config.STATE_DB = sdb
    config.KEY_DIR = kdir
    config.GEMINI_API_KEY = ""
    config.OFFLINE = True

    catalog_store.PRODUCT_JSON = pjson
    catalog_store.RELIABILITY_CSV = rel_csv
    catalog_store.CO2_CSV = co2_csv
    catalog_store.CATALOG_DB = cdb
    taxonomy.TAX_JSON = data_dir / "cache" / "taxonomy.json"
    store.DB_PATH = sdb

    def _forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Gemini API calls are forbidden in keyless demo recorder")

    gemini.generate = _forbid_gemini
    gemini.client = _forbid_gemini

    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    taxonomy.labels_for.cache_clear()
    human_auth.reset_for_tests()
    for brk in list(resilience._breakers.values()):
        brk.reset()

    runs.registry = runs.RunRegistry()
    fixture_client = DemoFixtureClient()
    bc.set_client(fixture_client)
    catalog_store.load_catalog(cdb, force=True)
    human_auth.approver_pins()

    port = _allocate_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(with_mcp=True)
    uv_cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(uv_cfg)
    thread = threading.Thread(target=server.run, daemon=True, name=f"uvicorn-video-{port}")
    thread.start()

    deadline = time.time() + 15.0
    ready = False
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url}/healthz", timeout=1.0)
            if resp.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    if not ready:
        raise RuntimeError("uvicorn server failed to start on ephemeral port")

    try:
        yield {
            "base_url": base_url,
            "fixture_client": fixture_client,
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


def _transcode_to_mp4(webm_path: Path, out_mp4: Path) -> dict[str, Any]:
    ffmpeg = "/usr/bin/ffmpeg" if Path("/usr/bin/ffmpeg").is_file() else (shutil.which("ffmpeg") or "ffmpeg")
    ffprobe = "/usr/bin/ffprobe" if Path("/usr/bin/ffprobe").is_file() else (shutil.which("ffprobe") or "ffprobe")

    vf = "fps=24,scale=1280:800:force_original_aspect_ratio=decrease,pad=1280:800:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(webm_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-crf",
            "26",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(out_mp4),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(out_mp4),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(probe.stdout)


def record_demo_video(output_path: Path, fast: bool = False) -> dict[str, Any]:
    if SHARED_PW.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(SHARED_PW))

    import httpx
    from playwright.sync_api import expect, sync_playwright

    def ms(normal_ms: int, fast_ms: int) -> int:
        return fast_ms if fast else normal_ms

    TMP_BASE.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="aimo-demo-video-", dir=str(TMP_BASE)))
    video_dir = work_dir / "pw_video"
    video_dir.mkdir(parents=True, exist_ok=True)
    staged_mp4 = work_dir / "demo-backup.mp4"

    try:
        with _boot_demo_gateway(work_dir) as env:
            base_url: str = env["base_url"]
            fixture_client: DemoFixtureClient = env["fixture_client"]
            human_auth = env["human_auth"]
            anna_pin = human_auth.local_pin("anna@hotel-aurora.example")
            if len(anna_pin) < 12:
                raise RuntimeError("expected >= 12-char approver passphrase")

            page_errors: list[str] = []
            recorded_webm: Path | None = None

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    record_video_dir=str(video_dir),
                    record_video_size={"width": 1280, "height": 800},
                )
                page = context.new_page()
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                _attach_offline_font_routes(page, base_url)

                page.goto(f"{base_url}/ui/?present=1", wait_until="domcontentloaded")
                expect(page.locator("[id='mode-pill']")).to_contain_text("LIVE", timeout=10000)
                expect(page.locator("[id='customer']")).to_have_value("hotel-aurora", timeout=10000)
                expect(page.locator("[id='btn-run']")).to_be_enabled(timeout=10000)

                _caption(
                    page,
                    "BEAT 1 / 6",
                    "info",
                    "Hotel Aurora Request (1,000 L Milk)",
                    "Keyless buyer agent plans over 13 MCP tools inside customer policy",
                    ms(3000, 200),
                )

                page.locator("[id='btn-run']").click()
                if page.locator("[id='signin-dialog']").is_visible():
                    _sign_in_anna(page, anna_pin, ms(1200, 100))

                expect(page.locator("[id='timeline'] .tl-item.k-wait")).to_be_visible(timeout=20000)
                expect(page.locator("[id='request-card']")).to_contain_text("Awaiting approval", timeout=10000)
                page.wait_for_timeout(1100)

                _caption(
                    page,
                    "BEAT 1 / 6",
                    "info",
                    "Agent Paused Outside Mandate",
                    "EUR 1,010 (>EUR 500 limit) & 3.3x weekly volume require Procurement Manager",
                    ms(3800, 200),
                )

                page.locator("[id='tab-plan']").click()
                expect(page.locator("[id='view-plan']")).to_be_visible()
                expect(page.locator("[id='view-plan']")).to_contain_text("Order lines the agent proposes", timeout=10000)
                expect(page.locator("[id='view-plan'] table.t")).to_contain_text("464306")
                expect(page.locator("[id='view-plan'] table.t .plan-screen.ok").first).to_contain_text(
                    "Prompt-injection screen: clean",
                    timeout=10000,
                )
                expect(page.locator("[id='view-plan'] article.cand.in-plan")).to_contain_text("Valio Eila")
                _safe_scroll(page, "[id='view-plan'] table.t")

                _caption(
                    page,
                    "BEAT 1 / 6",
                    "ok",
                    "Plan & Prompt-Injection Screen",
                    "464306 Valio Eila 1 L (EUR 1,010.00) selected · Prompt-injection screen: clean",
                    ms(4800, 250),
                )

                rej_summary = page.locator("[id='view-plan'] details.rejected-group > summary")
                if rej_summary.count() > 0:
                    rej_summary.first.click()
                    _safe_scroll(page, "[id='view-plan'] details.rejected-group")
                    _caption(
                        page,
                        "BEAT 1 / 6",
                        "warn",
                        "Cut-Off Exception Handled",
                        "Usual 10 L novobox (463038) excluded because its delivery cut-off passed",
                        ms(4000, 200),
                    )

                agent_headers = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}
                pending_resp = httpx.get(
                    f"{base_url}/v1/approvals?status=pending",
                    headers=agent_headers,
                    timeout=10.0,
                )
                if pending_resp.status_code != 200:
                    raise RuntimeError(f"pending approvals failed: {pending_resp.status_code}")
                pending_items = pending_resp.json()
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
                if attack_resp.status_code != 403 or attack_resp.json().get("error") != "agent_cannot_approve":
                    raise RuntimeError(f"expected 403 agent_cannot_approve, got {attack_resp.status_code}")

                page.locator("[id='tab-ops']").click()
                expect(page.locator("[id='blocked-pill']")).to_be_visible(timeout=10000)
                expect(page.locator("[id='blocked-pill']")).to_contain_text("1 attack blocked", timeout=10000)
                expect(page.locator("[id='view-ops']")).to_contain_text("Blocked attacks", timeout=10000)
                expect(page.locator("[id='view-ops']")).to_contain_text("approval.refused", timeout=10000)

                _caption(
                    page,
                    "BEAT 2 / 6",
                    "bad",
                    "403 Permissions Attack Blocked",
                    "Agent buyer-1 naming Anna + valid quote_hash -> 403 agent_cannot_approve (approval.refused)",
                    ms(6200, 250),
                )

                page.locator("[id='tab-approvals']").click()
                signin_card = page.locator("[id='view-approvals'] .signin-card")
                if signin_card.count() > 0 and signin_card.first.is_visible():
                    _caption(
                        page,
                        "BEAT 3 / 6",
                        "info",
                        "Human-Only Approval Inbox",
                        "MCP exposes 13 tools and zero approval tools; named human must sign in",
                        ms(2600, 150),
                    )
                    page.locator("[id='view-approvals'] .signin-card button").click()
                    _sign_in_anna(page, anna_pin, ms(2000, 100))

                expect(page.locator("[id='approver-chip']")).to_contain_text("Anna Virtanen", timeout=10000)
                expect(page.locator("[id='view-approvals']")).to_contain_text("Signature verified", timeout=10000)
                approve_btn = page.locator("[id='view-approvals'] .decide-actions button.btn-ok")
                expect(approve_btn).to_be_enabled(timeout=10000)
                _safe_scroll(page, "[id='view-approvals'] .decide-actions")

                _caption(
                    page,
                    "BEAT 3 / 6",
                    "ok",
                    "Anna Virtanen Signed In",
                    "8-hour human session active · Ed25519 JWS quote signature verified",
                    ms(4800, 250),
                )

                fixture_client.set_price("464306", 1.26)
                expired_clock = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
                os.environ["AIMO_NOW"] = expired_clock
                os.environ["AIMO_NOW_FROZEN"] = "1"

                with page.expect_response(
                    lambda r: f"/v1/approvals/{order_id}/decision" in r.url and r.request.method == "POST",
                    timeout=10000,
                ) as requote_resp_info:
                    approve_btn.click()

                requote_http = requote_resp_info.value
                if requote_http.status != 409:
                    raise RuntimeError(f"expected 409 requote_needed, got {requote_http.status}")
                requote_body = requote_http.json()
                new_order_id = (requote_body.get("detail") or {})["new_order"]["order_id"]

                expect(page.locator("[id='view-approvals'] .card.requote")).to_be_visible(timeout=10000)
                expect(page.locator("[id='view-approvals'] .card.requote")).to_contain_text(
                    "Prices changed: re-quote needed"
                )

                _caption(
                    page,
                    "BEAT 4 / 6",
                    "warn",
                    "409 Price-Change Re-Quote",
                    "15-min lock expired + 464306 rose EUR 1.01 -> EUR 1.26: diff EUR 1,010 -> EUR 1,260",
                    ms(3500, 150),
                )

                expect(page.locator("[id='timeline']")).to_contain_text("Order re-quoted", timeout=10000)
                _caption(
                    page,
                    "BEAT 4 / 6",
                    "warn",
                    "Replacement Signed Quote Issued",
                    "Old hash refused; fresh Ed25519 quote (EUR 1,260.00) awaits human approval",
                    ms(3200, 150),
                )

                requote_approve_btn = page.locator(
                    "[id='view-approvals'] .card.requote button.btn-ok, [id='view-approvals'] .decide-actions button.btn-ok"
                ).first
                expect(requote_approve_btn).to_be_enabled(timeout=10000)
                with page.expect_response(
                    lambda r: f"/v1/approvals/{new_order_id}/decision" in r.url and r.request.method == "POST",
                    timeout=10000,
                ) as placed_resp_info:
                    requote_approve_btn.click()

                placed_http = placed_resp_info.value
                if placed_http.status != 200:
                    raise RuntimeError(f"expected 200 on replacement approval, got {placed_http.status}")

                expect(page.locator("[id='timeline'] .tl-item.k-final")).to_be_visible(timeout=10000)
                expect(page.locator("[id='request-card']")).to_contain_text("Finished", timeout=10000)
                expect(page.locator("[id='order-response']")).to_be_visible(timeout=10000)
                expect(page.locator("[id='order-response']")).to_contain_text("Placed", timeout=10000)
                expect(page.locator("[id='order-response']")).to_contain_text("AP", timeout=10000)
                _safe_scroll(page, "[id='order-response']")
                page.wait_for_timeout(550)

                _caption(
                    page,
                    "BEAT 5 / 6",
                    "ok",
                    "Order Placed · Peppol AP",
                    "Replacement quote_hash approved by Anna -> Peppol BIS OrderResponse AP (Accepted)",
                    ms(5500, 250),
                )

                page.locator("[id='tab-audit']").click()
                expect(page.locator("[id='view-audit']")).to_be_visible(timeout=10000)
                with page.expect_response(
                    lambda r: r.url.endswith("/v1/audit") and r.request.method == "GET",
                    timeout=10000,
                ):
                    page.locator("[id='view-audit'] button", has_text="Whole ledger").click()
                expect(page.locator("[id='view-audit']")).to_contain_text(
                    "Whole ledger, every agent and every customer",
                    timeout=10000,
                )
                expect(page.locator("[id='view-audit'] ol.chain li")).not_to_have_count(0, timeout=10000)

                with page.expect_response(
                    lambda r: "/v1/audit/verify" in r.url and r.request.method == "GET",
                    timeout=10000,
                ):
                    page.locator("[id='view-audit'] .audit-tools button", has_text="Verify chain").click()
                expect(page.locator("[id='view-audit'] .verify-result.ok")).to_contain_text("Chain intact", timeout=10000)

                _caption(
                    page,
                    "BEAT 6 / 6",
                    "ok",
                    "Audit Chain Verified (Intact)",
                    "Ed25519 event signatures + SHA-256 hash links verified across whole ledger",
                    ms(3600, 200),
                )

                tamper_btn = page.locator("[id='view-audit'] .audit-tools button", has_text="Simulate tampering")
                expect(tamper_btn).to_be_visible(timeout=10000)
                tamper_btn.click()
                expect(page.locator("[id='view-audit'] .verify-result.bad")).to_contain_text(
                    "Chain broken",
                    timeout=10000,
                )

                _caption(
                    page,
                    "BEAT 6 / 6",
                    "bad",
                    "Tampering Detected Immediately",
                    "Altering a stored event breaks its Ed25519 signature and hash link (Chain broken)",
                    ms(4000, 200),
                )

                restore_btn = page.locator("[id='view-audit'] .audit-tools button", has_text="Restore event")
                expect(restore_btn).to_be_visible(timeout=10000)
                restore_btn.click()
                expect(page.locator("[id='view-audit'] .verify-result.ok")).to_contain_text("Chain intact", timeout=10000)

                _caption(
                    page,
                    "BEAT 6 / 6",
                    "ok",
                    "Audit Restored · Chain Intact",
                    "Trusted Trade Agents · Open MCP + OpenAPI + Ed25519 JWS + Peppol AP",
                    ms(3600, 200),
                )

                video_obj = page.video
                context.close()
                browser.close()
                if video_obj is not None:
                    recorded_webm = Path(video_obj.path())

            if page_errors:
                raise RuntimeError(f"Browser page errors encountered: {page_errors}")
            if recorded_webm is None or not recorded_webm.is_file():
                webm_candidates = sorted(video_dir.glob("*.webm"))
                if not webm_candidates:
                    raise RuntimeError("Playwright did not produce a .webm recording")
                recorded_webm = webm_candidates[0]

            probe_info = _transcode_to_mp4(recorded_webm, staged_mp4)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_mp4), str(output_path))
            return probe_info
    finally:
        for env_key in ("AIMO_NOW", "AIMO_NOW_FROZEN"):
            os.environ.pop(env_key, None)
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record keyless Playwright backup demo video.")
    parser.add_argument(
        "--output",
        default=str(ROOT / "docs" / "demo-backup.mp4"),
        help="Destination MP4 file path (default: docs/demo-backup.mp4)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use fast beat pacing for CI smoke tests",
    )
    args = parser.parse_args(argv)
    out_path = Path(args.output).expanduser()
    if not out_path.is_absolute():
        out_path = (Path.cwd() / out_path).resolve()

    info = record_demo_video(out_path, fast=bool(args.fast))
    fmt = info.get("format") or {}
    dur = float(fmt.get("duration") or 0.0)
    sz = out_path.stat().st_size
    print(f"Wrote {out_path} ({sz:,} bytes, {dur:.2f}s, 1280x800 h264 yuv420p)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
