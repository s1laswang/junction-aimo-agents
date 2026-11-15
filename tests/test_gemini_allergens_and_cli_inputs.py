from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import io
import json
import os
from pathlib import Path
import time
import tokenize
from typing import Any

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from aimo import cli, config, gemini
from aimo.backend import catalog_store, client as backend_client, orders as store
from aimo.buyer import run_demo
from aimo.catalog import embeddings, gemini_allergens, netcheck, plan_need, product_from_row, textsearch
from aimo.gateway.app import create_app
from aimo.models import EU14, Need, ProductSummary


def _synth_catalog_items() -> list[dict[str, Any]]:
    base_1l = [{"synkkaPackagingType": "BASE_UNIT_OR_EACH", "volume": {"unit": "LTR", "value": 1.0}}]
    return [
        {
            "id": "464306",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Valio",
            "allowedLotSize": 1.0,
            "temperatureCondition": 2,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Valio",
                "names": [
                    {"language": "en", "value": "Oat drink Barista 1 l"},
                    {"language": "fi", "value": "Kaurajuoma Barista 1 l"},
                ],
                "unitConversions": base_1l,
                "classifications": [
                    {
                        "name": "allergen",
                        "type": "ENUM",
                        "values": [
                            {"id": "AM", "unit": "FREE_FROM"},
                            {"id": "AN", "unit": "FREE_FROM"},
                            {"id": "AP", "unit": "FREE_FROM"},
                        ],
                    },
                    {"name": "nutritionalClaim", "type": "ENUM", "values": [{"id": "FREE_FROM_LACTOSE", "unit": "FREE_FROM"}]},
                    {"name": "nutritionFact", "type": "NUMBER", "values": [{"id": "FAT", "value": 3.0}]},
                ],
                "keyIngredients": [{"language": "fi", "value": "vesi, kaura, rapsiöljy"}],
            },
        },
        {
            "id": "501001",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Nordic",
            "allowedLotSize": 1.0,
            "temperatureCondition": 2,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Nordic",
                "names": [
                    {"language": "en", "value": "Oat drink Kitchen 1 l"},
                    {"language": "fi", "value": "Kaurajuoma Keittiö 1 l"},
                ],
                "unitConversions": base_1l,
                "classifications": [],
                "keyIngredients": [{"language": "fi", "value": "vesi, täysjyväkaura, manteli"}],
            },
        },
        {
            "id": "501002",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Nordic",
            "allowedLotSize": 1.0,
            "temperatureCondition": 2,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Nordic",
                "names": [
                    {"language": "en", "value": "Oat drink Plain 1 l"},
                    {"language": "fi", "value": "Kaurajuoma Maustamaton 1 l"},
                ],
                "unitConversions": base_1l,
                "classifications": [],
                "keyIngredients": [{"language": "fi", "value": "vesi, kaura, suola"}],
            },
        },
        {
            "id": "501003",
            "deleted": False,
            "salesUnit": "ST",
            "baseUnit": "ST",
            "category": "10400",
            "vendorName": "Nordic",
            "allowedLotSize": 1.0,
            "temperatureCondition": 2,
            "countryOfOrigin": "FI",
            "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
            "synkkaData": {
                "brand": "Nordic",
                "names": [
                    {"language": "en", "value": "Oat drink NoIng 1 l"},
                    {"language": "fi", "value": "Kaurajuoma Ilman Ainesosia 1 l"},
                ],
                "unitConversions": base_1l,
                "classifications": [],
                "keyIngredients": [],
            },
        },
    ]


class _StaticBackend:
    base_url = "fixture://static-allergens-cli"
    ttl_s = 300

    def price(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        meta = {"timeNow": "2026-11-14T14:00:00+0200"}
        products = [
            {
                "productId": pid,
                "salesUnit": "ST",
                "quantity": 1,
                "netPriceFound": True,
                "error": False,
                "catalogPrice": {"value": 1.85, "currency": "EUR"},
                "comparisonPrice": {"value": 1.85, "unit": "LTR"},
                "_fetched_at": now,
                "_meta": meta,
            }
            for pid in ids
        ]
        return {**meta, "products": products, "_oldest_fetch": now}

    def availability(
        self,
        ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        meta = {"timeNow": "2026-11-14T14:00:00+0200"}
        products = [
            {
                "productId": pid,
                "closingTime": "2026-11-17T17:00:00+0200",
                "salesBlock": False,
                "logisticModel": "Stock",
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableDate": requested_date.isoformat(),
                        "availableAmount": 500.0,
                        "salesUnit": "ST",
                    }
                ],
                "_fetched_at": now,
                "_meta": meta,
            }
            for pid in ids
        ]
        return {**meta, "products": products, "_oldest_fetch": now}


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pjson = tmp_path / "products.json"
    cdb = tmp_path / "catalog.sqlite"
    gem_cache = tmp_path / "cache" / "gemini_allergens.json"
    rec_cache = tmp_path / "cache" / "gemini_recipe.json"
    pjson.write_text(json.dumps(_synth_catalog_items()), encoding="utf-8")

    monkeypatch.setattr(config, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(config, "CATALOG_DB", cdb)
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", pjson)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", cdb)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", tmp_path / "missing_rel.csv")
    monkeypatch.setattr(catalog_store, "CO2_CSV", tmp_path / "missing_co2.csv")
    monkeypatch.setattr(gemini_allergens, "CACHE", gem_cache)
    monkeypatch.setattr(gemini_allergens, "RECIPE_CACHE", rec_cache)
    monkeypatch.setattr(gemini_allergens, "_mem", None)
    monkeypatch.setattr(embeddings, "EMB_PATH", tmp_path / "missing_emb.npy")
    monkeypatch.setattr(embeddings, "IDS_PATH", tmp_path / "missing_ids.json")
    monkeypatch.setattr(netcheck, "gemini_online", lambda *a, **k: False)
    monkeypatch.setenv("AIMO_NOW", "2026-11-14T14:00:00+02:00")
    monkeypatch.setenv("AIMO_SEMANTIC", "0")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")

    catalog_store.load_catalog(cdb, force=True)
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()
    backend_client.set_client(_StaticBackend())
    try:
        yield tmp_path
    finally:
        backend_client.set_client(None)
        monkeypatch.setattr(gemini_allergens, "_mem", None)
        textsearch._con.cache_clear()
        textsearch._nonfood_categories.cache_clear()


def test_test_file_zero_comments_and_zero_em_en_dashes() -> None:
    path = Path(__file__)
    src = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    comments = [tok for tok in tokens if tok.type == tokenize.COMMENT]
    assert comments == [], f"Unexpected comment tokens in {path}: {comments}"
    assert "\x23" not in src
    assert "\u2014" not in src
    assert "\u2013" not in src


@pytest.mark.parametrize(
    ("group", "status", "evidence"),
    [
        ("invalid_group", "contains", "milk"),
        ("", "contains", "milk"),
        ("milk", "free", "milk"),
        ("milk", "unknown", "milk"),
        ("milk", "not_found", "milk"),
        ("milk", "", "milk"),
        ("milk", "contains", None),
        ("milk", "contains", 123),
    ],
)
def test_group_hint_schema_rejects_bad_groups_statuses_and_evidence(
    group: Any,
    status: Any,
    evidence: Any,
) -> None:
    with pytest.raises(ValidationError):
        gemini_allergens.GroupHint(group=group, status=status, evidence=evidence)


@pytest.mark.parametrize(
    ("compatible", "confidence", "reason"),
    [
        ("not-a-bool", "high", "ok"),
        (None, "high", "ok"),
        (True, "certain", "ok"),
        (True, "", "ok"),
        (True, None, "ok"),
        (True, "high", None),
        (True, "high", 999),
    ],
)
def test_recipe_judgement_schema_rejects_invalid_fields(
    compatible: Any,
    confidence: Any,
    reason: Any,
) -> None:
    with pytest.raises(ValidationError):
        gemini_allergens.RecipeJudgement(compatible=compatible, confidence=confidence, reason=reason)


@pytest.mark.parametrize(
    "raw_payload",
    [
        None,
        b"",
        b"{broken-json",
        b"\xff\xfe\xfd\x80\x81",
        b"{}",
    ],
)
def test_load_missing_empty_corrupt_and_binary_cache_returns_empty_dict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_payload: bytes | None,
) -> None:
    cache_path = tmp_path / "gemini_allergens.json"
    if raw_payload is not None:
        cache_path.write_bytes(raw_payload)
    monkeypatch.setattr(gemini_allergens, "CACHE", cache_path)
    monkeypatch.setattr(gemini_allergens, "_mem", None)

    loaded = gemini_allergens._load()
    assert loaded == {}
    assert isinstance(loaded, dict)
    assert gemini_allergens.cached_inference("464306") is None
    assert gemini_allergens.cached_evidence("464306") == {}


def test_key_text_of_store_and_save_utf8_precedence_and_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "nested" / "gemini_allergens.json"
    monkeypatch.setattr(gemini_allergens, "CACHE", cache_path)
    monkeypatch.setattr(gemini_allergens, "_mem", None)

    assert gemini_allergens._key("464306", "vesi, kaura") != gemini_allergens._key("464306", "vesi, maito")
    assert gemini_allergens._key("464306", "vesi, kaura") != gemini_allergens._key("501001", "vesi, kaura")

    assert gemini_allergens._text_of({}) == ""
    assert gemini_allergens._text_of({"ingredients_text": None, "ingredients_fi": ""}) == ""
    assert gemini_allergens._text_of({"ingredients_text": "wheat", "ingredients_fi": "vehnä"}) == "wheat vehnä"
    assert len(gemini_allergens._text_of({"ingredients_text": "x" * 1500})) == 1200

    hints = [
        gemini_allergens.GroupHint(group="milk", status="may_contain", evidence="saattaa sisältää maitoa"),
        gemini_allergens.GroupHint(group="milk", status="contains", evidence="pastöroitu maito " + ("z" * 90)),
        gemini_allergens.GroupHint(group="milk", status="may_contain", evidence="maitojäämä"),
        gemini_allergens.GroupHint(group="nuts", status="may_contain", evidence="manteli, pähkinä"),
    ]
    out = gemini_allergens._store("p-utf8", "pastöroitu maito, manteli", hints)
    assert set(out.keys()) == set(EU14)
    assert out["milk"] == "contains"
    assert out["nuts"] == "may_contain"
    assert out["gluten"] == "not_found"

    gemini_allergens._save()
    assert list(cache_path.parent.glob("*.tmp")) == []

    monkeypatch.setattr(gemini_allergens, "_mem", None)
    inf = gemini_allergens.cached_inference("p-utf8")
    ev = gemini_allergens.cached_evidence("p-utf8")
    assert inf is not None
    assert inf["milk"] == "contains"
    assert inf["nuts"] == "may_contain"
    assert len(ev["milk"]) == 80
    assert ev["milk"].startswith("pastöroitu maito")
    assert ev["nuts"] == "manteli, pähkinä"


def test_store_thread_safety_under_concurrent_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "concurrent" / "gemini_allergens.json"
    monkeypatch.setattr(gemini_allergens, "CACHE", cache_path)
    monkeypatch.setattr(gemini_allergens, "_mem", {})

    def worker(idx: int) -> str:
        pid = f"prod-{idx:03d}"
        h = gemini_allergens.GroupHint(group="gluten", status="contains", evidence=f"vehnä {idx}")
        res = gemini_allergens._store(pid, f"vehnäjauho {idx}", [h])
        return res["gluten"]

    with ThreadPoolExecutor(max_workers=8) as ex:
        statuses = list(ex.map(worker, range(32)))

    assert statuses == ["contains"] * 32
    gemini_allergens._save()
    disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(disk) == 32


def test_plan_need_product_from_row_and_v1_plan_never_500_when_allergen_cache_corrupt(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gem_cache = isolated_env / "cache" / "gemini_allergens.json"
    gem_cache.parent.mkdir(parents=True, exist_ok=True)
    corrupt_payloads = (
        b"",
        b"{broken-json",
        b"\xff\xfe\x00\x81",
        b"[]",
        b"null",
        b"123",
        b'"not-an-object"',
        json.dumps({"id:501001": "not-a-dict", "id:464306": ["nuts"]}).encode("utf-8"),
        json.dumps({"id:501001": {"allergens": None, "evidence": None}}).encode("utf-8"),
    )
    with TestClient(create_app(with_mcp=False), raise_server_exceptions=False) as client:
        for cache_content in corrupt_payloads:
            gem_cache.write_bytes(cache_content)
            monkeypatch.setattr(gemini_allergens, "_mem", None)

            row = textsearch.get_rows(["501001"])["501001"]
            prod = product_from_row(row)
            assert prod.id == "501001"

            need = Need(
                customer_id="school-tampere",
                product_query="oat drink",
                quantity=20.0,
                unit="L",
                delivery_by=date(2026, 11, 18),
                excluded_allergens=["nuts"],
            )
            plan = plan_need(need)
            assert set(plan.need.excluded_allergens) == {"nuts", "milk"}
            assert any(line.product_id == "464306" for line in plan.lines)

            resp = client.post(
                "/v1/plan",
                headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "school-tampere/buyer-1"},
                json={
                    "customer_id": "school-tampere",
                    "product_query": "oat drink",
                    "quantity": 20.0,
                    "unit": "L",
                    "delivery_by": "2026-11-18",
                    "excluded_allergens": ["nuts"],
                },
            )
            assert resp.status_code == 200, (resp.status_code, resp.text)
            body = resp.json()
            assert set(body["need"]["excluded_allergens"]) == {"nuts", "milk"}
            assert any(line["product_id"] == "464306" for line in body["lines"])


def test_v1_plan_rejects_bad_need_inputs_with_clean_422_even_with_missing_allergen_cache(
    isolated_env: Path,
) -> None:
    bad_payloads = (
        {"customer_id": "school-tampere", "product_query": "oat drink", "quantity": 0, "unit": "L", "delivery_by": "2026-11-18"},
        {"customer_id": "school-tampere", "product_query": "oat drink", "quantity": -10, "unit": "L", "delivery_by": "2026-11-18"},
        {"customer_id": "school-tampere", "product_query": "   ", "quantity": 10, "unit": "L", "delivery_by": "2026-11-18"},
        {"customer_id": "school-tampere", "product_query": "oat\x00drink", "quantity": 10, "unit": "L", "delivery_by": "2026-11-18"},
    )
    with TestClient(create_app(with_mcp=False), raise_server_exceptions=False) as client:
        for bad_payload in bad_payloads:
            resp = client.post(
                "/v1/plan",
                headers={"X-Customer-Id": "school-tampere", "X-Agent-Id": "school-tampere/buyer-1"},
                json=bad_payload,
            )
            assert resp.status_code == 422, (resp.status_code, resp.text)


def test_infer_batch_handles_empty_whitespace_and_cache_hits_offline_then_parses_live_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "gemini_allergens.json"
    monkeypatch.setattr(gemini_allergens, "CACHE", cache_path)
    monkeypatch.setattr(gemini_allergens, "_mem", None)

    monkeypatch.setattr(
        gemini,
        "generate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gemini.generate must not run on empty or cached rows")),
    )
    assert gemini_allergens.infer_batch([]) == {}
    assert (
        gemini_allergens.infer_batch(
            [
                {"id": "empty-1", "ingredients_text": "", "ingredients_fi": "   "},
                {"id": "empty-2"},
            ]
        )
        == {}
    )

    gemini_allergens._store(
        "cached-1",
        "pastöroitu kerma",
        [gemini_allergens.GroupHint(group="milk", status="contains", evidence="kerma")],
    )
    gemini_allergens._save()
    monkeypatch.setattr(gemini_allergens, "_mem", None)

    hit_out = gemini_allergens.infer_batch([{"id": "cached-1", "ingredients_fi": "pastöroitu kerma"}])
    assert hit_out["cached-1"]["milk"] == "contains"

    calls: list[tuple[str, str]] = []

    class _JsonTextOnlyResp:
        parsed = None
        text = json.dumps(
            {
                "products": [
                    {
                        "id": "fresh-1",
                        "allergens": [{"group": "nuts", "status": "contains", "evidence": "manteli"}],
                    }
                ]
            }
        )

    def fake_generate(feature: str, model: str, prompt: str, config: Any = None) -> Any:
        calls.append((feature, prompt))
        return _JsonTextOnlyResp()

    monkeypatch.setattr(gemini, "generate", fake_generate)
    live_out = gemini_allergens.infer_batch(
        [{"id": "fresh-1", "name": "Almond Drink", "ingredients_fi": "vesi, manteli"}],
        feature="test.allergens",
    )
    assert len(calls) == 1
    assert calls[0][0] == "test.allergens"
    assert live_out["fresh-1"]["nuts"] == "contains"
    assert gemini_allergens.cached_evidence("fresh-1") == {"nuts": "manteli"}


def test_infer_allergens_handles_summary_model_dict_no_ingredients_and_bad_inputs_offline(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gemini,
        "generate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gemini.generate must not be called")),
    )
    row = textsearch.get_rows(["501001"])["501001"]
    gemini_allergens._store(
        "501001",
        gemini_allergens._text_of(row),
        [gemini_allergens.GroupHint(group="nuts", status="contains", evidence="manteli")],
    )
    gemini_allergens._save()

    summary = ProductSummary(id="501001", name="Oat drink Kitchen 1 l", sales_unit="ST")
    assert gemini_allergens.infer_allergens(summary)["nuts"] == "contains"
    assert gemini_allergens.infer_allergens({"id": "501001"})["nuts"] == "contains"
    assert gemini_allergens.infer_allergens({"id": "501003"}) == {}
    assert gemini_allergens.infer_allergens({"id": "unknown-999999"}) == {}

    with pytest.raises(KeyError):
        gemini_allergens.infer_allergens({})
    with pytest.raises((TypeError, ValueError)):
        gemini_allergens.infer_allergens(None)
    with pytest.raises((TypeError, ValueError)):
        gemini_allergens.infer_allergens(123)


def test_precompute_runs_offline_with_mocked_gemini_and_survives_chunk_failure(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(gemini, "client", lambda: object())
    call_idx = {"n": 0}

    def fake_infer_batch(ch: list[dict[str, Any]], feature: str = "catalog.allergens.precompute") -> dict[str, Any]:
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return {d["id"]: {g: "not_found" for g in EU14} for d in ch}

    monkeypatch.setattr(gemini_allergens, "infer_batch", fake_infer_batch)
    gemini_allergens.precompute(concurrency=4, batch=1, limit=2)
    out = capsys.readouterr().out
    assert "chunk failed: RuntimeError" in out
    assert "allergen hints: 1/2 products" in out


@pytest.mark.parametrize("bad_recipe_cache", [None, b"", b"{corrupt", b"\xff\xfe\x00"])
def test_recipe_compatible_handles_corrupt_cache_dict_id_and_object_inputs_keyless(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_recipe_cache: bytes | None,
) -> None:
    rec_cache = isolated_env / "cache" / "gemini_recipe.json"
    if bad_recipe_cache is not None:
        rec_cache.parent.mkdir(parents=True, exist_ok=True)
        rec_cache.write_bytes(bad_recipe_cache)

    calls: list[str] = []

    class _FakeRecipeResp:
        parsed = None
        text = json.dumps({"compatible": True, "confidence": "high", "reason": "Smooth crema in coffee."})

    def fake_generate(feature: str, model: str, prompt: str, config: Any = None) -> Any:
        calls.append(prompt)
        return _FakeRecipeResp()

    monkeypatch.setattr(gemini, "generate", fake_generate)

    res1 = gemini_allergens.recipe_compatible("464306", "501002", use="barista coffee")
    assert len(calls) == 1
    assert "Oat drink Barista 1 l" in calls[0]
    assert res1["compatible"] is True
    assert res1["confidence"] == "high"
    assert res1["source"] == "inferred:gemini"

    monkeypatch.setattr(
        gemini,
        "generate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("expected recipe cache hit")),
    )
    res2 = gemini_allergens.recipe_compatible("464306", "501002", use="barista coffee")
    assert res2 == res1

    monkeypatch.setattr(gemini, "generate", fake_generate)
    summary = ProductSummary(id="464306", name="Oat drink Barista 1 l", sales_unit="ST")
    res3 = gemini_allergens.recipe_compatible(summary, {"name": "Soy drink 1 l"}, use="porridge")
    assert res3["compatible"] is True


@pytest.mark.parametrize(
    "bad_argv",
    [
        [],
        ["unknown-cmd"],
        ["serve", "--port", "abc"],
        ["serve", "--port", "8000.5"],
        ["serve", "--port", ""],
        ["serve", "--port", "80a"],
        ["serve", "--port", "None"],
    ],
)
def test_cli_main_rejects_bad_subcommands_and_port_types_with_exit_2(
    capsys: pytest.CaptureFixture[str],
    bad_argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(bad_argv)
    capsys.readouterr()
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("explicit", "expected"),
    [
        ("http://localhost:8000/mcp", (True, 8000)),
        ("http://LOCALHOST.:8080/mcp", (True, 8080)),
        ("https://localhost/mcp", (True, 443)),
        ("http://127.0.0.1/mcp", (True, 80)),
        ("localhost:8123/mcp", (True, 8123)),
        ("http://0.0.0.0:9000/mcp", (True, 9000)),
        ("http://[::1]:8001/mcp", (True, 8001)),
        ("http://::1:8002/mcp", (True, 8002)),
        ("http://::1/mcp", (True, 80)),
        ("http://::/mcp", (True, 80)),
        ("http://:::8003/mcp", (True, 8003)),
        ("http://[0:0:0:0:0:0:0:1]:8004/mcp", (True, 8004)),
        ("http://localhost:99999/mcp", (False, None)),
        ("http://localhost:abc/mcp", (False, None)),
        ("http://[::1:8000/mcp", (False, None)),
        ("https://gateway.example.org/mcp", (False, None)),
        ("http://localhost.example.org:8000/mcp", (False, None)),
    ],
)
def test_local_mcp_port_parses_local_variants_and_handles_malformed_or_external_urls(
    explicit: str,
    expected: tuple[bool, int | None],
) -> None:
    assert cli._local_mcp_port(explicit) == expected


@pytest.mark.parametrize(
    ("host", "port", "expected_serve_url"),
    [
        ("127.0.0.1", 8000, "http://127.0.0.1:8000"),
        ("0.0.0.0", 8010, "http://127.0.0.1:8010"),
        ("::", 8011, "http://127.0.0.1:8011"),
        ("localhost", 8012, "http://127.0.0.1:8012"),
        ("", 8013, "http://127.0.0.1:8013"),
        ("   ", 8014, "http://127.0.0.1:8014"),
        ("[]", 8015, "http://127.0.0.1:8015"),
        ("::1", 8016, "http://[::1]:8016"),
        ("[::1]", 8017, "http://[::1]:8017"),
        ("10.0.0.5", 8018, "http://10.0.0.5:8018"),
    ],
)
def test_export_serve_url_normalizes_hosts_and_manages_mcp_url_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
    port: int,
    expected_serve_url: str,
) -> None:
    monkeypatch.setenv("AIMO_MCP_URL", "   ")
    cli._export_serve_url(host, port)
    assert os.environ["AIMO_SERVE_URL"] == expected_serve_url
    assert "AIMO_MCP_URL" not in os.environ
    assert capsys.readouterr().out == ""

    monkeypatch.setenv("AIMO_MCP_URL", "  http://127.0.0.1:9999/mcp  ")
    cli._export_serve_url(host, port)
    warn = capsys.readouterr().out
    assert "warning:" in warn
    assert os.environ["AIMO_MCP_URL"] == f"{expected_serve_url}/mcp"

    monkeypatch.setenv("AIMO_MCP_URL", "https://external.example.org/mcp")
    cli._export_serve_url(host, port)
    assert capsys.readouterr().out == ""
    assert os.environ["AIMO_MCP_URL"] == "https://external.example.org/mcp"


@pytest.mark.parametrize("bad_port", ["abc", "", None])
def test_export_serve_url_invalid_port_raises_before_mutating_env(
    monkeypatch: pytest.MonkeyPatch,
    bad_port: Any,
) -> None:
    monkeypatch.setenv("AIMO_SERVE_URL", "http://127.0.0.1:8000")
    with pytest.raises((TypeError, ValueError)):
        cli._export_serve_url("127.0.0.1", bad_port)
    assert os.environ["AIMO_SERVE_URL"] == "http://127.0.0.1:8000"


def test_build_catalog_handles_missing_files_not_implemented_and_keyless_skip_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        catalog_store,
        "load_catalog",
        lambda: (_ for _ in ()).throw(FileNotFoundError(2, "No such file", "products.json")),
    )
    cli._build_catalog("background")
    assert "challenge catalog data is missing" in capsys.readouterr().out

    monkeypatch.setattr(
        catalog_store,
        "load_catalog",
        lambda: (_ for _ in ()).throw(NotImplementedError()),
    )
    cli._build_catalog("background")
    assert "catalog store not implemented yet" in capsys.readouterr().out

    monkeypatch.setattr(catalog_store, "load_catalog", lambda: None)
    monkeypatch.setattr(embeddings, "available", lambda: False)
    missing_cache = tmp_path / "missing_allergens.json"
    monkeypatch.setattr(gemini_allergens, "CACHE", missing_cache)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    cli._build_catalog("background")
    out_keyless = capsys.readouterr().out
    assert "GEMINI_API_KEY not set" in out_keyless

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-never-called")
    cli._build_catalog("skip")
    out_skip = capsys.readouterr().out
    assert "skipped (--no-build)" in out_skip

    built: list[str] = []
    monkeypatch.setattr(embeddings, "build", lambda: built.append("emb"))
    monkeypatch.setattr(gemini_allergens, "precompute", lambda: built.append("alg"))
    cli._build_catalog("sync")
    out_sync = capsys.readouterr().out
    assert built == ["emb", "alg"]
    assert "catalog Gemini builds finished" in out_sync

    present_cache = tmp_path / "present_allergens.json"
    present_cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gemini_allergens, "CACHE", present_cache)
    monkeypatch.setattr(embeddings, "available", lambda: True)
    cli._build_catalog("background")
    assert "embeddings and allergen hints present" in capsys.readouterr().out


def test_cli_subcommand_dispatch_serve_demo_mcp_fetch_and_mcp_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uvicorn_calls: list[tuple[str, str, int, bool]] = []
    monkeypatch.setattr(cli, "_fetch", lambda force=False: (_ for _ in ()).throw(SystemExit("offline fetch warning")))
    monkeypatch.setattr(cli, "_build_catalog", lambda mode="background": None)
    import uvicorn

    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app_str, host, port, reload, log_level, proxy_headers: uvicorn_calls.append((app_str, host, port, reload)),
    )
    cli.main(["serve", "--port", "8111", "--host", "127.0.0.1", "--no-build"])
    serve_out = capsys.readouterr().out
    assert "warning: offline fetch warning" in serve_out
    assert uvicorn_calls == [("aimo.gateway.app:app", "127.0.0.1", 8111, False)]
    assert os.environ["AIMO_SERVE_URL"] == "http://127.0.0.1:8111"

    for demo_ret, expected_rc in ((0, 0), (1, 1), (2, 2), (None, 0), ("done", 0)):
        monkeypatch.setattr(run_demo, "main", lambda argv=None, _r=demo_ret: _r)
        with pytest.raises(SystemExit) as exc:
            cli.cmd_demo(argparse.Namespace(cmd="demo"), ["--transport", "fake"])
        assert exc.value.code == expected_rc

    fetched_flags: list[bool] = []
    monkeypatch.setattr(cli, "_fetch", lambda force=False: fetched_flags.append(force))
    cli.main(["fetch-data", "--force"])
    assert fetched_flags == [True]

    checked_targets: list[str] = []
    from aimo.gateway import mcp_check, mcp_server

    async def fake_mcp_check(target: str) -> None:
        checked_targets.append(target)

    monkeypatch.setattr(mcp_check, "main", fake_mcp_check)
    cli.main(["mcp-check", "stdio"])
    assert checked_targets == ["stdio"]

    mcp_runs: list[str] = []
    monkeypatch.setattr(mcp_server, "main", lambda: mcp_runs.append("ok"))
    cli.main(["mcp"])
    assert mcp_runs == ["ok"]
