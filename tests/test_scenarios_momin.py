"""Tests for the pieces ported from Momin's junction26-valio: recipes, drift, seller, gateway extras, s0..s7.

Offline by default (fake offers, temp state DB; needs data/catalog.sqlite for catalog-backed tests).
`live` tests boot a fresh gateway and run scenarios/run_all against the live mock API:
    AIMO_LIVE=1 uv run pytest -q tests/test_scenarios_momin.py -k live
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aimo import config
from aimo.backend import orders as store
from aimo.models import Offer

ROOT = Path(__file__).resolve().parents[1]
D = date(2026, 11, 20)


def _catalog_ready() -> bool:
    return config.CATALOG_DB.exists()


needs_catalog = pytest.mark.skipif(not _catalog_ready(), reason="catalog.sqlite not built")
live = pytest.mark.skipif(os.environ.get("AIMO_LIVE") != "1", reason="set AIMO_LIVE=1 to hit the live mock API")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    yield


def offer(pid: str, d: date, price: float = 5.0, available: bool = True, amount: float = 1e6, issues=None,
          comparison: float | None = None) -> Offer:
    return Offer(product_id=pid, requested_date=d, available=available, available_amount=amount, unit_price=price,
                 comparison_price=comparison, sales_unit="ST", fetched_at=datetime.now(timezone.utc),
                 logistic_model="Stock", issues=list(issues or []))


def fake_offers(price_map: dict | None = None, down: set | None = None):
    def fn(ids, d, customer_id=None):  # noqa: ARG001
        return {i: offer(i, d, (price_map or {}).get(i, 5.0), available=i not in (down or set()),
                         issues=["out_of_stock", "injected:stockout"] if i in (down or set()) else [])
                for i in ids}
    return fn


# ------------------------------------------------------------------ recipes
def test_recipe_list_has_momins_dishes():
    from aimo.recipes import list_recipes

    ids = {r["id"] for r in list_recipes()}
    assert {"lasagne", "salmon-soup", "pancakes", "carbonara"} <= ids and len(ids) == 11


@pytest.mark.skipif(not config.PRODUCT_JSON.exists(), reason="product data not present")
def test_lasagne_for_120_scales_and_converts_units():
    from aimo.recipes import recipe_needs

    rn = recipe_needs(text="Lasagne for 120 on Friday", customer_id="hotel-aurora", delivery_by="2026-11-20")
    q = {it.query: (it.quantity, it.unit) for it in rn.items}
    assert rn.portions == {"lasagne": 120} and rn.delivery_by == D
    assert q["lasagne"] == (7.2, "KG") and q["ground beef"] == (9.6, "KG")
    assert rn.excluded_allergens == []          # hotel policy excludes nothing
    # hotel's usual milk (463038) is lactose-free, so the recipe plans lactose-free dairy (gateway requires the claim)
    assert q["lactose-free milk"] == (12.0, "L") and q["lactose-free grated cheese"] == (4.8, "KG")


def test_school_milk_allergy_gets_dairy_free_swaps():
    from aimo.recipes import recipe_needs

    rn = recipe_needs("pancakes", 650, D, "school-tampere")
    q = {it.ingredient: it for it in rn.items}
    assert {"milk", "nuts"} <= set(rn.excluded_allergens)
    assert q["milk"].query == "oat drink"                       # NOT lactose-free milk (still milk protein)
    assert q["butter"].query == "lactose free margarine"        # Momin's option marked "dairy free"
    assert all({"milk", "nuts"} <= set(it.need.excluded_allergens) for it in rn.items)


def test_lactose_free_request_takes_first_option_and_claim():
    from aimo.recipes import recipe_needs

    rn = recipe_needs(text="lactose-free salmon soup for 40", customer_id="hotel-aurora", delivery_by=D)
    q = {it.ingredient: it for it in rn.items}
    assert q["milk"].query == "lactose-free milk" and q["milk"].need.required_claims == ["FREE_FROM_LACTOSE"]


def test_no_dairy_free_option_is_flagged_for_a_human():
    from aimo.recipes import recipe_needs

    rn = recipe_needs("lasagne", 30, D, "school-tampere")
    cheese = next(it for it in rn.items if it.ingredient == "grated cheese")
    assert cheese.swap and cheese.swap["query"] is None and "human" in cheese.swap["why"]
    assert "milk" in cheese.need.excluded_allergens


def test_unknown_recipe_and_missing_portions():
    from aimo.recipes import RecipeError, recipe_needs

    with pytest.raises(RecipeError) as e:
        recipe_needs("sushi", 10, D, "hotel-aurora")
    assert e.value.status == 404
    with pytest.raises(RecipeError):
        recipe_needs("lasagne", None, D, "hotel-aurora")


@needs_catalog
def test_plan_recipe_batches_offers_and_builds_quote_request():
    from aimo.recipes import plan_recipe

    calls = []
    base = fake_offers()

    def fn(ids, d, customer_id=None):
        calls.append(len(ids))
        return base(ids, d, customer_id)

    r = plan_recipe("lasagne", 120, D, "hotel-aurora", offers_fn=fn)
    assert len(calls) == 1 and calls[0] <= 1000                 # one batched upstream call for the recipe
    assert len(r["ingredients"]) == 8 and r["quote_request"]["lines"]
    assert r["quote_request"]["delivery_date"] == "2026-11-20"
    beef = next(i for i in r["ingredients"] if i["query"] == "ground beef")
    assert all("liver" not in l["product_name"].lower() for l in beef["plan"]["lines"])
    milk = next(i for i in r["ingredients"] if i["ingredient"] == "milk")
    assert all("cleaner" not in l["product_name"].lower() for l in milk["plan"]["lines"])
    assert "Momin" in r["provenance"]["credit"]


# ------------------------------------------------------------------ drift
def test_volume_flag_plain_language():
    from aimo.agents.drift import SEEDS, volume_flag

    milk = SEEDS["hotel-aurora"]["lines"][0]
    f = volume_flag(milk)
    assert f["kind"] == "volume" and f["change_pct"] == 50.0 and f["route"] == "approval"
    assert f["message"].startswith("Milk volume crept up 50% over 8 weeks (8 → 12 cases)")
    flat = SEEDS["hotel-aurora"]["lines"][1]
    assert volume_flag(flat) is None


def test_price_flag_routes():
    from aimo.agents.drift import price_flag

    line = {"product_id": "x", "label": "Croissants", "unit": "KI"}
    assert price_flag(line, 10.0, "2026-11-14", 10.3) is None              # < 5 %
    assert price_flag(line, 10.0, "2026-11-14", 10.8)["route"] == "auto"   # 8 %: within the 10 % band
    hi = price_flag(line, 10.0, "2026-11-14", 11.2)
    assert hi["route"] == "approval" and "rose 12%" in hi["message"] and "€10.00 → €11.20" in hi["message"]
    assert price_flag(line, 10.0, "2026-11-14", 9.0)["route"] == "auto"


@needs_catalog
def test_check_drift_baseline_then_price_and_stockout():
    from aimo.agents.drift import check_drift

    first = check_drift("hotel-aurora", today=date(2026, 11, 14), offers_fn=fake_offers({"460884": 40.0}), audit=False)
    assert not [f for f in first["flags"] if f["kind"] == "price"]          # baseline recorded, no flag yet
    assert {f["product_id"] for f in first["flags"] if f["kind"] == "volume"} == {"460396", "460884"}
    later = check_drift("hotel-aurora", today=date(2026, 11, 14), offers_fn=fake_offers({"460884": 44.8}, {"463982"}),
                        audit=False)
    kinds = {(f["product_id"], f["kind"]) for f in later["flags"]}
    assert ("460884", "price") in kinds and ("463982", "availability") in kinds
    eggs = next(f for f in later["flags"] if f["product_id"] == "463982")
    assert "Eggs cannot be delivered" in eggs["message"] and later["delivery_date"] == "2026-11-19"


def test_seeds_use_our_customers_and_safe_products():
    from aimo.agents.drift import SEEDS

    assert set(SEEDS) == {"hotel-aurora", "school-tampere", "hospital-meilahti"}


@needs_catalog
def test_seed_products_exist_and_respect_policy_allergens():
    from aimo.agents.drift import SEEDS
    from aimo.catalog import get_product
    from aimo.gateway.verify import policy_excluded_allergens

    for cust, seed in SEEDS.items():
        excl = policy_excluded_allergens(cust)
        for l in seed["lines"]:
            p = get_product(l["product_id"])
            assert p is not None, l
            for g in excl:
                st = p.allergens.get(g)
                assert getattr(st, "value", st) not in ("contains", "may_contain"), (cust, l["product_id"], g)


# ------------------------------------------------------------------ seller
def test_seller_ids_always_valio():
    from aimo.agents.seller import is_seller, seller_id

    assert seller_id(None) == "valio-aimo/seller-1" and seller_id("buyer-1") == "valio-aimo/seller-1"
    assert seller_id("valio-aimo/seller-7") == "valio-aimo/seller-7"
    assert is_seller("valio-aimo/seller-1") and not is_seller("buyer-1")


@needs_catalog
def test_seller_proposes_but_can_never_accept():
    from aimo.agents import seller
    from aimo.gateway.service import Identity

    prices = {"463038": 31.31, "464306": 1.01}
    p = seller.propose_offer("hotel-aurora", product_id="464306", qty_sales_units=20, replaces_product_id="463038",
                             delivery_date="2026-11-19", offers_fn=fake_offers(prices))
    assert p["status"] == "proposed" and p["seller_can_order"] is False and p["seller_id"] == "valio-aimo/seller-1"
    assert p["policy_preview"]["binding"] is False and p["policy_preview"]["outcome"] in ("auto_approve", "needs_approval")
    assert p["quote_request"]["lines"] == [{"product_id": "464306", "qty_sales_units": 20.0}]
    with pytest.raises(seller.ProposalError) as e:
        seller.accept_proposal(p["proposal_id"], Identity(agent_id="valio-aimo/seller-1", customer_id="hotel-aurora"))
    assert e.value.status == 403 and e.value.code == "seller_cannot_order"
    with pytest.raises(seller.ProposalError) as e2:
        seller.accept_proposal(p["proposal_id"], Identity(agent_id="buyer-1", customer_id="school-tampere"))
    assert e2.value.code == "customer_mismatch"
    assert seller.get_proposal(p["proposal_id"])["status"] == "proposed"   # still open, nothing ordered
    assert [x["proposal_id"] for x in seller.list_proposals("hotel-aurora")] == [p["proposal_id"]]


@needs_catalog
def test_seller_milk_proposal_to_milk_free_school_previews_reject():
    from aimo.agents import seller

    p = seller.propose_offer("school-tampere", product_id="460396", qty_sales_units=2, delivery_date="2026-11-17",
                             offers_fn=fake_offers())
    assert p["policy_preview"]["outcome"] == "reject"


@needs_catalog
def test_seller_cannot_quote_or_order_over_http():
    from fastapi.testclient import TestClient

    from aimo.gateway.app import create_app
    from aimo.gateway.extras import install

    api = TestClient(install(create_app(with_mcp=False)))
    h = {"X-Customer-Id": "hotel-aurora", "X-Agent-Id": "valio-aimo/seller-1"}
    r = api.post("/v1/quotes", headers=h, json={"customer_id": "hotel-aurora", "delivery_date": "2026-11-19",
                                                "lines": [{"product_id": "464306", "qty_sales_units": 1}]})
    assert r.status_code == 403 and r.json()["error"] == "scope_missing"
    assert api.get("/v1/recipes").status_code == 200
    assert api.get("/v1/drift/nobody").status_code == 404
    assert api.post("/v1/proposals/P-x/accept", headers=h).status_code in (403, 404)


def test_extras_mcp_tools_register_once():
    import asyncio

    from aimo.gateway.extras import EXTRA_TOOLS, register_mcp_tools
    from aimo.gateway.mcp_server import build_mcp

    m = register_mcp_tools(register_mcp_tools(build_mcp()))
    names = [t.name for t in asyncio.run(m.list_tools())]
    assert set(EXTRA_TOOLS) <= set(names) and len(names) == len(set(names))


# ------------------------------------------------------------------ live acceptance suite
@live
def test_scenarios_s0_to_s7_live():
    p = subprocess.run([sys.executable, "-m", "scenarios.run_all"], cwd=ROOT, capture_output=True, text=True,
                       timeout=900)
    print(p.stdout[-3000:])
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
