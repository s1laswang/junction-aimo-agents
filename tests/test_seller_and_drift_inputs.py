from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import threading
from datetime import date, datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import aimo.catalog as cat
from aimo import config
from aimo.agents import drift, seller
from aimo.agents.seller import ProposalError
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.catalog import textsearch
from aimo.gateway import fallback_catalog
from aimo.gateway.app import create_app
from aimo.gateway.service import Identity
from aimo.models import Candidate, Offer, Product

D = date(2026, 11, 19)
H_SELLER = {"X-Agent-Id": "valio-aimo/seller-1"}
H_BUYER = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "buyer-1"}


def _synth_products() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    base_10l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 10.0}}]
    return [
        {
            "id": "463038",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Valio",
            "allowedLotSize": 1.0,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [{"language": "en", "value": "Valio Eila lactose free semi skimmed milk drink 10 l"}],
                "unitConversions": base_10l,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                ],
                "keyIngredients": [{"language": "en", "value": "pasteurized lactose free milk"}],
                "marketingTexts": [{"language": "en", "value": "Lactose free milk drink"}],
            },
        },
        {
            "id": "464306",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Valio",
            "allowedLotSize": 1.0,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [{"language": "en", "value": "Valio Eila semi skimmed milk drink 1 l lactose free"}],
                "unitConversions": base_1l,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                ],
                "keyIngredients": [{"language": "en", "value": "pasteurized lactose free milk"}],
                "marketingTexts": [{"language": "en", "value": "Lactose free milk drink 1 l"}],
            },
        },
        {
            "id": "460396",
            "deleted": False,
            "salesUnit": "CS",
            "baseUnit": "CS",
            "category": "10400",
            "vendorName": "Valio",
            "allowedLotSize": 1.0,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "CS", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [{"language": "en", "value": "Valio skimmed milk 10 l novobox"}],
                "unitConversions": base_10l,
                "classifications": [
                    {"name": "allergen", "type": "ENUM", "values": [{"id": "AM", "unit": "CONTAINS"}]},
                ],
                "keyIngredients": [{"language": "en", "value": "pasteurized skimmed milk"}],
                "marketingTexts": [{"language": "en", "value": "Skimmed milk 10 l"}],
            },
        },
    ]


class _FakeBackendClient:
    def __init__(self, price_map: dict[str, Any] | None = None, avail_map: dict[str, Any] | None = None):
        self.price_map = price_map or {}
        self.avail_map = avail_map or {}
        self.down_until = 0.0
        self.last_error = None

    def get_offers_with_meta(
        self,
        product_ids: list[str],
        requested_date: date,
        customer_id: str | None = None,
        quantities: dict[str, float] | None = None,
    ) -> tuple[dict[str, Offer], dict[str, Any]]:
        now = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
        out: dict[str, Offer] = {}
        for pid in product_ids:
            raw_price = self.price_map.get(pid, 12.0 if pid == "463038" else 0.95 if pid == "464306" else 5.0)
            raw_avail = self.avail_map.get(pid, 500.0)
            out[pid] = Offer.model_construct(
                product_id=pid,
                requested_date=requested_date,
                available=True,
                available_amount=raw_avail,
                unit_price=raw_price,
                comparison_price=None,
                comparison_unit=None,
                sales_unit="ST",
                fetched_at=now,
                logistic_model="Stock",
                issues=[],
                provenance={"source": "fake"},
            )
        return out, {"source": "fake", "served_from": "memory", "fetched_at": now.isoformat(), "valid_for_s": 300}


def _make_offer(
    pid: str = "464306",
    d: date = D,
    *,
    price: Any = 2.5,
    available: Any = True,
    amount: Any = 100.0,
    comparison: Any = None,
    issues: list[str] | None = None,
) -> Offer:
    return Offer.model_construct(
        product_id=pid,
        requested_date=d,
        available=available,
        available_amount=amount,
        unit_price=price,
        comparison_price=comparison,
        comparison_unit=None,
        sales_unit="ST",
        fetched_at=datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc),
        logistic_model="Stock",
        issues=list(issues or []),
        provenance={"source": "fake"},
    )


def _fake_offers(price_map: dict[str, Any] | None = None, amount_map: dict[str, Any] | None = None):
    def fn(ids, d, customer_id=None):
        return {
            i: _make_offer(
                i,
                d,
                price=(price_map or {}).get(i, 30.0 if i == "463038" else 0.95 if i == "464306" else 5.0),
                amount=(amount_map or {}).get(i, 1000.0),
            )
            for i in ids
        }

    return fn


@pytest.fixture(scope="module")
def synth_catalog_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("seller-drift-catalog")
    pjson = d / "valio_aimo_junction26_product_data.json"
    pjson.write_text(json.dumps(_synth_products()), encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, synth_catalog_dir):
    store.close_all()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    for var in ("AIMO_FAULTS", "AIMO_AGENT_KEYS", "AIMO_AGENT_KEY", "AIMO_NOW", "AIMO_NOW_FROZEN", "AIMO_ENV"):
        monkeypatch.delenv(var, raising=False)
    pjson = synth_catalog_dir / "valio_aimo_junction26_product_data.json"
    cdb = synth_catalog_dir / "catalog.sqlite"
    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synth_catalog_dir / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", synth_catalog_dir / "missing_co2.csv")
    monkeypatch.setattr(cat, "_SOURCE", "full")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    fake_backend = _FakeBackendClient()
    monkeypatch.setattr(bc, "get_offers_with_meta", fake_backend.get_offers_with_meta)
    yield
    store.close_all()
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.fixture
def api():
    return TestClient(create_app(with_mcp=False), raise_server_exceptions=False)


def _point_catalog_missing(monkeypatch, tmp_path):
    missing_dir = tmp_path / "no-catalog"
    missing_json = missing_dir / "valio_aimo_junction26_product_data.json"
    missing_db = missing_dir / "catalog.sqlite"
    monkeypatch.setattr(config, "PRODUCT_JSON", missing_json)
    monkeypatch.setattr(config, "CATALOG_DB", missing_db)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", missing_json)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", missing_db)
    monkeypatch.setattr(cat, "_SOURCE", "missing")
    monkeypatch.setattr(fallback_catalog, "_index", None)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.mark.parametrize(
    "bad_date",
    [
        "not-a-date",
        "2026-99-99",
        "2026-02-30",
        "2026-13-01",
        "2026-00-10",
        "2026/11/19",
        "2026-11-19junk",
        "2026-11-19Tbad",
        "",
        "   ",
        "2026-11-19\x00",
        "x" * 80,
        True,
        False,
        123,
        20261119,
        3.14,
        [],
        {},
    ],
)
def test_propose_offer_rejects_bad_delivery_date(bad_date):
    with pytest.raises(ProposalError) as exc:
        seller.propose_offer(
            "hotel-aurora",
            product_id="464306",
            qty_sales_units=5,
            delivery_date=bad_date,
            offers_fn=_fake_offers(),
        )
    assert exc.value.status == 422
    assert exc.value.code == "bad_delivery_date"


@pytest.mark.parametrize(
    "good_date,expected_iso",
    [
        ("2026-11-19", "2026-11-19"),
        (date(2026, 11, 19), "2026-11-19"),
        (datetime(2026, 11, 19, 10, 30, tzinfo=timezone.utc), "2026-11-19"),
        ("2026-11-19T10:30:00Z", "2026-11-19"),
    ],
)
def test_propose_offer_accepts_valid_delivery_dates(good_date, expected_iso):
    prop = seller.propose_offer(
        "hotel-aurora",
        product_id="464306",
        qty_sales_units=4,
        delivery_date=good_date,
        offers_fn=_fake_offers(),
    )
    assert prop["delivery_date"] == expected_iso
    assert prop["qty_sales_units"] == 4.0


@pytest.mark.parametrize(
    "bad_qty",
    [
        True,
        False,
        0,
        0.0,
        -0.0,
        -1,
        -0.0001,
        -1e9,
        float("nan"),
        float("inf"),
        float("-inf"),
        1_000_001,
        1_000_000.01,
        1e300,
        10**400,
        "10",
        "abc",
        [5],
        {"qty": 5},
    ],
)
@pytest.mark.parametrize("pid", ["464306", None])
def test_propose_offer_rejects_bad_qty_sales_units(bad_qty, pid):
    with pytest.raises(ProposalError) as exc:
        seller.propose_offer(
            "hotel-aurora",
            product_id=pid,
            qty_sales_units=bad_qty,
            delivery_date="2026-11-19",
            offers_fn=_fake_offers(),
        )
    assert exc.value.status == 422
    assert exc.value.code == "bad_quantity"


@pytest.mark.parametrize(
    "bad_pid",
    [
        True,
        False,
        123,
        0,
        [],
        {},
        "",
        "   ",
        "\t\n",
        "\x00",
        "464306\x00",
        "464 306",
        "464\n306",
        "x" * 65,
    ],
)
def test_propose_offer_rejects_bad_product_id_and_replaces_product_id(bad_pid):
    with pytest.raises(ProposalError) as exc_pid:
        seller.propose_offer(
            "hotel-aurora",
            product_id=bad_pid,
            qty_sales_units=5,
            delivery_date="2026-11-19",
            offers_fn=_fake_offers(),
        )
    assert exc_pid.value.status == 422
    assert exc_pid.value.code == "bad_product_id"

    with pytest.raises(ProposalError) as exc_rep:
        seller.propose_offer(
            "hotel-aurora",
            product_id="464306",
            replaces_product_id=bad_pid,
            qty_sales_units=5,
            delivery_date="2026-11-19",
            offers_fn=_fake_offers(),
        )
    assert exc_rep.value.status == 422
    assert exc_rep.value.code == "bad_product_id"

    with pytest.raises(ProposalError) as exc_cheaper:
        seller.propose_offer(
            "hotel-aurora",
            replaces_product_id=bad_pid,
            delivery_date="2026-11-19",
            offers_fn=_fake_offers(),
        )
    assert exc_cheaper.value.status == 422
    assert exc_cheaper.value.code == "bad_product_id"


def test_propose_offer_and_find_cheaper_raise_503_catalog_missing(monkeypatch, tmp_path):
    _point_catalog_missing(monkeypatch, tmp_path)

    with pytest.raises(ProposalError) as exc_pick:
        seller.propose_offer(
            "hotel-aurora",
            product_id="464306",
            qty_sales_units=2,
            delivery_date="2026-11-19",
            offers_fn=_fake_offers(),
        )
    assert exc_pick.value.status == 503
    assert exc_pick.value.code == "catalog_missing"

    with pytest.raises(ProposalError) as exc_auto:
        seller.propose_offer(
            "hotel-aurora",
            delivery_date="2026-11-19",
            offers_fn=_fake_offers(),
        )
    assert exc_auto.value.status == 503
    assert exc_auto.value.code == "catalog_missing"

    with pytest.raises(ProposalError) as exc_fc:
        seller.find_cheaper("hotel-aurora", D, offers_fn=_fake_offers())
    assert exc_fc.value.status == 503
    assert exc_fc.value.code == "catalog_missing"


def test_per_unit_and_find_cheaper_division_guards(monkeypatch):
    assert seller._per_unit(None, 1.0) is None
    for bad_price in (None, 0, 0.0, -5.0, float("nan"), float("inf"), float("-inf"), True, False, "bad"):
        o = _make_offer(price=bad_price, comparison=None)
        assert seller._per_unit(o, 1.0) is None
    for bad_content in (None, 0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), True, False, "bad"):
        o = _make_offer(price=5.0, comparison=None)
        assert seller._per_unit(o, bad_content) is None
    for bad_comp in (0, 0.0, -2.0, float("nan"), float("inf"), float("-inf"), True, False):
        o = _make_offer(price=6.0, comparison=bad_comp)
        assert seller._per_unit(o, 2.0) == 3.0

    base_prod = Product.model_construct(
        id="463038",
        name="Base Milk 10L",
        brand="Valio",
        sales_unit="ST",
        content_amount=10.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        allergens={},
        allergen_source={},
        delivery_reliability=float("nan"),
        co2_kg_per_kg=float("inf"),
    )
    zero_content_cand = Product.model_construct(
        id="cand-zero",
        name="Zero Content Milk",
        brand="Valio",
        sales_unit="ST",
        content_amount=0.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        allergens={},
        allergen_source={},
        delivery_reliability=0.99,
        co2_kg_per_kg=0.5,
    )
    nan_content_cand = Product.model_construct(
        id="cand-nan",
        name="NaN Content Milk",
        brand="Valio",
        sales_unit="ST",
        content_amount=float("nan"),
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        allergens={},
        allergen_source={},
        delivery_reliability=0.99,
        co2_kg_per_kg=0.5,
    )
    bool_content_cand = Product.model_construct(
        id="cand-bool",
        name="Bool Content Milk",
        brand="Valio",
        sales_unit="ST",
        content_amount=True,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        allergens={},
        allergen_source={},
        delivery_reliability=0.99,
        co2_kg_per_kg=0.5,
    )
    nan_stock_cand = Product.model_construct(
        id="cand-nan-stock",
        name="NaN Stock Milk",
        brand="Valio",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        allergens={},
        allergen_source={},
        delivery_reliability=0.99,
        co2_kg_per_kg=0.5,
    )
    good_cand = Product.model_construct(
        id="464306",
        name="Good Milk 1L",
        brand="Valio",
        sales_unit="ST",
        content_amount=1.0,
        content_unit="L",
        claims=["FREE_FROM_LACTOSE"],
        allergens={},
        allergen_source={},
        delivery_reliability=0.98,
        co2_kg_per_kg=0.8,
    )

    monkeypatch.setattr(seller, "usual_items", lambda _cid: [{"product_id": "463038", "weekly_qty": 2.0}])
    monkeypatch.setattr(cat, "get_product", lambda pid: base_prod if pid == "463038" else None)
    monkeypatch.setattr("aimo.catalog.plan.find_candidates", lambda _need: ({"463038": {}, "464306": {}}, {"464306": 1.0}))

    def _fake_rank(_need, _offers, limit=40, rows=None, relevance=None):
        return [
            Candidate.model_construct(product=zero_content_cand, offer=_make_offer("cand-zero", price=0.5), tier="best", score=1.0, reasons=["r"]),
            Candidate.model_construct(product=nan_content_cand, offer=_make_offer("cand-nan", price=0.5), tier="best", score=1.0, reasons=["r"]),
            Candidate.model_construct(product=bool_content_cand, offer=_make_offer("cand-bool", price=0.5), tier="best", score=1.0, reasons=["r"]),
            Candidate.model_construct(product=nan_stock_cand, offer=_make_offer("cand-nan-stock", price=0.5, amount=float("nan")), tier="best", score=1.0, reasons=["r"]),
            Candidate.model_construct(product=good_cand, offer=_make_offer("464306", price=float("nan")), tier="best", score=1.0, reasons=["r"]),
            Candidate.model_construct(product=good_cand, offer=_make_offer("464306", price=1.2, amount=float("inf")), tier="best", score=1.0, reasons=["same type"]),
        ]

    monkeypatch.setattr("aimo.catalog.substitution.rank_substitutes", _fake_rank)
    props, _notes = seller.find_cheaper(
        "hotel-aurora",
        D,
        offers_fn=lambda ids, d, customer_id=None: {"463038": _make_offer("463038", d, price=25.0), "464306": _make_offer("464306", d, price=1.2)},
    )
    assert len(props) == 1
    assert props[0]["product"].id == "464306"
    assert props[0]["qty_sales_units"] == 20.0
    assert math.isfinite(props[0]["score"])
    assert props[0]["estimated_saving_eur"] == 26.0


@pytest.mark.parametrize(
    "bad_proposal_id",
    [
        "",
        "   ",
        "\t",
        None,
        123,
        0,
        True,
        False,
        [],
        {},
        "\x00",
        "P-20261115-abcd\x00",
        "P 20261115",
        "../P-1",
        "P/1",
        "P\\1",
        "x" * 129,
    ],
)
def test_proposal_lookup_accept_decline_list_guard_bad_proposal_id(bad_proposal_id):
    ident = Identity(agent_id="buyer-1", customer_id="hotel-aurora")

    with pytest.raises(ProposalError) as exc_get:
        seller.get_proposal(bad_proposal_id)
    assert exc_get.value.status == 422
    assert exc_get.value.code == "bad_proposal_id"

    with pytest.raises(ProposalError) as exc_list:
        seller.list_proposals(proposal_id=bad_proposal_id)
    assert exc_list.value.status == 422
    assert exc_list.value.code == "bad_proposal_id"

    with pytest.raises(ProposalError) as exc_acc:
        seller.accept_proposal(bad_proposal_id, ident)
    assert exc_acc.value.status == 422
    assert exc_acc.value.code == "bad_proposal_id"

    with pytest.raises(ProposalError) as exc_dec:
        seller.decline_proposal(bad_proposal_id, ident, "no thanks")
    assert exc_dec.value.status == 422
    assert exc_dec.value.code == "bad_proposal_id"


def test_proposal_missing_and_bad_list_filters():
    ident = Identity(agent_id="buyer-1", customer_id="hotel-aurora")
    for fn in (
        lambda: seller.get_proposal("P-missing-0001"),
        lambda: seller.accept_proposal("P-missing-0001", ident),
        lambda: seller.decline_proposal("P-missing-0001", ident),
    ):
        with pytest.raises(ProposalError) as exc:
            fn()
        assert exc.value.status == 404
        assert exc.value.code == "proposal_not_found"

    for bad_filter in (True, False, 123, ["a"], "   ", "hotel\x00", "x" * 200):
        with pytest.raises(ProposalError) as exc_cid:
            seller.list_proposals(customer_id=bad_filter)
        assert exc_cid.value.status == 422
    for bad_status in (True, False, 123, ["proposed"], "   ", "open\x00", "x" * 80):
        with pytest.raises(ProposalError) as exc_st:
            seller.list_proposals(status=bad_status)
        assert exc_st.value.status == 422


def test_drift_volume_price_availability_baseline_and_next_delivery_guards():
    base_line = {"product_id": "460396", "label": "Milk", "unit": "CS", "weeks": [8, 12]}
    assert drift.volume_flag(base_line)["change_pct"] == 50.0

    for bad_weeks in (
        [],
        [8],
        [0, 12],
        [0.0, 12.0],
        [-10, -5],
        [-5, 10],
        [float("nan"), 12],
        [8, float("nan"), 12],
        [8, float("inf")],
        [float("-inf"), 12],
        [True, 5],
        [False, 5],
        ["8", "12"],
        None,
    ):
        assert drift.volume_flag({"product_id": "x", "label": "Milk", "unit": "CS", "weeks": bad_weeks}) is None
    assert drift.volume_flag("not-a-dict") is None

    pline = {"product_id": "460884", "label": "Croissants", "unit": "KI"}
    for bad_old in (0, 0.0, -10.0, float("nan"), float("inf"), float("-inf"), True, False, None, "10"):
        assert drift.price_flag(pline, bad_old, "2026-11-14", 12.0) is None
    for bad_new in (0, 0.0, -5.0, float("nan"), float("inf"), float("-inf"), True, False, None, "12"):
        assert drift.price_flag(pline, 10.0, "2026-11-14", bad_new) is None
    assert drift.price_flag(pline, 1e-300, "2026-11-14", 1e308) is None

    ok_offer = _make_offer("460396", D, price=5.0, available=True, amount=100.0)
    for bad_qty in (0, -5, float("nan"), float("inf"), float("-inf"), True, False, "10"):
        bad_line = {"product_id": "460396", "label": "Milk", "unit": "CS", "weeks": [bad_qty]}
        assert drift.availability_flag(bad_line, ok_offer, D) is None

    for bad_avail in (float("nan"), float("-inf"), -1.0, True, False, "bad"):
        f = drift.availability_flag(base_line, _make_offer("460396", D, amount=bad_avail), D)
        assert f is not None and f["kind"] == "availability" and f["route"] == "approval"
        assert "availability error" in f["message"]
        assert "nan" not in f["message"].lower() and "inf" not in f["message"].lower()

    assert drift.availability_flag(base_line, _make_offer("460396", D, amount=float("inf")), D) is None

    drift._save_baseline(
        "hotel-aurora",
        {
            "p_zero": 0.0,
            "p_neg": -4.5,
            "p_nan": float("nan"),
            "p_inf": float("inf"),
            "p_ninf": float("-inf"),
            "p_bool": True,
            "p_none": None,
            "460884": 40.0,
        },
        replace=True,
    )
    saved = drift._baseline("hotel-aurora")
    assert saved and set(saved) == {"460884"} and saved["460884"][0] == 40.0

    res = drift.check_drift(
        "hotel-aurora",
        today=date(2026, 11, 14),
        offers_fn=lambda ids, d, customer_id=None: {
            "460396": _make_offer("460396", d, price=float("nan"), amount=float("nan")),
            "463982": _make_offer("463982", d, price=float("inf"), amount=100.0),
            "460468": _make_offer("460468", d, price=-3.0, amount=100.0),
            "460884": _make_offer("460884", d, price=44.0, amount=100.0),
        },
        audit=False,
    )
    encoded = json.dumps(res, allow_nan=False)
    assert encoded
    assert any(f["product_id"] == "460884" and f["kind"] == "price" for f in res["flags"])
    assert any(f["product_id"] == "460396" and f["kind"] == "availability" for f in res["flags"])

    for wd in range(7):
        nd = drift.next_delivery(wd, today=date(2026, 11, 15))
        assert nd > date(2026, 11, 15) and nd.weekday() == wd

    for bad_wd in (-1, -7, 7, 8, 99, True, False, 1.5, 3.0, "3", None, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            drift.next_delivery(bad_wd, today=date(2026, 11, 15))

    for bad_today in (True, False, "2026-11-15", 123):
        with pytest.raises(ValueError):
            drift.next_delivery(3, today=bad_today)


def _assert_clean_4xx_or_503(r, expected_statuses: set[int]):
    assert r.status_code != 500, r.text
    assert r.status_code in expected_statuses, (r.status_code, r.text)
    body = r.json()
    assert isinstance(body, dict) and body.get("error") and body.get("message"), body
    return body


def test_http_proposals_routes_bad_inputs_never_500(api):
    for bad_date in ("not-a-date", "2026-99-99", "2026-02-30", "", "   ", 123, True, []):
        r = api.post(
            "/v1/proposals",
            headers=H_SELLER,
            json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5, "delivery_date": bad_date},
        )
        _assert_clean_4xx_or_503(r, {422})

    for bad_qty in (0, -1, -50.5, 1_000_001, 1e300, "abc", [], {}):
        r = api.post(
            "/v1/proposals",
            headers=H_SELLER,
            json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": bad_qty, "delivery_date": "2026-11-19"},
        )
        _assert_clean_4xx_or_503(r, {422})

    for raw_nonfinite in (
        b'{"customer_id":"hotel-aurora","product_id":"464306","qty_sales_units":NaN}',
        b'{"customer_id":"hotel-aurora","product_id":"464306","qty_sales_units":Infinity}',
        b'{"customer_id":"hotel-aurora","product_id":"464306","qty_sales_units":-Infinity}',
    ):
        r = api.post(
            "/v1/proposals",
            headers={**H_SELLER, "Content-Type": "application/json"},
            content=raw_nonfinite,
        )
        _assert_clean_4xx_or_503(r, {400, 422})

    for bad_pid in ("", "   ", "\x00", "464306\x00", "464 306", "x" * 65, True, 123, []):
        r1 = api.post(
            "/v1/proposals",
            headers=H_SELLER,
            json={"customer_id": "hotel-aurora", "product_id": bad_pid, "qty_sales_units": 5, "delivery_date": "2026-11-19"},
        )
        _assert_clean_4xx_or_503(r1, {404, 422})

        r2 = api.post(
            "/v1/proposals",
            headers=H_SELLER,
            json={
                "customer_id": "hotel-aurora",
                "product_id": "464306",
                "replaces_product_id": bad_pid,
                "qty_sales_units": 5,
                "delivery_date": "2026-11-19",
            },
        )
        if r2.status_code == 200:
            assert isinstance(r2.json(), dict)
        else:
            _assert_clean_4xx_or_503(r2, {404, 422})

    for bad_path_id in ("%20%20", "%00", "P-1%00", "x" * 200, "P%201"):
        r_get = api.get(f"/v1/proposals/{bad_path_id}")
        _assert_clean_4xx_or_503(r_get, {404, 422})

        r_acc = api.post(f"/v1/proposals/{bad_path_id}/accept", headers=H_BUYER)
        _assert_clean_4xx_or_503(r_acc, {404, 422})

        r_dec = api.post(f"/v1/proposals/{bad_path_id}/decline", headers=H_BUYER, json={"reason": "no"})
        _assert_clean_4xx_or_503(r_dec, {404, 422})

    for missing_id in ("P-missing-9999", "does-not-exist"):
        assert api.get(f"/v1/proposals/{missing_id}").status_code == 404
        assert api.post(f"/v1/proposals/{missing_id}/accept", headers=H_BUYER).status_code == 404
        assert api.post(f"/v1/proposals/{missing_id}/decline", headers=H_BUYER, json={"reason": "no"}).status_code == 404

    ok = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 3, "delivery_date": "2026-11-19"},
    )
    assert ok.status_code == 200, ok.text
    pid = ok.json()["proposal_id"]

    for bad_reason in ("no\x00", "x" * 2001, 123, True, []):
        r_bad_dec = api.post(f"/v1/proposals/{pid}/decline", headers=H_BUYER, json={"reason": bad_reason})
        _assert_clean_4xx_or_503(r_bad_dec, {422})

    for bad_q in ("   ", "\x00", "x" * 200):
        r_list_c = api.get("/v1/proposals", params={"customer_id": bad_q})
        if r_list_c.status_code == 200:
            assert isinstance(r_list_c.json(), list)
        else:
            _assert_clean_4xx_or_503(r_list_c, {401, 403, 422})
        r_list_s = api.get("/v1/proposals", params={"status": bad_q})
        if r_list_s.status_code == 200:
            assert isinstance(r_list_s.json(), list)
        else:
            _assert_clean_4xx_or_503(r_list_s, {401, 403, 422})


def test_http_proposals_missing_catalog_returns_503_and_validates_inputs_first(api, monkeypatch, tmp_path):
    _point_catalog_missing(monkeypatch, tmp_path)

    r_pick = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 5, "delivery_date": "2026-11-19"},
    )
    body_pick = _assert_clean_4xx_or_503(r_pick, {503})
    assert body_pick["error"] == "catalog_missing"

    r_auto = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "hotel-aurora", "delivery_date": "2026-11-19"},
    )
    body_auto = _assert_clean_4xx_or_503(r_auto, {503})
    assert body_auto["error"] == "catalog_missing"

    r_bad_qty = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 2_000_000, "delivery_date": "2026-11-19"},
    )
    _assert_clean_4xx_or_503(r_bad_qty, {422})

    r_bad_pid = api.post(
        "/v1/proposals",
        headers=H_SELLER,
        json={"customer_id": "hotel-aurora", "product_id": "   ", "qty_sales_units": 5, "delivery_date": "2026-11-19"},
    )
    _assert_clean_4xx_or_503(r_bad_pid, {404, 422})


def test_http_drift_route_bad_inputs_and_missing_catalog_never_500(api, monkeypatch, tmp_path):
    for bad_cid in ("unknown-cust", "%00", "   ", "-1", "x" * 200):
        r = api.get(f"/v1/drift/{bad_cid}")
        body = _assert_clean_4xx_or_503(r, {404})
        assert body["error"] == "no_standing_order"

    for bad_reb in ("not-a-bool", "999"):
        r_reb = api.get("/v1/drift/hotel-aurora", params={"rebaseline": bad_reb})
        _assert_clean_4xx_or_503(r_reb, {422})

    r_unauth_reb = api.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"})
    _assert_clean_4xx_or_503(r_unauth_reb, {401})

    _point_catalog_missing(monkeypatch, tmp_path)
    bad_backend = _FakeBackendClient(
        price_map={"460396": float("nan"), "463982": float("inf"), "460468": -2.0, "460884": 42.0},
        avail_map={"460396": float("nan"), "463982": float("inf"), "460468": 50.0, "460884": 50.0},
    )
    monkeypatch.setattr(bc, "get_offers_with_meta", bad_backend.get_offers_with_meta)

    r_ok = api.get("/v1/drift/hotel-aurora")
    if r_ok.status_code == 200:
        payload = json.loads(r_ok.text)
        assert payload["customer_id"] == "hotel-aurora"
    else:
        payload = _assert_clean_4xx_or_503(r_ok, {503})
        assert payload["error"] == "catalog_missing"
    assert json.dumps(payload, allow_nan=False)


def _ensure_threadsafe_catalog_for_test(monkeypatch) -> list[Any]:
    if any(hasattr(textsearch, attr) for attr in ("_db_lock", "_lock", "_con_lock")):
        return []
    catalog_store.load_catalog()
    tls = threading.local()
    opened: list[Any] = []
    guard = threading.Lock()

    def _thread_con():
        con = getattr(tls, "con", None)
        if con is None:
            con = catalog_store.connect()
            tls.con = con
            with guard:
                opened.append(con)
        return con

    _thread_con.cache_clear = lambda: None
    monkeypatch.setattr(textsearch, "_con", _thread_con)
    return opened


def test_concurrent_http_drift_and_proposals_zero_500s(monkeypatch):
    opened_cons = _ensure_threadsafe_catalog_for_test(monkeypatch)
    try:
        app = create_app(with_mcp=False)
        seed_client = TestClient(app, raise_server_exceptions=False)
        shared_props: list[str] = []
        for _ in range(4):
            r_seed = seed_client.post(
                "/v1/proposals",
                headers=H_SELLER,
                json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 2, "delivery_date": "2026-11-19"},
            )
            assert r_seed.status_code == 200, r_seed.text
            shared_props.append(r_seed.json()["proposal_id"])

        race_prop_r = seed_client.post(
            "/v1/proposals",
            headers=H_SELLER,
            json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 2, "delivery_date": "2026-11-19"},
        )
        assert race_prop_r.status_code == 200, race_prop_r.text
        race_pid = race_prop_r.json()["proposal_id"]

        workers = 12
        barrier = threading.Barrier(workers)

        def _race_worker(idx: int) -> tuple[int, dict[str, Any]]:
            c = TestClient(app, raise_server_exceptions=False)
            barrier.wait(timeout=10)
            if idx % 2 == 0:
                resp = c.post(f"/v1/proposals/{race_pid}/accept", headers=H_BUYER)
            else:
                resp = c.post(f"/v1/proposals/{race_pid}/decline", headers=H_BUYER, json={"reason": f"parallel-{idx}"})
            assert resp.status_code != 500, resp.text
            return resp.status_code, json.loads(resp.text)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            race_results = [f.result(timeout=30) for f in [pool.submit(_race_worker, i) for i in range(workers)]]

        race_statuses = [st for st, _ in race_results]
        assert race_statuses.count(200) == 1, race_results
        assert race_statuses.count(409) == workers - 1, race_results
        for st, body in race_results:
            if st == 409:
                assert body["error"] == "not_open"

        barrier2 = threading.Barrier(workers)

        def _mixed_load_worker(idx: int) -> list[int]:
            c = TestClient(app, raise_server_exceptions=False)
            barrier2.wait(timeout=10)
            statuses: list[int] = []
            customers = ("hotel-aurora", "school-tampere", "hospital-meilahti")
            for step in range(12):
                op = (idx + step) % 8
                cust = customers[(idx + step) % len(customers)]
                if op == 0:
                    r = c.get(f"/v1/drift/{cust}")
                    assert r.status_code == 200, r.text
                elif op == 1:
                    r = c.get("/v1/drift/hotel-aurora", params={"rebaseline": "true"}, headers={"X-Customer-Id": "hotel-aurora"})
                    assert r.status_code == 200, r.text
                elif op == 2:
                    r = c.post(
                        "/v1/proposals",
                        headers=H_SELLER,
                        json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": 1 + (step % 5), "delivery_date": "2026-11-19"},
                    )
                    assert r.status_code == 200, r.text
                elif op == 3:
                    r = c.get("/v1/proposals", params={"customer_id": "hotel-aurora", "status": "all"})
                    assert r.status_code == 200, r.text
                elif op == 4:
                    target = shared_props[(idx + step) % len(shared_props)]
                    r = c.get(f"/v1/proposals/{target}")
                    assert r.status_code == 200, r.text
                elif op == 5:
                    target = shared_props[(idx + step) % len(shared_props)]
                    r = c.post(f"/v1/proposals/{target}/accept", headers=H_BUYER)
                    assert r.status_code in (200, 409), r.text
                elif op == 6:
                    r = c.post(
                        "/v1/proposals",
                        headers=H_SELLER,
                        json={"customer_id": "hotel-aurora", "product_id": "464306", "qty_sales_units": -5, "delivery_date": "2026-11-19"},
                    )
                    assert r.status_code == 422, r.text
                else:
                    r = c.get("/v1/drift/unknown-customer")
                    assert r.status_code == 404, r.text
                assert r.status_code < 500, r.text
                parsed = json.loads(r.text)
                assert json.dumps(parsed, allow_nan=False)
                statuses.append(r.status_code)
            return statuses

        with ThreadPoolExecutor(max_workers=workers) as pool:
            all_statuses = [f.result(timeout=60) for f in [pool.submit(_mixed_load_worker, i) for i in range(workers)]]

        flat = [s for sub in all_statuses for s in sub]
        assert len(flat) == workers * 12
        assert all(s < 500 for s in flat)
    finally:
        for con in opened_cons:
            con.close()


def test_concurrent_direct_sqlite_drift_and_seller_threads(monkeypatch):
    opened_cons = _ensure_threadsafe_catalog_for_test(monkeypatch)
    try:
        workers = 12
        barrier = threading.Barrier(workers)
        ident = Identity(agent_id="buyer-1", customer_id="hotel-aurora")
        seed_prop = seller.propose_offer(
            "hotel-aurora",
            product_id="464306",
            qty_sales_units=2,
            delivery_date="2026-11-19",
            offers_fn=_fake_offers(),
        )
        seed_pid = seed_prop["proposal_id"]

        def _direct_worker(idx: int) -> int:
            barrier.wait(timeout=10)
            count = 0
            for step in range(15):
                drift._save_baseline("hotel-aurora", {"460884": 38.0 + (idx % 5) + step * 0.1}, replace=bool(step % 2))
                base = drift._baseline("hotel-aurora")
                assert "460884" in base
                dres = drift.check_drift(
                    "hotel-aurora",
                    today=date(2026, 11, 14),
                    rebaseline=(step % 4 == 0),
                    offers_fn=_fake_offers({"460884": 43.0 + step * 0.2}),
                    audit=(step % 3 == 0),
                )
                assert dres["customer_id"] == "hotel-aurora"
                p = seller.propose_offer(
                    "hotel-aurora",
                    product_id="464306",
                    qty_sales_units=1 + (step % 4),
                    delivery_date="2026-11-19",
                    offers_fn=_fake_offers(),
                )
                got = seller.get_proposal(p["proposal_id"])
                assert got["proposal_id"] == p["proposal_id"]
                listed = seller.list_proposals(customer_id="hotel-aurora")
                assert listed
                if step == 0:
                    try:
                        if idx % 2 == 0:
                            seller.accept_proposal(seed_pid, ident)
                        else:
                            seller.decline_proposal(seed_pid, ident, "concurrent")
                    except ProposalError as exc:
                        assert exc.status == 409 and exc.code == "not_open"
                count += 1
            return count

        with ThreadPoolExecutor(max_workers=workers) as pool:
            counts = [f.result(timeout=60) for f in [pool.submit(_direct_worker, i) for i in range(workers)]]

        assert sum(counts) == workers * 15
        final_seed = seller.get_proposal(seed_pid)
        assert final_seed["status"] in ("accepted", "declined")
    finally:
        for con in opened_cons:
            con.close()
