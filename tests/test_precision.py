import os
import re
from datetime import date
from pathlib import Path

import pytest

from aimo.parse import parse_need
from precision_cases import SET1

catalog = pytest.importorskip("aimo.catalog")
substitute = pytest.importorskip("aimo.substitute")

DATA = Path(os.environ.get("AIMO_DATA_DIR", "data")) / "valio_aimo_junction26_product_data.json"
pytestmark = pytest.mark.skipif(not (DATA.exists() and os.environ.get("AIMO_EVAL")), reason="precision eval: needs data and AIMO_EVAL=1")

T = date(2026, 11, 13)
BAD_DAIRY = r"chocolate|suklaa|yogh?urt|jogurt|oat|kaura|soy|soija|powder|jauhe|ice cream|jäätelö"

CASES = SET1


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
