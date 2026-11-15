from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import aimo.catalog as cat
from aimo import config
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import clock, orders as store, replay
from aimo.buyer import clarify
from aimo.buyer.eval_gold import load_gold
from aimo.buyer.fakes import FakeGateway
from aimo.buyer.need import (
    HELSINKI,
    detect_allergens,
    detect_claims,
    guess_query,
    now_helsinki,
    parse_need,
    parse_quantity,
    parse_request,
    resolve_date,
)
from aimo.buyer.profiles import get_profile
from aimo.catalog import textsearch
from aimo.gateway import service
from aimo.models import AllergenStatus

ROOT = Path(__file__).resolve().parents[1]
GOLD_20_PATH = ROOT / "tests" / "fixtures" / "gold" / "gold-buyer-20.jsonl"
SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)


def test_now_helsinki_respects_aimo_now_and_utc_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T22:30:00+00:00")
    dt = now_helsinki()
    assert dt.tzinfo == HELSINKI
    assert dt == datetime(2026, 11, 15, 0, 30, tzinfo=HELSINKI)

    assert resolve_date("by Monday")[0] == date(2026, 11, 16)
    assert resolve_date("maanantaiksi")[0] == date(2026, 11, 16)
    assert resolve_date("till måndag")[0] == date(2026, 11, 16)
    assert resolve_date("keskiviikoksi")[0] == date(2026, 11, 18)

    need_fi = parse_need("Tarvitaan 300 litraa laktoositonta maitoa maanantaiksi.", "hotel-aurora", use_gemini=False)
    assert need_fi.delivery_by == date(2026, 11, 16)
    assert "delivery_date_assumed" not in need_fi.preferences

    monkeypatch.setenv("AIMO_NOW", "2026-11-16T09:00:00+02:00")
    assert now_helsinki().date() == date(2026, 11, 16)
    assert resolve_date("by Monday")[0] == date(2026, 11, 23)
    assert resolve_date("keskiviikoksi")[0] == date(2026, 11, 18)


def test_now_helsinki_respects_aimo_replay_and_request_header_and_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_REPLAY", "1")

    dt_bundled = now_helsinki()
    assert dt_bundled.tzinfo == HELSINKI
    assert dt_bundled.date() == date(2026, 11, 14)
    assert resolve_date("by Monday")[0] == date(2026, 11, 16)
    assert resolve_date("till måndag")[0] == date(2026, 11, 16)
    assert resolve_date("keskiviikoksi")[0] == date(2026, 11, 18)

    custom_snap = tmp_path / "snap"
    custom_snap.mkdir()
    (custom_snap / "meta.json").write_text(
        json.dumps({"time_now": "2026-11-15T11:15:00+02:00", "dates": ["2026-11-16"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIMO_REPLAY", str(custom_snap))
    dt_custom = now_helsinki()
    assert dt_custom == datetime(2026, 11, 15, 11, 15, tzinfo=HELSINKI)
    assert resolve_date("maanantaiksi")[0] == date(2026, 11, 16)

    tok = clock.set_request_now("2026-11-18T08:00:00Z")
    try:
        assert now_helsinki() == datetime(2026, 11, 18, 10, 0, tzinfo=HELSINKI)
    finally:
        clock.reset_request_now(tok)

    def broken_clock() -> datetime:
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(clock, "now", broken_clock)
    fallback_dt = now_helsinki()
    assert fallback_dt.tzinfo == HELSINKI
    assert isinstance(fallback_dt, datetime)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("120 litraa kaurajuomaa", (120.0, "L")),
        ("1 litra maitoa", (1.0, "L")),
        ("300 liter laktosfri mjölk", (300.0, "L")),
        ("5 desilitraa kermaa", (0.5, "L")),
        ("250 millilitraa kermaa", (0.25, "L")),
        ("30 kiloa voita", (30.0, "KG")),
        ("1 kilo voita", (1.0, "KG")),
        ("15 kilogrammaa jauhoja", (15.0, "KG")),
        ("20 kilogram smör", (20.0, "KG")),
        ("500 grammaa voita", (0.5, "KG")),
        ("750 gram ost", (0.75, "KG")),
        ("2,5 kiloa juustoa", (2.5, "KG")),
        ("1 000 litraa maitoa", (1000.0, "L")),
        ("1.000,5 liter mjölk", (1000.5, "L")),
        ("24 kpl ruisleipää", (24.0, "PCS")),
        ("12 kappaletta leipää", (12.0, "PCS")),
        ("1 kappale kakku", (1.0, "PCS")),
        ("20 st yoghurt", (20.0, "PCS")),
        ("1 st bröd", (1.0, "PCS")),
        ("15 stycken bröd", (15.0, "PCS")),
        ("8 styck ost", (8.0, "PCS")),
        ("10 pkt voita", (10.0, "PCS")),
        ("6 pakkausta kahvia", (6.0, "PCS")),
        ("1 pakkaus voita", (1.0, "PCS")),
        ("20 purkkia jogurttia", (20.0, "PCS")),
        ("1 purkki rahkaa", (1.0, "PCS")),
        ("30 tölkkiä kaurajuomaa", (30.0, "PCS")),
        ("30 tolkkia kaurajuomaa", (30.0, "PCS")),
        ("4 laatikkoa maitoa", (4.0, "PCS")),
        ("1 laatikko voita", (1.0, "PCS")),
        ("10 förp smör", (10.0, "PCS")),
        ("1 förpackning smör", (1.0, "PCS")),
        ("12 förpackningar havredryck", (12.0, "PCS")),
    ],
)
def test_multilingual_quantities_and_units(text: str, expected: tuple[float, str]) -> None:
    got = parse_quantity(text)
    assert got is not None
    assert abs(got[0] - expected[0]) < 1e-9
    assert got[1] == expected[1]


def test_ordinal_dates_do_not_false_match_swedish_st_unit() -> None:
    assert parse_quantity("by 1st November") is None
    assert parse_quantity("by 21st November") is None
    need = parse_need("We need 100 litres of lactose-free milk by 21st November.", "hotel-aurora", SAT, use_gemini=False)
    assert need.quantity == 100.0
    assert need.unit == "L"
    assert need.delivery_by == date(2026, 11, 21)


@pytest.mark.parametrize("bad_text", ["Tilaa 0 litraa maitoa maanantaiksi.", "Vi behöver -5 liter mjölk till måndag."])
def test_non_positive_multilingual_quantities_fail_validation(bad_text: str) -> None:
    with pytest.raises(ValidationError):
        parse_need(bad_text, "hotel-aurora", SAT, use_gemini=False)


@pytest.mark.parametrize(
    "phrase,expected_date",
    [
        ("maanantaiksi", date(2026, 11, 16)),
        ("tiistaiksi", date(2026, 11, 17)),
        ("keskiviikoksi", date(2026, 11, 18)),
        ("torstaiksi", date(2026, 11, 19)),
        ("perjantaiksi", date(2026, 11, 20)),
        ("lauantaiksi", date(2026, 11, 21)),
        ("sunnuntaiksi", date(2026, 11, 15)),
        ("keskiviikkona", date(2026, 11, 18)),
        ("keskiviikkoon mennessä", date(2026, 11, 18)),
        ("keskiviikolle", date(2026, 11, 18)),
        ("maanantaihin mennessä", date(2026, 11, 16)),
        ("maanantaille", date(2026, 11, 16)),
        ("ensi viikon keskiviikoksi", date(2026, 11, 18)),
        ("ensi viikolla", date(2026, 11, 16)),
        ("tänään", date(2026, 11, 14)),
        ("huomenna", date(2026, 11, 15)),
        ("huomiseksi", date(2026, 11, 15)),
        ("ylihuomenna", date(2026, 11, 16)),
        ("på måndag", date(2026, 11, 16)),
        ("till måndag", date(2026, 11, 16)),
        ("senast måndag", date(2026, 11, 16)),
        ("till tisdag", date(2026, 11, 17)),
        ("på onsdag", date(2026, 11, 18)),
        ("senast torsdag", date(2026, 11, 19)),
        ("till fredag", date(2026, 11, 20)),
        ("på lördag", date(2026, 11, 21)),
        ("på söndag", date(2026, 11, 15)),
        ("nästa vecka", date(2026, 11, 16)),
        ("idag", date(2026, 11, 14)),
        ("i dag", date(2026, 11, 14)),
        ("imorgon", date(2026, 11, 15)),
        ("i morgon", date(2026, 11, 15)),
        ("i övermorgon", date(2026, 11, 16)),
    ],
)
def test_multilingual_weekday_and_relative_dates(phrase: str, expected_date: date) -> None:
    resolved = resolve_date(phrase, SAT)
    assert resolved is not None
    assert resolved[0] == expected_date


def test_multilingual_claims_and_coordinated_allergens() -> None:
    assert detect_claims("laktoositonta maitoa") == ["FREE_FROM_LACTOSE"]
    assert detect_claims("laktoosittomia tuotteita") == ["FREE_FROM_LACTOSE"]
    assert detect_claims("laktosfritt smör") == ["FREE_FROM_LACTOSE"]
    assert detect_claims("vähälaktoosista kermaa") == ["LOW_LACTOSE"]
    assert detect_claims("laktosreducerad grädde") == ["LOW_LACTOSE"]
    assert detect_claims("gluteenitonta leipää") == ["FREE_FROM_GLUTEN"]
    assert detect_claims("gluteenittomia sämpylöitä") == ["FREE_FROM_GLUTEN"]
    assert detect_claims("glutenfritt rågbröd") == ["FREE_FROM_GLUTEN"]
    assert detect_claims("keliaakikoille sopiva") == ["FREE_FROM_GLUTEN"]
    assert detect_claims("vegaanista juustoa") == ["VEGAN"]
    assert detect_claims("vegansk ost") == ["VEGAN"]
    assert detect_claims("kasvisruokailijoille") == ["VEGETARIAN"]
    assert detect_claims("vegetarisk lunch") == ["VEGETARIAN"]
    assert detect_claims("luomumaitoa") == ["ORGANIC"]
    assert detect_claims("ekologisk mjölk") == ["ORGANIC"]
    assert detect_claims("kananmunatonta ja munatonta") == ["FREE_FROM_EGGS"]
    assert detect_claims("äggfri majonnäs") == ["FREE_FROM_EGGS"]
    assert detect_claims("soijatonta jogurttia") == ["FREE_FROM_SOY"]
    assert detect_claims("sojafri dryck") == ["FREE_FROM_SOY"]
    assert detect_claims("sokeritonta mehua, ei lisättyä sokeria") == ["NO_ADDED_SUGARS"]
    assert detect_claims("sockerfri dryck utan tillsatt socker") == ["NO_ADDED_SUGARS"]

    assert detect_allergens("oppilailla maito- ja pähkinäallergia") == ["milk", "nuts"]
    assert detect_allergens("elever har mjölk- och nötallergi") == ["milk", "nuts"]
    assert detect_allergens("maito-, kananmuna- ja pähkinäallergia") == ["milk", "eggs", "nuts"]
    assert detect_allergens("mjölk-, ägg- och jordnötsallergi") == ["milk", "eggs", "peanuts"]
    assert detect_allergens("allergisk mot mjölk och nötter") == ["milk", "nuts"]
    assert detect_allergens("maidoton ja pähkinätön ruokavalio") == ["milk", "nuts"]
    assert detect_allergens("mjölkfri och nötfri kost") == ["milk", "nuts"]
    assert detect_allergens("maapähkinäallergia") == ["peanuts"]
    assert detect_allergens("jordnötsallergi") == ["peanuts"]
    assert detect_allergens("kala-allergia ja soija-allergia") == ["fish", "soy"]
    assert detect_allergens("fiskallergi och sojaallergi") == ["fish", "soy"]
    assert detect_allergens("seesamiallergia, selleriallergia ja sinappiallergia") == ["sesame", "celery", "mustard"]
    assert detect_allergens("sesamallergi och senapsallergi") == ["sesame", "mustard"]


@pytest.mark.parametrize(
    "text,expected_query",
    [
        ("Tarvitsemme 120 litraa kaurajuomaa keskiviikoksi.", "oat drink"),
        ("Vi behöver 120 liter havredryck senast onsdag.", "oat drink"),
        ("Pyydän 40 litraa laktoositonta ruokakermaa maanantaiksi koululle.", "lactose-free cooking cream"),
        ("Vi vill gärna beställa 40 liter laktosfri matlagningsgrädde till måndag tack.", "lactose-free cooking cream"),
        ("Saisimmeko 20 litraa vispikermaa perjantaiksi juhlia varten.", "whipping cream"),
        ("Beställ 20 liter kuohukermaa maanantaiksi.", "whipping cream"),
        ("Vi behöver 20 liter vispgrädde på fredag.", "whipping cream"),
        ("Tarvitaan 100 litraa kevytmaitoa maanantaihin mennessä hotellille.", "semi-skimmed milk"),
        ("Vi behöver 100 liter lättmjölk för leverans på måndag.", "semi-skimmed milk"),
        ("Tilataan 80 litraa rasvatonta maitoa tiistaiksi sairaalalle.", "skimmed milk"),
        ("Vi behöver 80 liter fettfri mjölk till tisdag.", "skimmed milk"),
        ("Haluamme 60 litraa täysmaitoa torstaiksi.", "whole milk"),
        ("Vi behöver 60 liter standardmjölk på torsdag.", "whole milk"),
        ("Vi behöver 60 liter helmjölk på torsdag.", "whole milk"),
        ("Tarvitaan 300 litraa laktoositonta maitoa maanantaiksi.", "lactose-free milk"),
        ("Vi behöver 300 liter laktosfri mjölk till måndag.", "lactose-free milk"),
        ("Tilaa 25 kg suolattua voita maanantaiksi.", "salted butter"),
        ("Beställ 25 kg saltat smör till måndag.", "salted butter"),
        ("Tilaa 30 kg voita maanantaiksi.", "butter"),
        ("Beställ 30 kg smör till måndag.", "butter"),
        ("Tarvitsemme 15 kg gluteenitonta ruisleipää keskiviikoksi.", "gluten-free rye bread"),
        ("Vi behöver 15 kg glutenfritt rågbröd till onsdag.", "gluten-free rye bread"),
        ("Tarvitsemme 10 kg luomu jogurttia ja 5 kg juustoa tiistaiksi.", "organic yogurt"),
        ("Vi behöver 10 kg ekologisk yoghurt till tisdag.", "organic yogurt"),
        ("Tarvitsemme 100 litraa maidotonta kaurajuomaa keskiviikoksi.", "dairy-free oat drink"),
        ("Oat drink for a school, pupils have milk and nut allergies, 200 L by Wednesday", "Oat drink"),
        ("We need 1,000 litres of lactose-free milk at Hotel Aurora Helsinki by Monday.", "lactose-free milk"),
        ("2,5 kg grated cheese for 18.11.", "grated cheese"),
    ],
)
def test_product_query_normalization_and_english_preservation(text: str, expected_query: str) -> None:
    assert guess_query(text) == expected_query


def test_multilingual_multi_item_ja_and_och_orders() -> None:
    fi_needs = parse_request(
        "Tarvitsemme 40 litraa ruokakermaa ja 20 kiloa voita maanantaiksi, laktoositonta.",
        "hotel-aurora",
        SAT,
        use_gemini=False,
    )
    assert [(n.product_query, n.quantity, n.unit) for n in fi_needs] == [
        ("cooking cream", 40.0, "L"),
        ("butter", 20.0, "KG"),
    ]
    assert all(n.delivery_by == date(2026, 11, 16) for n in fi_needs)
    assert all(n.required_claims == ["FREE_FROM_LACTOSE"] for n in fi_needs)
    assert all("delivery_date_assumed" not in n.preferences for n in fi_needs)
    assert all(n.preferred_product_ids == [] for n in fi_needs)

    sv_needs = parse_request(
        "Vi behöver 40 liter matlagningsgrädde och 20 kg smör till måndag, laktosfritt.",
        "hotel-aurora",
        SAT,
        use_gemini=False,
    )
    assert [(n.product_query, n.quantity, n.unit) for n in sv_needs] == [
        ("cooking cream", 40.0, "L"),
        ("butter", 20.0, "KG"),
    ]
    assert all(n.delivery_by == date(2026, 11, 16) for n in sv_needs)
    assert all(n.required_claims == ["FREE_FROM_LACTOSE"] for n in sv_needs)
    assert all("delivery_date_assumed" not in n.preferences for n in sv_needs)

    sv_school = parse_request(
        "Vi behöver 120 liter havredryck och 15 kg rågbröd senast onsdag, elever har mjölk- och nötallergi.",
        "school-tampere",
        SAT,
        use_gemini=False,
    )
    assert [(n.product_query, n.quantity, n.unit, n.required_claims) for n in sv_school] == [
        ("oat drink", 120.0, "L", []),
        ("rye bread", 15.0, "KG", []),
    ]
    assert all(n.delivery_by == date(2026, 11, 18) for n in sv_school)
    assert all(set(n.excluded_allergens) == {"milk", "nuts"} for n in sv_school)

    sv_no_comma = parse_need(
        "Vi behöver 120 liter havredryck till onsdag för elever med mjölk- och nötallergi.",
        "school-tampere",
        SAT,
        use_gemini=False,
    )
    assert sv_no_comma.product_query == "oat drink"
    assert sv_no_comma.quantity == 120.0 and sv_no_comma.unit == "L"
    assert sv_no_comma.delivery_by == date(2026, 11, 18)
    assert set(sv_no_comma.excluded_allergens) == {"milk", "nuts"}

    fi_no_comma = parse_need(
        "Tarvitsemme 50 kg gluteenitonta leipää keliaakikoille maanantaiksi.",
        "hospital-meilahti",
        SAT,
        use_gemini=False,
    )
    assert fi_no_comma.product_query == "gluten-free bread"
    assert fi_no_comma.quantity == 50.0 and fi_no_comma.unit == "KG"
    assert fi_no_comma.delivery_by == date(2026, 11, 16)
    assert fi_no_comma.required_claims == ["FREE_FROM_GLUTEN"]
    assert set(fi_no_comma.excluded_allergens) == {"gluten"}


GOLD_20_EXPECTED: dict[str, dict] = {
    "g01": {"items": [(1000.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
    "g02": {"items": [(200.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
    "g03": {"items": [(20.0, "KG", "lactose-free butter", ["FREE_FROM_LACTOSE"], [])], "date": date(2026, 11, 16), "allergens": set()},
    "g04": {"items": [(40.0, "L", "lactose-free cooking cream", ["FREE_FROM_LACTOSE"], [])], "date": date(2026, 11, 16), "allergens": set()},
    "g05": {
        "items": [
            (40.0, "L", "cream", ["FREE_FROM_LACTOSE"], []),
            (20.0, "KG", "butter", ["FREE_FROM_LACTOSE"], []),
        ],
        "date": date(2026, 11, 16),
        "allergens": set(),
    },
    "g06": {"items": [(200.0, "L", "oat drink", [], [])], "date": date(2026, 11, 18), "allergens": {"milk", "nuts"}},
    "g07": {"items": [(120.0, "L", "oat drink", [], [])], "date": date(2026, 11, 18), "allergens": {"milk", "nuts"}},
    "g08": {"items": [(1500.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
    "g09": {"items": [(4000.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
    "g10": {"items": [(300.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
    "g11": {"items": [(300.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
    "g12": {"items": [(40.0, "KG", "salted butter", [], [])], "date": date(2026, 11, 16), "allergens": set()},
    "g13": {"items": [(100.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 18), "allergens": set()},
    "g14": {"items": [(30.0, "KG", "butter", [], [])], "date": date(2026, 11, 16), "allergens": set()},
    "g15": {"items": [(100.0, "L", "oat drink", [], [])], "date": date(2026, 11, 18), "allergens": {"gluten"}},
    "g16": {"items": [(100.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], [])], "date": date(2026, 11, 16), "allergens": {"gluten"}},
    "g17": {"items": [(60.0, "L", "oat drink", [], [])], "date": date(2026, 11, 19), "allergens": {"milk", "nuts"}},
    "g18": {"items": [(500.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
    "g19": {
        "items": [
            (30.0, "L", "lactose-free cream", ["FREE_FROM_LACTOSE"], []),
            (10.0, "KG", "lactose-free butter", ["FREE_FROM_LACTOSE"], []),
        ],
        "date": date(2026, 11, 16),
        "allergens": set(),
    },
    "g20": {"items": [(2000.0, "L", "lactose-free milk", ["FREE_FROM_LACTOSE"], ["463038"])], "date": date(2026, 11, 16), "allergens": set()},
}


def test_all_20_gold_items_deterministic_regex_parse_and_clarify() -> None:
    records = load_gold([str(GOLD_20_PATH)])
    assert len(records) == 20
    assert {r["id"] for r in records} == set(GOLD_20_EXPECTED)

    gw = FakeGateway("hotel-aurora", "hotel-aurora/buyer-1", now=SAT)

    for rec in records:
        gid = rec["id"]
        exp = GOLD_20_EXPECTED[gid]
        needs = parse_request(rec["request"], rec["customer_id"], SAT, use_gemini=False)
        assert len(needs) == len(exp["items"]), gid
        for need, (qty, unit, query, claims, pref_ids) in zip(needs, exp["items"]):
            assert need.customer_id == rec["customer_id"], gid
            assert need.preferences.get("parsed_by") == "regex", gid
            assert "delivery_date_assumed" not in need.preferences, gid
            assert need.quantity == qty, gid
            assert need.unit == unit, gid
            assert need.product_query == query, gid
            assert need.required_claims == claims, gid
            assert need.preferred_product_ids == pref_ids, gid
            assert need.delivery_by == exp["date"], gid
            assert set(need.excluded_allergens) == exp["allergens"], gid

        if gid in ("g07", "g10", "g11", "g14"):
            qs = clarify.find_questions(
                needs,
                get_profile(rec["customer_id"]),
                rec["request"],
                SAT,
                lambda q, c: gw.search_products(q, c),
            )
            assert qs == [], f"{gid} produced spurious clarification questions: {qs}"


def test_clarify_fi_sv_next_week_vs_explicit_weekday() -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    fi_vague = "Tarvitsemme 100 litraa kaurajuomaa ensi viikolla."
    needs_fi_vague = parse_request(fi_vague, prof, SAT, use_gemini=False)
    qs_fi_vague = clarify.find_questions(needs_fi_vague, prof, fi_vague, SAT, search_fn)
    assert [q["type"] for q in qs_fi_vague] == ["date"]

    sv_vague = "Vi behöver 100 liter havredryck nästa vecka."
    needs_sv_vague = parse_request(sv_vague, prof, SAT, use_gemini=False)
    qs_sv_vague = clarify.find_questions(needs_sv_vague, prof, sv_vague, SAT, search_fn)
    assert [q["type"] for q in qs_sv_vague] == ["date"]

    fi_specific = "Tarvitsemme 100 litraa kaurajuomaa ensi viikon keskiviikoksi."
    needs_fi_spec = parse_request(fi_specific, prof, SAT, use_gemini=False)
    assert needs_fi_spec[0].delivery_by == date(2026, 11, 18)
    assert clarify.find_questions(needs_fi_spec, prof, fi_specific, SAT, search_fn) == []

    sv_specific = "Vi behöver 100 liter havredryck nästa vecka på onsdag."
    needs_sv_spec = parse_request(sv_specific, prof, SAT, use_gemini=False)
    assert needs_sv_spec[0].delivery_by == date(2026, 11, 18)
    assert clarify.find_questions(needs_sv_spec, prof, sv_specific, SAT, search_fn) == []


def test_clarify_fi_sv_unit_variant_and_conflict() -> None:
    gw = FakeGateway("cafe-x", "cafe-x/buyer-1", now=SAT)
    search_fn = lambda q, c: gw.search_products(q, c)
    prof = get_profile("cafe-x")

    sv_unit = "Vi behöver 20 förp laktosfri mjölk till måndag."
    needs_sv_unit = parse_request(sv_unit, prof, SAT, use_gemini=False)
    qs_sv_unit = clarify.find_questions(needs_sv_unit, prof, sv_unit, SAT, search_fn)
    assert [q["type"] for q in qs_sv_unit] == ["unit"]

    fi_unit = "Tilaa 20 tölkkiä laktoositonta maitoa maanantaiksi."
    needs_fi_unit = parse_request(fi_unit, prof, SAT, use_gemini=False)
    qs_fi_unit = clarify.find_questions(needs_fi_unit, prof, fi_unit, SAT, search_fn)
    assert [q["type"] for q in qs_fi_unit] == ["unit"]

    sv_variant = "Vi behöver 50 liter laktosfri mjölk till måndag."
    needs_sv_var = parse_request(sv_variant, prof, SAT, use_gemini=False)
    qs_sv_var = clarify.find_questions(needs_sv_var, prof, sv_variant, SAT, search_fn)
    assert [q["type"] for q in qs_sv_var] == ["variant"]

    sv_variant_named = "Vi behöver 50 liter laktosfri lättmjölk till måndag."
    needs_sv_var_named = parse_request(sv_variant_named, prof, SAT, use_gemini=False)
    assert clarify.find_questions(needs_sv_var_named, prof, sv_variant_named, SAT, search_fn) == []

    school_prof = get_profile("school-tampere")
    sv_conflict = "Vi behöver 50 liter mjölk till onsdag."
    needs_sv_conflict = parse_request(sv_conflict, school_prof, SAT, use_gemini=False)
    qs_sv_conflict = clarify.find_questions(needs_sv_conflict, school_prof, sv_conflict, SAT, search_fn)
    assert [q["type"] for q in qs_sv_conflict] == ["conflict"]

    assert clarify.variant_key("Valio Lättmjölk laktosfri") == "semi-skimmed (1.5 %)"
    assert clarify.variant_key("Arla Fettfri mjölkdryck") == "fat-free / skimmed"
    assert clarify.variant_key("Valio Vispgrädde") == "whipping cream"
    assert clarify.variant_key("Valio Matlagningsgrädde") == "cooking cream"


@pytest.fixture(scope="module")
def replay_catalog_env(tmp_path_factory: pytest.TempPathFactory):
    mp = pytest.MonkeyPatch()
    cat_tmp = tmp_path_factory.mktemp("replay-cat")
    orig_source = cat._SOURCE
    prev_con = None
    try:
        if not config.PRODUCT_JSON.exists():
            mp.setattr(config, "DATA_DIR", cat_tmp)
            mp.setattr(config, "PRODUCT_JSON", cat_tmp / "valio_aimo_junction26_product_data.json")
            mp.setattr(config, "RELIABILITY_CSV", cat_tmp / "valio_aimo_junction26_delivery_reliability.csv")
            mp.setattr(config, "CO2_CSV", cat_tmp / "valio_aimo_junction26_carbon_footprint.csv")
            mp.setattr(config, "CATALOG_DB", cat_tmp / "catalog.sqlite")
            mp.setenv("AIMO_OFFLINE", "0")
            mp.setattr(config, "OFFLINE", False)
            mp.setenv("AIMO_API_KEY", "")
            mp.setattr(config, "AIMO_API_KEY", "")
            mp.setenv("AIMO_REPLAY", "1")
            mp.delenv("AIMO_CATALOG_FIXTURE", raising=False)
            src = cat._use_fixture_catalog()
            assert src == "fixture-subset"
            mp.setattr(cat, "_SOURCE", src)
            mp.setattr(catalog_store, "PRODUCT_JSON", config.PRODUCT_JSON)
            mp.setattr(catalog_store, "RELIABILITY_CSV", config.RELIABILITY_CSV)
            mp.setattr(catalog_store, "CO2_CSV", config.CO2_CSV)
            mp.setattr(catalog_store, "CATALOG_DB", config.CATALOG_DB)
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()
            prev_con = textsearch._con
        yield
    finally:
        if prev_con is not None:
            try:
                prev_con().close()
            except Exception:
                pass
            textsearch._con.cache_clear()
            textsearch._nonfood_categories.cache_clear()
        mp.setattr(cat, "_SOURCE", orig_source)
        mp.undo()
        shutil.rmtree(cat_tmp, ignore_errors=True)


@pytest.mark.parametrize("gold_id", ["g07", "g10", "g11", "g14"])
def test_replay_backed_service_plan_need_for_fi_sv_gold_items(
    gold_id: str,
    replay_catalog_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.delenv("AIMO_NOW", raising=False)
    monkeypatch.setenv("AIMO_REPLAY_CLOCK", "snapshot")
    monkeypatch.setenv("AIMO_NOW_FROZEN", "1")
    monkeypatch.setenv("AIMO_REPLAY", "1")

    rc = replay.ReplayClient(replay.BUNDLED_DIR)
    bc.set_client(rc)
    try:
        rec = next(r for r in load_gold([str(GOLD_20_PATH)]) if r["id"] == gold_id)
        needs = parse_request(rec["request"], rec["customer_id"], use_gemini=False)
        assert len(needs) == 1
        need = needs[0]
        assert "delivery_date_assumed" not in need.preferences

        qs = clarify.find_questions(
            needs,
            get_profile(rec["customer_id"]),
            rec["request"],
            now_helsinki(),
            lambda q, claims: [p.model_dump() for p in cat.search(q, claims=claims)],
        )
        assert qs == []

        ident = service.Identity(agent_id="buyer-1", customer_id=rec["customer_id"])
        plan = service.plan_need(need, identity=ident)
        assert len(plan.lines) >= 1
        assert plan.shortfall == 0

        acceptable = set(rec["gold"]["acceptable_product_ids"][0])
        total_covered = sum(line.covered_amount for line in plan.lines)
        assert total_covered >= need.quantity - 1e-6

        forbidden = rec["gold"]["forbidden_allergens"]
        for line in plan.lines:
            assert line.tier != "rejected"
            if gold_id in ("g07", "g10", "g11"):
                assert line.product_id in acceptable
            prod = cat.get_product(line.product_id)
            assert prod is not None
            for allergen in forbidden:
                assert prod.allergens.get(allergen) == AllergenStatus.free
    finally:
        bc.set_client(None)
