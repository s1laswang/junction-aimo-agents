from datetime import date

from aimo import valio

D = date(2026, 11, 16)
IDS = ["464073", "463335"]


def test_price_and_availability_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path))
    calls = []

    def fake(endpoint, body):
        calls.append((endpoint, body["products"]))
        key = "catalogPrice" if endpoint == "price" else "scheduleLines"
        return {"products": [{"productId": i, key: 1} for i in body["products"]]}

    monkeypatch.setattr(valio, "_post", fake)
    p = valio.price("hotel-aurora", D, IDS)
    assert set(p) == set(IDS)
    valio.price("hotel-aurora", D, IDS + ["460448"])
    assert calls[-1] == ("price", ["460448"])
    valio.price("hotel-aurora", D, IDS)
    assert len(calls) == 2
    assert set(valio.availability("hotel-aurora", D, IDS)) == set(IDS)


def test_batches_of_1000(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path))
    sizes = []

    def fake(endpoint, body):
        sizes.append(len(body["products"]))
        return {"products": [{"productId": i} for i in body["products"]]}

    monkeypatch.setattr(valio, "_post", fake)
    valio.price("c", D, [str(i) for i in range(2300)])
    assert sizes == [1000, 1000, 300]
