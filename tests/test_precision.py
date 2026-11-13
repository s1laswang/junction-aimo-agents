import os
import re
from datetime import date
from pathlib import Path

import pytest

from aimo.parse import parse_need

catalog = pytest.importorskip("aimo.catalog")
substitute = pytest.importorskip("aimo.substitute")

DATA = Path(os.environ.get("AIMO_DATA_DIR", "data")) / "valio_aimo_junction26_product_data.json"
pytestmark = pytest.mark.skipif(not DATA.exists(), reason="product data missing")

T = date(2026, 11, 13)
BAD_DAIRY = r"chocolate|suklaa|yogh?urt|jogurt|oat|kaura|soy|soija|powder|jauhe|ice cream|jäätelö"

CASES = [
    ("I need 1,000 litres of lactose-free milk by Monday", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("200 kg of Finnish potatoes by Friday", [(r"potato|peruna", r"salad|salaatti|chips|fries|ranskal|mash|muusi|dumpling")]),
    ("50 l whipping cream tomorrow, organic", [(r"cream|kerma", r"sour|kermaviili|ice|jäätelö|cheese|juusto|sauce|kastike|soup|keitto")]),
    ("20 kg Valio butter", [(r"butter|voi\b", r"cookie|keksi|bun|pulla|sauce|kastike|croissant|peanut|maapähkinä")]),
    ("100 kg gluten-free pasta", [(r"pasta", r"sauce|kastike|salad|salaatti|lasagne|kana|chicken|liha|meat")]),
    ("500 eggs next week", [(r"\begg|muna", r"plant|salad|salaatti|noodle|nuudeli|mayo|majoneesi")]),
    ("30 kg lactose-free cheese", [(r"cheese|juusto|luostari", r"cream cheese|tuorejuusto|sauce|kastike|pizza|burger|mac &")]),
    ("200 l laktoositonta maitoa huomenna, pähkinätön", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("Tarvitsen 50 kg gluteenitonta jauhoa", [(r"flour|jauho", r"mix|seos|pizza|bread|leipä|pulla")]),
    ("40 kg kanaa, gluteeniton", [(r"chicken|kana", r"soup|keitto|sauce|kastike|egg|muna|sandwich|leipä")]),
    ("100 l oat drink no soy", [(r"oat|kaura", r"soy|soija|ice|jäätelö|yogh?urt|jogurt|porridge|puuro")]),
    ("20 kg rahkaa", [(r"quark|rahka", r"cake|kakku|pie|piirakka")]),
    ("300 l milk for school lunch under 1.5 EUR", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("100 kg carrots", [(r"carrot|porkkana", r"salad|salaatti|cake|kakku|juice|mehu|soup|keitto")]),
    ("80 kg rice", [(r"\brice\b|riisi", r"pudding|puuro|cake|kakku|noodle|milk|maito|crisp|waffle")]),
    ("Tilaan 60 kg perunaa ja 40 l maitoa maanantaiksi", [(r"potato|peruna", r"salad|salaatti|chips|fries|ranskal"), (r"\bmilk\b|maito", BAD_DAIRY)]),
    ("30 kg lohta huomenna", [(r"salmon|lohi", r"sauce|kastike|salad|salaatti|soup|keitto|pasta")]),
    ("20 kg organic apples", [(r"apple|omena", r"juice|mehu|pie|piirakka|sauce|sose|jam|hillo|cider|siideri|bar|cinnamon|rawbite")]),
    ("100 kg tomatoes by Wednesday", [(r"tomato|tomaatti", r"sauce|kastike|soup|keitto|ketchup|paste|purée|sose|pizza")]),
]


@pytest.mark.parametrize("text,expect", CASES, ids=[c[0][:40] for c in CASES])
def test_top_product_is_what_was_asked(text, expect):
    need = parse_need(text, "hotel-aurora", T)
    assert len(need.items) == len(expect)
    for item, (must, mustnot) in zip(need.items, expect):
        found = catalog.search(item.query, item.constraints, limit=50)
        assert found, f"no products for {item.query}"
        ranked = sorted(found, key=lambda p: -substitute.match_score(item, p))
        top = ranked[0]
        name = top.name.lower()
        assert re.search(must, name), (top.product_id, top.name)
        assert not re.search(mustnot, name), (top.product_id, top.name)
        assert substitute.match_score(item, top) >= 0.5, (top.product_id, top.name)
        assert substitute.allergen_safe(top, item.constraints, item.constraints.exclude_allergens), (top.product_id, top.name)
