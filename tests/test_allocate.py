import json
from datetime import date, datetime, timezone
from pathlib import Path

from aimo.allocate import allocate, units_for
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
