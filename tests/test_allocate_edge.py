import json
from datetime import date, datetime, timezone

from aimo import allocate as alloc_mod
from aimo.allocate import allocate, fill, options, reallocate, stockout_plan, units_for
from aimo.models import NeedItem, Offer, Product, QuoteLine, UnitSize

NOW = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)
D = date(2026, 11, 16)


def make_product(pid="p1", name="Milk", sales_unit="ST", size=1.0, unit="l"):
    us = UnitSize(value=size, unit=unit) if size is not None else None
    return Product(product_id=pid, name=name, sales_unit=sales_unit, unit_size=us)


def make_offer(
    pid="p1",
    name="Milk",
    sales_unit="ST",
    size=1.0,
    unit="l",
    amount=100.0,
    avail_date=D,
    closing_time=None,
    sales_block=None,
    price=2.0,
    gross_factor=1.14,
    units_needed=1.0,
    notes=None,
):
    total_net = round(price, 2)
    total_gross = round(price * gross_factor, 2)
    return Offer(
        product_id=pid,
        product=make_product(pid=pid, name=name, sales_unit=sales_unit, size=size, unit=unit),
        units_needed=units_needed,
        unit_price_net=price,
        total_net=total_net,
        total_gross=total_gross,
        available_amount=amount,
        available_date=avail_date,
        closing_time=closing_time,
        sales_block=sales_block,
        notes=list(notes or []),
    )


def test_units_for_volume_units():
    p1 = make_product(size=1.0, unit="l")
    assert units_for(p1, 4, "l") == 4.0
    assert units_for(p1, 2.3, "L") == 3.0
    assert units_for(p1, 2, "ltr") == 2.0
    assert units_for(p1, 10, "dl") == 1.0
    assert units_for(p1, 15, "dl") == 2.0
    assert units_for(p1, 100, "cl") == 1.0
    assert units_for(p1, 205, "cl") == 3.0
    assert units_for(p1, 1000, "ml") == 1.0
    assert units_for(p1, 1001, "ml") == 2.0

    p_small = make_product(size=250.0, unit="ml")
    assert units_for(p_small, 1, "l") == 4.0
    assert units_for(p_small, 5, "dl") == 2.0
    assert units_for(p_small, 50, "cl") == 2.0
    assert units_for(p_small, 600, "ml") == 3.0

    p_tenth = make_product(size=0.1, unit="l")
    assert units_for(p_tenth, 0.3, "l") == 3.0


def test_units_for_mass_units():
    p_half = make_product(size=0.5, unit="kg")
    assert units_for(p_half, 2, "kg") == 4.0
    assert units_for(p_half, 1.2, "kgm") == 3.0
    assert units_for(p_half, 500, "g") == 1.0
    assert units_for(p_half, 750, "g") == 2.0
    assert units_for(p_half, 1000, "grm") == 2.0

    p_gram = make_product(size=200.0, unit="g")
    assert units_for(p_gram, 1, "kg") == 5.0
    assert units_for(p_gram, 450, "g") == 3.0


def test_units_for_matching_sales_unit():
    p_no_size = make_product(sales_unit="PRK", size=None)
    assert units_for(p_no_size, 5, "PRK") == 5.0
    assert units_for(p_no_size, 5.2, " prk ") == 6.0

    p_case = make_product(sales_unit="KG", size=10.0, unit="kg")
    assert units_for(p_case, 3, "kg") == 3.0


def test_units_for_incompatible_and_unknown_units():
    p_vol = make_product(size=1.0, unit="l")
    p_mass = make_product(size=1.0, unit="kg")
    assert units_for(p_vol, 5, "kg") is None
    assert units_for(p_vol, 500, "g") is None
    assert units_for(p_mass, 5, "l") is None
    assert units_for(p_mass, 500, "ml") is None
    assert units_for(p_vol, 2, "tray") is None
    assert units_for(make_product(sales_unit=None, size=1.0, unit="l"), 2, "tray") is None


def test_units_for_zero_missing_and_fallback_data(tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("AIMO_DATA_DIR", str(tmp_path / "missing_dir"))
        alloc_mod._data.cache_clear()
        assert units_for(make_product(size=0.0, unit="l"), 5, "l") is None
        assert units_for(make_product(size=0.0, unit="kg"), 5, "kg") is None
        assert units_for(make_product(size=-1.0, unit="l"), 5, "l") is None
        assert units_for(make_product(size=None), 5, "l") is None
        assert units_for(make_product(size=1.0, unit="unknown"), 5, "l") is None
    alloc_mod._data.cache_clear()

    sample = [
        {
            "id": "vol_case",
            "salesUnit": "KI",
            "units": [{"unitId": "KI", "sizeInBaseUnits": 6}],
            "synkkaData": {"unitConversions": [{"volume": {"value": 2.0, "unit": "l"}}]},
        },
        {
            "id": "mass_case",
            "salesUnit": "KI",
            "units": [{"unitId": "KI", "sizeInBaseUnits": 4}],
            "synkkaData": {"unitConversions": [{"netWeight": {"value": 500.0, "unit": "g"}}]},
        },
        {
            "id": "incomplete",
            "salesUnit": "KI",
            "units": [],
            "synkkaData": {"unitConversions": [{"volume": {"value": 1.0, "unit": "l"}}]},
        },
        {
            "id": "bad_unit",
            "salesUnit": "KI",
            "units": [{"unitId": "KI", "sizeInBaseUnits": 2}],
            "synkkaData": {"unitConversions": [{"netWeight": {"value": 1.0, "unit": "stones"}}]},
        },
    ]
    (tmp_path / "valio_aimo_junction26_product_data.json").write_text(json.dumps(sample))
    with monkeypatch.context() as m:
        m.setenv("AIMO_DATA_DIR", str(tmp_path))
        alloc_mod._data.cache_clear()
        assert units_for(make_product(pid="vol_case", sales_unit="KI", size=None), 24, "l") == 2.0
        assert units_for(make_product(pid="mass_case", sales_unit="KI", size=None), 3, "kg") == 2.0
        assert units_for(make_product(pid="incomplete", sales_unit="KI", size=None), 5, "l") is None
        assert units_for(make_product(pid="bad_unit", sales_unit="KI", size=None), 5, "kg") is None

    dir_b = tmp_path / "dir_b"
    dir_b.mkdir()
    sample_b = [
        {
            "id": "vol_case",
            "salesUnit": "KI",
            "units": [{"unitId": "KI", "sizeInBaseUnits": 8}],
            "synkkaData": {"unitConversions": [{"volume": {"value": 1.0, "unit": "l"}}]},
        }
    ]
    (dir_b / "valio_aimo_junction26_product_data.json").write_text(json.dumps(sample_b))
    with monkeypatch.context() as m:
        m.setenv("AIMO_DATA_DIR", str(dir_b))
        assert units_for(make_product(pid="vol_case", sales_unit="KI", size=None), 24, "l") == 3.0
        assert units_for(make_product(pid="mass_case", sales_unit="KI", size=None), 3, "kg") is None
    with monkeypatch.context() as m:
        m.setenv("AIMO_DATA_DIR", str(tmp_path / "missing_again"))
        assert units_for(make_product(pid="vol_case", sales_unit="KI", size=None), 24, "l") is None
    alloc_mod._data.cache_clear()


def test_fill_and_allocate_exact_coverage():
    item = NeedItem(query="milk", quantity=20, unit="l")
    o = make_offer(pid="p1", size=2.0, unit="l", amount=15, price=3.0, gross_factor=1.14)
    out, short, skipped = fill(item, [o], D, NOW)
    assert short == 0.0
    assert skipped == []
    assert len(out) == 1
    assert out[0].units_needed == 10.0
    assert out[0].total_net == 30.0
    assert out[0].total_gross == 34.2

    free = make_offer(pid="p_free", size=1.0, unit="l", amount=20, price=0.0, gross_factor=1.14)
    alloc, short2 = allocate(item, [free], D, datetime(2026, 11, 13, 18, 0))
    assert short2 == 0.0
    assert alloc[0].total_net == 0.0
    assert alloc[0].total_gross == 0.0


def test_fill_and_allocate_case_pack_rounding_up():
    item = NeedItem(query="milk", quantity=25, unit="l")
    pack = make_offer(pid="case10", size=10.0, unit="l", amount=10, price=12.0)
    extra = make_offer(pid="unused", size=1.0, unit="l", amount=50, sales_block="hold")
    out, short, skipped = fill(item, [pack, extra], D, NOW)
    assert short == 0.0
    assert skipped == []
    assert len(out) == 1
    assert out[0].units_needed == 3.0

    alloc, alloc_short = allocate(item, [pack, extra], D)
    assert alloc_short == 0.0
    assert alloc[0].units_needed == 3.0
    assert alloc[0].notes == []


def test_fill_and_allocate_split_across_two_offers():
    item = NeedItem(query="milk", quantity=100, unit="l")
    o1 = make_offer(pid="p1", size=1.0, unit="l", amount=40, price=1.5)
    o2 = make_offer(pid="p2", size=2.0, unit="l", amount=20, price=2.8)
    out, short = allocate(item, [o1, o2], D, NOW)
    assert [o.product_id for o in out] == ["p1", "p2"]
    assert [o.units_needed for o in out] == [40.0, 20.0]
    assert short == 20.0

    o3 = make_offer(pid="p3", size=2.0, unit="l", amount=30, price=2.8)
    out_full, short_full = allocate(item, [o1, o3], D, NOW)
    assert [o.units_needed for o in out_full] == [40.0, 30.0]
    assert short_full == 0.0


def test_fill_and_allocate_skips_and_notes():
    item = NeedItem(query="milk", quantity=10, unit="l")
    bad = [
        make_offer(pid="b_block", name="Blocked", sales_block="QA"),
        make_offer(pid="b_nodate", name="NoDate", avail_date=None),
        make_offer(pid="b_late", name="Late", avail_date=date(2026, 11, 20)),
        make_offer(pid="b_cut1", name="CutPast", closing_time="2026-11-13T12:00:00+00:00"),
        make_offer(pid="b_cut2", name="CutExactNaive", closing_time="2026-11-13T18:00:00"),
        make_offer(pid="b_zero", name="Zero", amount=0),
        make_offer(pid="b_neg", name="Neg", amount=-4),
        make_offer(pid="b_dim", name="Cheese", size=1.0, unit="kg", amount=10),
        make_offer(pid="b_frac", name="Frac", amount=0.75),
    ]
    good1 = make_offer(
        pid="g1",
        name="Good1",
        amount=6,
        closing_time="2026-11-14T12:00:00+00:00",
        notes=["preexisting"],
    )
    good2 = make_offer(pid="g2", name="Good2", amount=10, closing_time="not-iso")

    out_f, short_f, skipped = fill(item, bad + [good1, good2], D, NOW)
    assert short_f == 0.0
    assert [o.units_needed for o in out_f] == [6.0, 4.0]
    assert len(skipped) == 9
    assert "sales blocked: QA" in skipped[0]
    assert "availability date unknown" in skipped[1]
    assert "available 2026-11-20, after deliver_by 2026-11-16" in skipped[2]
    assert "order cutoff 2026-11-13T12:00:00+00:00 already passed" in skipped[3]
    assert "order cutoff 2026-11-13T18:00:00 already passed" in skipped[4]
    assert "no stock" in skipped[5]
    assert "no stock" in skipped[6]
    assert "cannot convert l to ST" in skipped[7]
    assert "stock below one sales unit" in skipped[8]

    out_a, short_a = allocate(item, bad + [good1, good2], D, NOW)
    assert short_a == 0.0
    assert out_a[0].notes == ["preexisting"] + skipped
    assert out_a[1].notes == []
    assert good1.notes == ["preexisting"]

    empty_out, empty_short = allocate(item, bad, D, NOW)
    assert empty_out == []
    assert empty_short == 10.0


def test_reallocate_stock_drop_refilled_from_next():
    item = NeedItem(query="milk", quantity=100, unit="l")
    p1 = make_offer(pid="p1", name="Primary", amount=100, price=1.5)
    p2 = make_offer(pid="p2", name="Backup", amount=100, price=1.6)
    prev, _ = allocate(item, [p1, p2], D, NOW)

    fresh = [p1.model_copy(update={"available_amount": 35}), p2]
    out, short, changes = reallocate(item, prev, fresh, D, NOW)
    assert short == 0.0
    assert {o.product_id: o.units_needed for o in out} == {"p1": 35.0, "p2": 65.0}
    assert "p1 Primary: 100 -> 35 units, stock dropped" in changes
    assert "+65 from p2 Backup delivered 2026-11-16" in changes


def test_reallocate_primary_no_longer_offered():
    item = NeedItem(query="milk", quantity=80, unit="l")
    p1 = make_offer(pid="p1", name="Primary", amount=100, price=1.5)
    p2 = make_offer(pid="p2", name="Backup", amount=100, price=1.6)
    prev, _ = allocate(item, [p1], D, NOW)

    out, short, changes = reallocate(item, prev, [p2], D, NOW)
    assert short == 0.0
    assert len(out) == 1
    assert out[0].product_id == "p2" and out[0].units_needed == 80.0
    assert "p1 Primary: 80 -> 0 units, no longer offered" in changes
    assert "+80 from p2 Backup delivered 2026-11-16" in changes

    out_b, short_b, changes_b = reallocate(item, prev, [p1.model_copy(update={"sales_block": "recall"}), p2], D, NOW)
    assert short_b == 0.0
    assert "p1 Primary: 80 -> 0 units, sales blocked: recall" in changes_b
    assert out_b[0].product_id == "p2" and out_b[0].units_needed == 80.0


def test_reallocate_shortfall_and_merge_into_kept():
    item = NeedItem(query="milk", quantity=100, unit="l")
    p1 = make_offer(pid="p1", name="Primary", amount=100, price=1.5)
    p2 = make_offer(pid="p2", name="Backup", amount=30, price=1.6)
    prev, _ = allocate(item, [p1], D, NOW)

    fresh_short = [p1.model_copy(update={"available_amount": 20}), p2]
    out, short, changes = reallocate(item, prev, fresh_short, D, NOW)
    assert {o.product_id: o.units_needed for o in out} == {"p1": 20.0, "p2": 30.0}
    assert short == 50.0
    assert changes[-1] == "shortfall 50 l"

    prev_split, _ = allocate(
        item,
        [p1.model_copy(update={"available_amount": 40}), p2.model_copy(update={"available_amount": 100})],
        D,
        NOW,
    )
    fresh_merge = [
        p1.model_copy(update={"available_amount": 0.4}),
        p2.model_copy(update={"available_amount": 100}),
    ]
    out_m, short_m, changes_m = reallocate(item, prev_split, fresh_merge, D, NOW)
    assert short_m == 0.0
    assert len(out_m) == 1
    assert out_m[0].product_id == "p2" and out_m[0].units_needed == 100.0
    assert out_m[0].total_net == 160.0
    assert "p1 Primary: 40 -> 0 units, stock dropped" in changes_m
    assert "+40 from p2 Backup delivered 2026-11-16" in changes_m


def test_options_partial_and_empty_when_covered():
    item = NeedItem(query="milk", quantity=100, unit="l")
    assert options(item, [make_offer(amount=100)], D, NOW) == []

    res = options(
        item,
        [make_offer(pid="p1", amount=40), make_offer(pid="p2", amount=10, avail_date=date(2026, 11, 18))],
        D,
        NOW,
    )
    assert len(res) == 1
    assert res[0] == {
        "kind": "partial",
        "date": D,
        "covered": 40.0,
        "shortfall": 60.0,
        "why": "40 l of 100 l by 2026-11-16, 60 l short",
    }


def test_options_late_pluralization():
    item = NeedItem(query="milk", quantity=100, unit="l")
    one_day = options(item, [make_offer(amount=100, avail_date=date(2026, 11, 17))], D, NOW)
    assert [x["kind"] for x in one_day] == ["late"]
    assert one_day[0]["days_late"] == 1
    assert one_day[0]["why"] == "all 100 l available by 2026-11-17, 1 day after 2026-11-16"

    four_days = options(item, [make_offer(amount=100, avail_date=date(2026, 11, 20))], D, NOW)
    assert [x["kind"] for x in four_days] == ["late"]
    assert four_days[0]["days_late"] == 4
    assert four_days[0]["why"] == "all 100 l available by 2026-11-20, 4 days after 2026-11-16"


def test_options_split_one_and_multiple_days():
    item = NeedItem(query="milk", quantity=100, unit="l")
    res_1 = {
        x["kind"]: x
        for x in options(
            item,
            [make_offer(pid="p1", amount=60, avail_date=D), make_offer(pid="p2", amount=40, avail_date=date(2026, 11, 17))],
            D,
            NOW,
        )
    }
    assert set(res_1) == {"partial", "late", "split"}
    assert res_1["split"]["date"] == date(2026, 11, 17)
    assert res_1["split"]["on_time"] == 60.0
    assert res_1["split"]["later"] == 40.0
    assert res_1["split"]["days_late"] == 1
    assert res_1["split"]["why"] == "60 l by 2026-11-16, remaining 40 l on 2026-11-17, 1 day late"

    res_3 = {
        x["kind"]: x
        for x in options(
            item,
            [
                make_offer(pid="p1", amount=60, avail_date=D),
                make_offer(pid="p2", amount=15, avail_date=date(2026, 11, 18)),
                make_offer(pid="p3", amount=25, avail_date=date(2026, 11, 19)),
            ],
            D,
            NOW,
        )
    }
    assert res_3["late"]["date"] == date(2026, 11, 19)
    assert res_3["late"]["days_late"] == 3
    assert "3 days after" in res_3["late"]["why"]
    assert res_3["split"]["date"] == date(2026, 11, 19)
    assert res_3["split"]["days_late"] == 3
    assert res_3["split"]["why"] == "60 l by 2026-11-16, remaining 40 l on 2026-11-19, 3 days late"


def test_stockout_plan_unchanged_and_changed():
    l1 = QuoteLine(product_id="p1", name="Milk", quantity=30, sales_unit="PRK", unit_price_net=1.5, line_total_net=45.0)
    l2 = QuoteLine(product_id="p2", name="Butter", quantity=10, sales_unit="KI", unit_price_net=4.0, line_total_net=40.0)
    fresh_ok = [
        make_offer(pid="p1", name="Milk", sales_unit="PRK", size=1.0, unit="l", amount=50, price=1.5),
        make_offer(pid="p2", name="Butter", sales_unit="KI", size=1.0, unit="kg", amount=20, price=4.0),
    ]
    plan_ok = stockout_plan([l1, l2], fresh_ok, D, NOW)
    assert plan_ok["changed"] is False
    assert plan_ok["shortfall"] == 0.0
    assert plan_ok["changes"] == []
    assert plan_ok["options"] == []
    assert [(x.product_id, x.quantity) for x in plan_ok["lines"]] == [("p1", 30.0), ("p2", 10.0)]

    fresh_swap = [
        make_offer(pid="p1", name="Milk", sales_unit="PRK", size=1.0, unit="l", amount=12, price=1.5),
        make_offer(pid="p1_alt", name="Milk Alt", sales_unit="PRK", size=1.0, unit="l", amount=50, price=1.6),
    ]
    plan_swap = stockout_plan([l1], fresh_swap, D, NOW)
    assert plan_swap["changed"] is True
    assert plan_swap["shortfall"] == 0.0
    assert plan_swap["options"] == []
    assert [(x.product_id, x.quantity) for x in plan_swap["lines"]] == [("p1", 12.0), ("p1_alt", 18.0)]

    gone_line = QuoteLine(product_id="gone", name=None, quantity=10, sales_unit=None, unit_price_net=2.0, line_total_net=20.0)
    plan_gone = stockout_plan([gone_line], [], D, NOW)
    assert plan_gone["changed"] is True
    assert plan_gone["shortfall"] == 10.0
    assert "gone : 10 -> 0 units, no longer offered" in plan_gone["changes"]


def test_stockout_plan_shortfall_accumulation_and_options():
    l1 = QuoteLine(product_id="p1", name="Milk", quantity=50, sales_unit="PRK", unit_price_net=1.5, line_total_net=75.0)
    l2 = QuoteLine(product_id="p2", name="Butter", quantity=40, sales_unit=None, unit_price_net=4.0, line_total_net=160.0)
    fresh = [
        make_offer(pid="p1", name="Milk", sales_unit="PRK", size=1.0, unit="l", amount=20, avail_date=D, price=1.5),
        make_offer(pid="p1", name="Milk", sales_unit="PRK", size=1.0, unit="l", amount=30, avail_date=date(2026, 11, 18), price=1.5),
        make_offer(pid="p2", name="Butter", sales_unit="KI", size=1.0, unit="kg", amount=15, avail_date=D, price=4.0),
        make_offer(pid="p2", name="Butter", sales_unit="KI", size=1.0, unit="kg", amount=25, avail_date=date(2026, 11, 17), price=4.0),
    ]
    plan = stockout_plan([l1, l2], fresh, D, NOW)
    assert plan["changed"] is True
    assert plan["shortfall"] == 55.0
    assert [(x.product_id, x.quantity) for x in plan["lines"]] == [("p1", 20.0), ("p2", 15.0)]

    by_pid_kind = {(o["product_id"], o["kind"]): o for o in plan["options"]}
    assert set(by_pid_kind) == {
        ("p1", "partial"),
        ("p1", "late"),
        ("p1", "split"),
        ("p2", "partial"),
        ("p2", "late"),
        ("p2", "split"),
    }
    assert by_pid_kind[("p1", "split")]["later"] == 30.0
    assert by_pid_kind[("p1", "split")]["days_late"] == 2
    assert by_pid_kind[("p2", "split")]["later"] == 25.0
    assert by_pid_kind[("p2", "split")]["days_late"] == 1
