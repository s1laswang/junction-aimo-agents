import json
import os
from datetime import date

import pytest

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


def test_429_retry_after_then_stale(tmp_path, monkeypatch):
    import httpx
    import pytest
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(valio.time, "sleep", lambda s: None)
    monkeypatch.setattr(valio, "_post", lambda e, b: {"products": [{"productId": "1", "catalogPrice": {"value": 2}}]})
    valio.price("c", D, ["1"])

    calls = []

    class R:
        status_code = 429
        headers = {"Retry-After": "1"}

    class C:
        def post(self, *a, **k):
            calls.append(1)
            return R()

    monkeypatch.undo()
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(valio.time, "sleep", lambda s: None)
    monkeypatch.setattr(valio, "_http", lambda: C())
    out = valio.price("c", date(2026, 11, 20), ["1"])
    assert out["1"]["catalogPrice"]["value"] == 2 and len(calls) == 3
    with pytest.raises(valio.UpstreamUnavailable):
        valio.price("c", date(2026, 11, 20), ["999"])
    assert valio.UpstreamUnavailable.code == "UPSTREAM_UNAVAILABLE"


def test_missing_env_raises_upstream_unavailable_on_cold_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(valio, "_client", None)
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    with pytest.raises(valio.UpstreamUnavailable, match="VALIO_API_BASE or VALIO_API_KEY not configured"):
        valio.price("hotel-aurora", D, IDS)

    monkeypatch.setenv("VALIO_API_BASE", "https://example.test")
    monkeypatch.setenv("VALIO_API_KEY", "   ")
    with pytest.raises(valio.UpstreamUnavailable, match="VALIO_API_BASE or VALIO_API_KEY not configured"):
        valio.availability("hotel-aurora", D, IDS)

    monkeypatch.setenv("VALIO_API_BASE", "")
    monkeypatch.setenv("VALIO_API_KEY", "secret")
    with pytest.raises(valio.UpstreamUnavailable, match="VALIO_API_BASE or VALIO_API_KEY not configured"):
        valio.price("hotel-aurora", D, IDS)
    assert valio._client is None


def test_warm_and_stale_cache_succeed_without_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_CACHE_DIR", str(tmp_path))
    cache_path = tmp_path / f"price__hotel-aurora__{D.isoformat()}.json"
    cache_path.write_text(json.dumps({i: {"productId": i, "catalogPrice": {"value": 3.5}} for i in IDS}))
    os.utime(cache_path, (1000, 1000))

    monkeypatch.setattr(valio, "_client", None)
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)

    warm = valio.price("hotel-aurora", D, IDS)
    assert set(warm) == set(IDS)
    assert warm["464073"]["catalogPrice"]["value"] == 3.5
    assert int(cache_path.stat().st_mtime) == 1000

    stale = valio.price("hotel-aurora", date(2026, 11, 20), IDS)
    assert set(stale) == set(IDS)
    assert stale["463335"]["catalogPrice"]["value"] == 3.5
