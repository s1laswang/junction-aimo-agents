import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aimo.allocate import allocate, options, reallocate, stockout_plan, units_for
from aimo.models import NeedItem, Offer, Product, UnitSize

FX = Path(__file__).parent / "fixtures"
AV = {p["productId"]: p for p in json.loads((FX / "availability.json").read_text())["products"]}
PR = {p["productId"]: p for p in json.loads((FX / "price.json").read_text())["products"]}
NOW = datetime(2026, 11, 13, 22, 57, tzinfo=timezone.utc)
D = date(2026, 11, 16)
ITEM = NeedItem(query="lactose free milk", quantity=1000, unit="L")
UNIT = {"ST": "ST", "KI": "KI", "PRK": "PRK"}


def offer(pid, size=None, line=0, unit_size=True):
    a = AV[pid]
    sl = a["scheduleLines"][line]
    price = PR[pid]["catalogPrice"]["value"]
    tax = PR[pid]["tax"]["taxRate"]
    us = UnitSize(value=size, unit="L") if size and unit_size else None
    return Offer(
        product_id=pid,
        product=Product(product_id=pid, name=f"p{pid}", sales_unit=sl["salesUnit"], unit_size=us),
        units_needed=1,
        unit_price_net=price,
        total_net=price,
        total_gross=round(price * (1 + tax / 100), 2),
        available_amount=sl["availableAmount"],
        available_date=date.fromisoformat(sl["availableDate"]),
        closing_time=a["closingTime"],
        sales_block="blocked" if sl["salesBlock"] or a["salesBlock"] else None,
    )


def test_units_for_litre_packs():
    assert units_for(offer("464073", 1).product, 1000, "L") == 1000
    assert units_for(offer("464073", 1).product, 2.5, "l") == 3
    assert units_for(offer("464179", 10).product, 1000, "L") == 100
    assert units_for(offer("464073", 1).product, 500, "ml") == 1


@pytest.mark.skipif(
    not (Path(os.environ.get("AIMO_DATA_DIR", "data")) / "valio_aimo_junction26_product_data.json").exists(),
    reason="no data",
)
def test_units_for_from_data_file():
    p = offer("464201", unit_size=False).product
    assert units_for(p, 1000, "L") == 1000
    assert units_for(offer("464073", 1).product, 5, "kg") is None


def test_single_product_covers():
    out, short = allocate(ITEM, [offer("464201", 1)], D, NOW)
    assert short == 0.0
    assert len(out) == 1 and out[0].units_needed == 1000
    assert out[0].total_net == round(1000 * 1.56, 2)


def test_split_across_products_reports_shortfall():
    out, short = allocate(ITEM, [offer("462775", 1), offer("463380", 1)], D, NOW)
    assert [o.units_needed for o in out] == [51, 698]
    assert short == 1000 - 51 - 698


def test_split_then_unlimited_covers():
    out, short = allocate(ITEM, [offer("462775", 1), offer("463380", 1), offer("464201", 1)], D, NOW)
    assert [o.units_needed for o in out] == [51, 698, 251]
    assert short == 0.0


def test_closed_cutoff_skipped_with_note():
    out, short = allocate(ITEM, [offer("464119", 10), offer("464201", 1)], D, NOW)
    assert [o.product_id for o in out] == ["464201"]
    assert any("464119" in n and "cutoff" in n for n in out[0].notes)


def test_after_deliver_by_and_unknown_date_skipped():
    late = offer("464201", 1).model_copy(update={"available_date": date(2026, 11, 20)})
    unk = offer("464073", 1).model_copy(update={"available_date": None})
    out, short = allocate(ITEM, [late, unk], D, NOW)
    assert out == [] and short == 1000


def test_sales_block_skipped():
    b = offer("464201", 1).model_copy(update={"sales_block": "X"})
    out, short = allocate(ITEM, [b, offer("464073", 1)], D, NOW)
    assert [o.product_id for o in out] == ["464073"] and short == 0.0


def test_case_pack_rounds_up():
    out, short = allocate(ITEM, [offer("464179", 10)], D, NOW)
    assert out == [] and short == 1000
    out, short = allocate(ITEM, [offer("464179", 10).model_copy(update={"closing_time": None})], D, NOW)
    assert out[0].units_needed == 100 and short == 0.0


def test_two_schedule_lines_same_product():
    a = offer("464201", 1).model_copy(update={"available_amount": 400})
    b = offer("464201", 1).model_copy(update={"available_amount": 9999, "available_date": date(2026, 11, 17)})
    out, short = allocate(ITEM, [a, b], date(2026, 11, 17), NOW)
    assert [o.units_needed for o in out] == [400, 600] and short == 0.0


def test_reallocate_stock_drop_refills_from_next():
    fresh = [offer("462775", 1), offer("463380", 1), offer("464201", 1)]
    prev, short = allocate(ITEM, fresh, D, NOW)
    fresh[1] = fresh[1].model_copy(update={"available_amount": 300})
    out, short, ch = reallocate(ITEM, prev, fresh, D, NOW)
    assert {o.product_id: o.units_needed for o in out} == {"462775": 51, "463380": 300, "464201": 649}
    assert short == 0.0
    assert "463380 p463380: 698 -> 300 units, stock dropped" in ch
    assert "+398 from 464201 p464201 delivered 2026-11-16" in ch


def test_reallocate_blocked_and_shortfall():
    fresh = [offer("462775", 1), offer("463380", 1)]
    prev, _ = allocate(ITEM, fresh, D, NOW)
    fresh[1] = fresh[1].model_copy(update={"sales_block": "Z"})
    out, short, ch = reallocate(ITEM, prev, fresh, D, NOW)
    assert [o.units_needed for o in out] == [51]
    assert short == 949
    assert any("463380: 698 -> 0 units, sales blocked" in c for c in ch)


def test_reallocate_unchanged_is_noop():
    fresh = [offer("462775", 1), offer("463380", 1), offer("464201", 1)]
    prev, _ = allocate(ITEM, fresh, D, NOW)
    out, short, ch = reallocate(ITEM, prev, fresh, D, NOW)
    assert ch == [] and short == 0.0
    assert [o.units_needed for o in out] == [51, 698, 251]


def later(pid, d, amount):
    return offer(pid, 1).model_copy(update={"available_date": d, "available_amount": amount, "closing_time": None})


def test_options_fully_covered_is_empty():
    assert options(ITEM, [offer("464201", 1)], D, NOW) == []


def test_options_partial_only():
    o = options(ITEM, [offer("462775", 1), offer("463380", 1)], D, NOW)
    assert [x["kind"] for x in o] == ["partial"]
    assert o[0]["covered"] == 749 and o[0]["shortfall"] == 251


def test_options_late_and_split():
    offers = [offer("462775", 1), offer("463380", 1), later("464201", date(2026, 11, 18), 9999)]
    o = {x["kind"]: x for x in options(ITEM, offers, D, NOW)}
    assert o["partial"]["shortfall"] == 251
    assert o["late"]["date"] == date(2026, 11, 18) and o["late"]["days_late"] == 2
    assert o["split"]["on_time"] == 749 and o["split"]["later"] == 251
    assert o["split"]["date"] == date(2026, 11, 18)
    assert "251" in o["split"]["why"]


def test_options_nothing_on_time_gives_late_only():
    o = options(ITEM, [later("464201", date(2026, 11, 17), 9999)], D, NOW)
    assert [x["kind"] for x in o] == ["late"] and o[0]["days_late"] == 1
    assert "1 day after" in o[0]["why"]


def test_options_never_covers():
    o = options(ITEM, [offer("462775", 1)], D, NOW)
    assert [x["kind"] for x in o] == ["partial"]


def test_stockout_plan_swaps_to_next_offer():
    from aimo.models import QuoteLine
    fresh = [offer("464201", 1).model_copy(update={"available_amount": 0}), offer("464073", 1)]
    line = QuoteLine(product_id="464201", name="p", quantity=200, sales_unit="ST", unit_price_net=1.56, line_total_net=312)
    p = stockout_plan([line], fresh, D, NOW)
    assert p["changed"] and p["shortfall"] == 0.0
    assert [(l.product_id, l.quantity) for l in p["lines"]] == [("464073", 200)]
    assert any("464201: 200 -> 0 units" in c for c in p["changes"])


def test_stockout_plan_untouched():
    from aimo.models import QuoteLine
    line = QuoteLine(product_id="464201", name="p", quantity=200, sales_unit="ST", unit_price_net=1.56, line_total_net=312)
    p = stockout_plan([line], [offer("464201", 1)], D, NOW)
    assert not p["changed"] and p["lines"][0].quantity == 200
