import os
import pytest

pytest.importorskip("pitch.measure_data")
from pitch.measure_data import measure


@pytest.mark.skipif(not os.path.exists("data/valio_aimo_junction26_product_data.json"), reason="no data")
def test_measure_sane():
    m = measure()
    assert m["products"] == 17546
    assert 0 < m["lactose_free_claim"] < m["products"]
    assert 0 <= m["reliability_min"] <= m["reliability_median"] <= 1
