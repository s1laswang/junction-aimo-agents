from __future__ import annotations

import concurrent.futures
import contextlib
import inspect
import json
import logging
import socket
import sqlite3
import threading
import time
from typing import Any, Iterator
import urllib.error
import urllib.request

import pytest
import uvicorn

from aimo import config
from aimo.agents import drift, seller
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.backend import replay
from aimo.catalog import textsearch
from aimo.gateway import human_auth
from aimo.gateway.app import create_app


def _synthetic_catalog_products() -> list[dict[str, Any]]:
    def _prod(
        pid: str,
        sales: str,
        name_en: str,
        brand: str,
        category: str,
        conv: list[dict[str, Any]],
        allergens: list[tuple[str, str]],
        claims: list[str],
    ) -> dict[str, Any]:
        return {
            "id": pid,
            "deleted": False,
            "salesUnit": sales,
            "baseUnit": sales,
            "category": category,
            "vendorName": brand,
            "allowedLotSize": 1.0,
            "countryOfOrigin": "FI",
            "units": [{"unitId": sales, "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": brand,
                "names": [{"language": "en", "value": name_en}],
                "unitConversions": conv,
                "classifications": [
                    {
                        "name": "allergen",
                        "type": "ENUM",
                        "values": [{"id": code, "unit": state} for code, state in allergens],
                    },
                    {
                        "name": "nutritionalClaim",
                        "type": "ENUM",
                        "values": [{"id": cl, "unit": "FREE_FROM"} for cl in claims],
                    },
                    {"name": "nutritionFact", "values": [{"id": "FAT", "value": 1.5}]},
                ],
                "keyIngredients": [{"language": "en", "value": "water, food ingredients"}],
                "marketingTexts": [{"language": "en", "value": f"{name_en} for foodservice kitchens."}],
            },
        }

    vol_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    vol_10l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 10.0}}]
    vol_20l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 20.0}}]
    wt_1kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 1.0}}]
    wt_5kg = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "netWeight": {"unit": "KGM", "value": 5.0}}]

    dairy_free_nut_free = [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "CONTAINS")]
    gluten_free_dairy = [("AW", "FREE_FROM"), ("AM", "CONTAINS")]
    gluten_free_vegan = [("AW", "FREE_FROM"), ("AM", "FREE_FROM"), ("AN", "FREE_FROM")]

    return [
        _prod(
            "463038",
            "KI",
            "Valio Eila semi skimmed milk drink 10 l novobox lactose free",
            "Valio",
            "10400",
            vol_20l,
            [("AM", "CONTAINS")],
            ["FREE_FROM_LACTOSE"],
        ),
        _prod(
            "464306",
            "ST",
            "Valio Eila semi skimmed milk drink 1 l lactose free",
            "Valio",
            "10400",
            vol_1l,
            [("AM", "CONTAINS")],
            ["FREE_FROM_LACTOSE"],
        ),
        _prod(
            "460396",
            "CS",
            "Valio skimmed milk 10 l novobox",
            "Valio",
            "10400",
            vol_10l,
            [("AM", "CONTAINS")],
            [],
        ),
        _prod(
            "463982",
            "KI",
            "Kieku Barn Eggs L180 12300g",
            "Kieku",
            "10400",
            wt_5kg,
            [("AE", "CONTAINS")],
            [],
        ),
        _prod(
            "460468",
            "KI",
            "Paulig Presidentti Tumma Paahto 18x300g",
            "Paulig",
            "21100",
            wt_5kg,
            [("AM", "FREE_FROM")],
            [],
        ),
        _prod(
            "460884",
            "KI",
            "Reuter and Stolt Croissant butter 18 percent 80 x 90g",
            "Reuter",
            "41100",
            wt_5kg,
            [("AM", "CONTAINS"), ("AW", "CONTAINS")],
            [],
        ),
        _prod(
            "470889",
            "ST",
            "Oatly Oat Drink Whole 1 L",
            "Oatly",
            "10400",
            vol_1l,
            dairy_free_nut_free,
            [],
        ),
        _prod(
            "470863",
            "ST",
            "Oatly Oat Drink Semi 1L",
            "Oatly",
            "10400",
            vol_1l,
            dairy_free_nut_free,
            [],
        ),
        _prod(
            "461578",
            "CS",
            "Vaasan Rye bread 45x85g",
            "Vaasan",
            "11100",
            wt_5kg,
            dairy_free_nut_free,
            [],
        ),
        _prod(
            "460573",
            "KG",
            "PP POTATO PRE-WASHED BELLAROSA",
            "Valio",
            "17100",
            wt_1kg,
            [("AM", "FREE_FROM"), ("AN", "FREE_FROM"), ("AW", "FREE_FROM")],
            ["FREE_FROM_GLUTEN"],
        ),
        _prod(
            "473328",
            "ST",
            "Schar Vital dark sliced bread 350 g gluten-free",
            "Schar",
            "11100",
            wt_1kg,
            gluten_free_vegan,
            ["FREE_FROM_GLUTEN"],
        ),
        _prod(
            "462775",
            "ST",
            "Arla Milkdrink 1L lactose free fat free UHT",
            "Arla",
            "10400",
            vol_1l,
            gluten_free_dairy,
            ["FREE_FROM_LACTOSE", "FREE_FROM_GLUTEN"],
        ),
        _prod(
            "461765",
            "KI",
            "Moilas Gluten-free Dark Bread 600g sliced frozen",
            "Moilas",
            "41100",
            wt_5kg,
            gluten_free_vegan,
            ["FREE_FROM_GLUTEN"],
        ),
        _prod(
            "469269",
            "ST",
            "Urtekram Organic Gluten-free Oat Muesli 550g",
            "Urtekram",
            "21100",
            wt_1kg,
            gluten_free_vegan,
            ["FREE_FROM_GLUTEN"],
        ),
    ]


class _ErrorLogCatcher(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[str] = []
        self.interface_errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.records.append(msg)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            if isinstance(exc, sqlite3.InterfaceError) or "InterfaceError" in repr(exc):
                self.interface_errors.append(msg)
        elif "InterfaceError" in msg:
            self.interface_errors.append(msg)


def _install_sqlite_lock_shim_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    if "_lock" not in inspect.getsource(drift):
        orig_drift_conn = drift._conn
        orig_baseline = drift._baseline
        orig_save_baseline = drift._save_baseline

        def locked_drift_conn():
            with store._lock:
                return orig_drift_conn()

        def locked_baseline(customer_id: str):
            with store._lock:
                return orig_baseline(customer_id)

        def locked_save_baseline(customer_id: str, prices: dict[str, float], replace: bool) -> None:
            with store._lock:
                orig_save_baseline(customer_id, prices, replace)

        monkeypatch.setattr(drift, "_conn", locked_drift_conn)
        monkeypatch.setattr(drift, "_baseline", locked_baseline)
        monkeypatch.setattr(drift, "_save_baseline", locked_save_baseline)

    if "_lock" not in inspect.getsource(seller):
        orig_seller_conn = seller._conn
        orig_save = seller._save
        orig_get = seller.get_proposal
        orig_list = seller.list_proposals

        def locked_seller_conn():
            with store._lock:
                return orig_seller_conn()

        def locked_save(p: dict[str, Any]) -> None:
            with store._lock:
                orig_save(p)

        def locked_get(proposal_id: str) -> dict[str, Any]:
            with store._lock:
                return orig_get(proposal_id)

        def locked_list(customer_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
            with store._lock:
                return orig_list(customer_id, status)

        monkeypatch.setattr(seller, "_conn", locked_seller_conn)
        monkeypatch.setattr(seller, "_save", locked_save)
        monkeypatch.setattr(seller, "get_proposal", locked_get)
        monkeypatch.setattr(seller, "list_proposals", locked_list)

    if "local" not in inspect.getsource(textsearch._con) and "_lock" not in inspect.getsource(textsearch):
        tls = threading.local()

        def thread_local_con() -> sqlite3.Connection:
            con = getattr(tls, "con", None)
            if con is None:
                con = catalog_store.connect()
                tls.con = con
            return con

        monkeypatch.setattr(textsearch, "_con", thread_local_con)


@pytest.fixture
def live_gateway_server(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, _ErrorLogCatcher]]:
    state_db = tmp_path / "state.sqlite"
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_DEMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")

    used_synth = not config.PRODUCT_JSON.exists()
    if used_synth:
        pjson = tmp_path / "valio_aimo_junction26_product_data.json"
        cdb = tmp_path / "catalog.sqlite"
        pjson.write_text(json.dumps(_synthetic_catalog_products()), encoding="utf-8")
        monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(config, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
        monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
    else:
        monkeypatch.setattr(catalog_store, "PRODUCT_JSON", config.PRODUCT_JSON)
        monkeypatch.setattr(catalog_store, "CATALOG_DB", config.CATALOG_DB)
        monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", config.RELIABILITY_CSV)
        monkeypatch.setattr(catalog_store, "CO2_CSV", config.CO2_CSV)

    if hasattr(textsearch._con, "cache_clear"):
        textsearch._con.cache_clear()
    if hasattr(textsearch._nonfood_categories, "cache_clear"):
        textsearch._nonfood_categories.cache_clear()

    catalog_store.load_catalog()
    _install_sqlite_lock_shim_if_needed(monkeypatch)
    human_auth.reset_for_tests()
    bc.set_client(replay.ReplayClient(replay.BUNDLED_DIR))

    catcher = _ErrorLogCatcher()
    tracked_loggers = [
        logging.getLogger("uvicorn.error"),
        logging.getLogger("aimo"),
        logging.getLogger("aimo.gateway"),
        logging.getLogger("aimo.agents.drift"),
        logging.getLogger("aimo.agents.seller"),
    ]
    for lg in tracked_loggers:
        lg.addHandler(catcher)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    app = create_app(with_mcp=False)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.05)
    else:
        server.should_exit = True
        sock.close()
        pytest.fail("ephemeral uvicorn gateway failed to start")

    try:
        yield base, catcher
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        with contextlib.suppress(Exception):
            sock.close()
        for lg in tracked_loggers:
            lg.removeHandler(catcher)
        bc.set_client(None)
        human_auth.reset_for_tests()
        if hasattr(textsearch._con, "cache_clear"):
            textsearch._con.cache_clear()
        if hasattr(textsearch._nonfood_categories, "cache_clear"):
            textsearch._nonfood_categories.cache_clear()


def _http_call(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json", **(headers or {})}
    if raw is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=raw, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body_bytes = err.read()
        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            parsed = {"raw": body_bytes.decode("utf-8", errors="replace")}
        return err.code, parsed


def test_concurrent_24_drift_and_proposals_routes_zero_5xx_or_interface_error(
    live_gateway_server: tuple[str, _ErrorLogCatcher],
) -> None:
    base, catcher = live_gateway_server
    seller_hdr = {"X-Agent-Id": "valio-aimo/seller-1"}

    seed_specs = [
        ("hotel-aurora", "464306", 5.0),
        ("school-tampere", "470889", 4.0),
        ("hospital-meilahti", "473328", 3.0),
        ("hotel-aurora", "464306", 8.0),
    ]
    accept_targets: list[tuple[str, str]] = []
    decline_targets: list[tuple[str, str]] = []

    for cust, pid, qty in seed_specs[:2]:
        st, body = _http_call(
            "POST",
            f"{base}/v1/proposals",
            headers=seller_hdr,
            payload={"customer_id": cust, "product_id": pid, "qty_sales_units": qty},
        )
        assert st == 200, body
        accept_targets.append((body["proposal_id"], cust))

    for cust, pid, qty in seed_specs[2:]:
        st, body = _http_call(
            "POST",
            f"{base}/v1/proposals",
            headers=seller_hdr,
            payload={"customer_id": cust, "product_id": pid, "qty_sales_units": qty},
        )
        assert st == 200, body
        decline_targets.append((body["proposal_id"], cust))

    operations: list[tuple[str, str, dict[str, str] | None, dict[str, Any] | None]] = [
        ("GET", f"{base}/v1/drift/hotel-aurora", None, None),
        ("GET", f"{base}/v1/drift/hotel-aurora", None, None),
        ("GET", f"{base}/v1/drift/hotel-aurora?rebaseline=true", {"X-Customer-Id": "hotel-aurora"}, None),
        ("GET", f"{base}/v1/drift/hotel-aurora", None, None),
        ("GET", f"{base}/v1/drift/school-tampere", None, None),
        ("GET", f"{base}/v1/drift/school-tampere", None, None),
        ("GET", f"{base}/v1/drift/school-tampere?rebaseline=true", {"X-Customer-Id": "school-tampere"}, None),
        ("GET", f"{base}/v1/drift/school-tampere", None, None),
        ("GET", f"{base}/v1/drift/hospital-meilahti", None, None),
        ("GET", f"{base}/v1/drift/hospital-meilahti", None, None),
        ("GET", f"{base}/v1/drift/hospital-meilahti?rebaseline=true", {"X-Customer-Id": "hospital-meilahti"}, None),
        ("GET", f"{base}/v1/drift/hospital-meilahti", None, None),
        (
            "POST",
            f"{base}/v1/proposals",
            seller_hdr,
            {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 6.0},
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            seller_hdr,
            {"customer_id": "school-tampere", "product_id": "470889", "qty_sales_units": 3.0},
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            seller_hdr,
            {"customer_id": "hospital-meilahti", "product_id": "473328", "qty_sales_units": 2.0},
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            {"X-Agent-Id": "valio-aimo/seller-2"},
            {"customer_id": "hotel-aurora"},
        ),
        ("GET", f"{base}/v1/proposals", None, None),
        ("GET", f"{base}/v1/proposals?customer_id=hotel-aurora", None, None),
        ("GET", f"{base}/v1/proposals?customer_id=school-tampere&status=all", None, None),
        ("GET", f"{base}/v1/proposals?status=proposed", None, None),
        (
            "POST",
            f"{base}/v1/proposals/{accept_targets[0][0]}/accept",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": accept_targets[0][1]},
            {},
        ),
        (
            "POST",
            f"{base}/v1/proposals/{accept_targets[1][0]}/accept",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": accept_targets[1][1]},
            {},
        ),
        (
            "POST",
            f"{base}/v1/proposals/{decline_targets[0][0]}/decline",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": decline_targets[0][1]},
            {"reason": "covered by weekly stock"},
        ),
        (
            "POST",
            f"{base}/v1/proposals/{decline_targets[1][0]}/decline",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": decline_targets[1][1]},
            {"reason": "not needed this week"},
        ),
    ]
    assert len(operations) == 24

    barrier = threading.Barrier(24)

    def worker(idx: int) -> tuple[int, int, Any]:
        method, url, hdrs, body = operations[idx]
        barrier.wait(timeout=10.0)
        status, resp = _http_call(method, url, headers=hdrs, payload=body)
        return idx, status, resp

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(worker, i) for i in range(24)]
        results = [f.result(timeout=30.0) for f in futures]

    server_errors = [(idx, st, resp) for idx, st, resp in results if st >= 500]
    assert server_errors == []
    assert catcher.interface_errors == []
    assert all(st == 200 for _idx, st, _resp in results), results

    for pid, _cust in accept_targets:
        st_p, p_body = _http_call("GET", f"{base}/v1/proposals/{pid}")
        assert st_p == 200 and p_body["status"] == "accepted"

    for pid, _cust in decline_targets:
        st_p, p_body = _http_call("GET", f"{base}/v1/proposals/{pid}")
        assert st_p == 200 and p_body["status"] == "declined"

    st_v, verify_body = _http_call("GET", f"{base}/v1/audit/verify")
    assert st_v == 200
    assert verify_body["ok"] is True


def test_concurrent_24_seller_vs_buyer_auth_refusals(
    live_gateway_server: tuple[str, _ErrorLogCatcher],
) -> None:
    base, catcher = live_gateway_server

    st1, hotel_prop = _http_call(
        "POST",
        f"{base}/v1/proposals",
        headers={"X-Agent-Id": "valio-aimo/seller-1"},
        payload={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5.0},
    )
    assert st1 == 200, hotel_prop
    hotel_pid = hotel_prop["proposal_id"]

    st2, school_prop = _http_call(
        "POST",
        f"{base}/v1/proposals",
        headers={"X-Agent-Id": "valio-aimo/seller-1"},
        payload={"customer_id": "school-tampere", "product_id": "470889", "qty_sales_units": 5.0},
    )
    assert st2 == 200, school_prop
    school_pid = school_prop["proposal_id"]

    cases: list[tuple[str, str, dict[str, str] | None, dict[str, Any] | None, int, str]] = [
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/accept",
            {"X-Agent-Id": "valio-aimo/seller-1", "X-Customer-Id": "hotel-aurora"},
            {},
            403,
            "seller_cannot_order",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{school_pid}/accept",
            {"X-Agent-Id": "valio-aimo/seller-9", "X-Customer-Id": "school-tampere"},
            {},
            403,
            "seller_cannot_order",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/decline",
            {"X-Agent-Id": "valio-aimo/seller-1", "X-Customer-Id": "hotel-aurora"},
            {"reason": "seller trying to decline"},
            403,
            "seller_cannot_order",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{school_pid}/decline",
            {"X-Agent-Id": "valio-aimo/seller-2", "X-Customer-Id": "school-tampere"},
            {"reason": "seller trying to decline"},
            403,
            "seller_cannot_order",
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": "hotel-aurora"},
            {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5.0},
            403,
            "seller_required",
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            {"X-Agent-Id": "school-tampere/buyer-1", "X-Customer-Id": "school-tampere"},
            {"customer_id": "school-tampere", "product_id": "470889", "qty_sales_units": 5.0},
            403,
            "seller_required",
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            {"X-Agent-Id": "hospital-meilahti/buyer-1", "X-Customer-Id": "hospital-meilahti"},
            {"customer_id": "hospital-meilahti", "product_id": "473328", "qty_sales_units": 2.0},
            403,
            "seller_required",
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            {},
            {"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 2.0},
            401,
            "identity_required",
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            {"X-Agent-Id": "valio-aimo/seller-1"},
            {"customer_id": "unknown-buyer-org", "product_id": "464306", "qty_sales_units": 5.0},
            403,
            "unknown_customer",
        ),
        (
            "POST",
            f"{base}/v1/proposals",
            {"X-Agent-Id": "valio-aimo/seller-1"},
            {"customer_id": "../bad-customer", "product_id": "464306", "qty_sales_units": 5.0},
            403,
            "unknown_customer",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/accept",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": "unknown-buyer-org"},
            {},
            403,
            "unknown_customer",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/decline",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": "unknown-buyer-org"},
            {"reason": "unknown caller"},
            403,
            "unknown_customer",
        ),
        (
            "GET",
            f"{base}/v1/drift/hotel-aurora?rebaseline=true",
            {"X-Customer-Id": "unknown-buyer-org"},
            None,
            403,
            "unknown_customer",
        ),
        (
            "GET",
            f"{base}/v1/drift/unknown-buyer-org?rebaseline=true",
            {"X-Customer-Id": "hotel-aurora"},
            None,
            403,
            "unknown_customer",
        ),
        (
            "GET",
            f"{base}/v1/drift/hotel-aurora?rebaseline=true",
            {"X-Customer-Id": "school-tampere"},
            None,
            403,
            "customer_mismatch",
        ),
        (
            "GET",
            f"{base}/v1/drift/school-tampere?rebaseline=true",
            {"X-Customer-Id": "hospital-meilahti"},
            None,
            403,
            "customer_mismatch",
        ),
        (
            "GET",
            f"{base}/v1/drift/hospital-meilahti?rebaseline=true",
            {"X-Customer-Id": "hotel-aurora"},
            None,
            403,
            "customer_mismatch",
        ),
        (
            "GET",
            f"{base}/v1/drift/hotel-aurora?rebaseline=true",
            None,
            None,
            401,
            "identity_required",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/accept",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": "school-tampere"},
            {},
            403,
            "customer_mismatch",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{school_pid}/accept",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": "hospital-meilahti"},
            {},
            403,
            "customer_mismatch",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/decline",
            {"X-Agent-Id": "buyer-1", "X-Customer-Id": "school-tampere"},
            {"reason": "wrong customer"},
            403,
            "not_your_proposal",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/accept",
            {"X-Customer-Id": "hotel-aurora"},
            {},
            401,
            "identity_required",
        ),
        (
            "POST",
            f"{base}/v1/proposals/{hotel_pid}/decline",
            {"X-Agent-Id": "buyer-1"},
            {"reason": "missing customer header"},
            401,
            "identity_required",
        ),
        (
            "POST",
            f"{base}/v1/quotes",
            {"X-Agent-Id": "valio-aimo/seller-1", "X-Customer-Id": "hotel-aurora"},
            {
                "customer_id": "hotel-aurora",
                "delivery_date": "2026-11-19",
                "lines": [{"product_id": "464306", "qty_sales_units": 1}],
            },
            403,
            "scope_missing",
        ),
    ]
    assert len(cases) == 24

    barrier = threading.Barrier(24)

    def run_case(idx: int) -> tuple[int, int, Any, int, str]:
        method, url, hdrs, body, exp_status, exp_err = cases[idx]
        barrier.wait(timeout=10.0)
        st, resp = _http_call(method, url, headers=hdrs, payload=body)
        return idx, st, resp, exp_status, exp_err

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        futs = [pool.submit(run_case, i) for i in range(24)]
        outcomes = [f.result(timeout=30.0) for f in futs]

    assert catcher.interface_errors == []
    for idx, st, resp, exp_status, exp_err in outcomes:
        assert st < 500, (idx, st, resp)
        assert st == exp_status, (idx, st, resp, exp_status)
        assert resp.get("error") == exp_err, (idx, resp, exp_err)

    st_h, open_hotel = _http_call("GET", f"{base}/v1/proposals/{hotel_pid}")
    st_s, open_school = _http_call("GET", f"{base}/v1/proposals/{school_pid}")
    assert st_h == 200 and open_hotel["status"] == "proposed"
    assert st_s == 200 and open_school["status"] == "proposed"


def test_drift_rebaseline_and_proposal_agent_keys_enforcement(
    live_gateway_server: tuple[str, _ErrorLogCatcher],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, catcher = live_gateway_server
    monkeypatch.setenv("AIMO_AGENT_KEYS", "hotel-aurora:hotel-secret-key-99")

    st_p, prop = _http_call(
        "POST",
        f"{base}/v1/proposals",
        headers={"X-Agent-Id": "valio-aimo/seller-1"},
        payload={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 2.0},
    )
    assert st_p == 200, prop
    pid = prop["proposal_id"]

    st_no_key, body_no_key = _http_call(
        "GET",
        f"{base}/v1/drift/hotel-aurora?rebaseline=true",
        headers={"X-Customer-Id": "hotel-aurora"},
    )
    assert st_no_key == 401 and body_no_key["error"] == "agent_key_required"

    st_bad_key, body_bad_key = _http_call(
        "GET",
        f"{base}/v1/drift/hotel-aurora?rebaseline=true",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "wrong-key"},
    )
    assert st_bad_key == 403 and body_bad_key["error"] == "invalid_agent_key"

    st_ok_key, _body_ok_key = _http_call(
        "GET",
        f"{base}/v1/drift/hotel-aurora?rebaseline=true",
        headers={"X-Customer-Id": "hotel-aurora", "X-Agent-Key": "hotel-secret-key-99"},
    )
    assert st_ok_key == 200

    st_acc_missing, body_acc_missing = _http_call(
        "POST",
        f"{base}/v1/proposals/{pid}/accept",
        headers={"X-Agent-Id": "buyer-1", "X-Customer-Id": "hotel-aurora"},
        payload={},
    )
    assert st_acc_missing == 401 and body_acc_missing["error"] == "agent_key_required"

    st_dec_bad, body_dec_bad = _http_call(
        "POST",
        f"{base}/v1/proposals/{pid}/decline",
        headers={"X-Agent-Id": "buyer-1", "X-Customer-Id": "hotel-aurora", "X-Agent-Key": "wrong-key"},
        payload={"reason": "bad key"},
    )
    assert st_dec_bad == 403 and body_dec_bad["error"] == "invalid_agent_key"

    st_acc_ok, body_acc_ok = _http_call(
        "POST",
        f"{base}/v1/proposals/{pid}/accept",
        headers={"X-Agent-Id": "buyer-1", "X-Customer-Id": "hotel-aurora", "X-Agent-Key": "hotel-secret-key-99"},
        payload={},
    )
    assert st_acc_ok == 200 and body_acc_ok["proposal"]["status"] == "accepted"
    assert catcher.interface_errors == []
