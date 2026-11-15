from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

import aimo.catalog as cat
from aimo import config, gemini
from aimo.backend import catalog_store
from aimo.backend import client as bc
from aimo.backend import orders as store
from aimo.buyer.agent import BuyerAgent
from aimo.buyer.mock_model import MOCK_LABEL, MOCK_MODEL_NAME
from aimo.buyer.need import HELSINKI
from aimo.buyer.profiles import mandate_for
from aimo.buyer.tool_schemas import TOOL_SCHEMAS
from aimo.buyer.tools import InProcessTransport, ToolSpec, gateway_agent_id
from aimo.catalog import embeddings, gemini_allergens, get_product, textsearch
from aimo.catalog.allergens import GS1_GROUPS
from aimo.gateway import extras, human_auth, service
from aimo.gateway.app import create_app
from aimo.gateway.mcp_server import build_mcp
from aimo.models import EU14, Quote
from aimo.trust import policy as trust_policy
from aimo.trust.signing import verify_quote_detailed

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
DELIVERY = date(2026, 11, 16)
HKI = timezone(timedelta(hours=2))
APPROVER_PIN = "test-pin-12chars-ok"

SCHOOL_QUERIES: list[tuple[str, float, str]] = [
    ("milk", 20.0, "L"),
    ("whole milk", 20.0, "L"),
    ("oat drink", 20.0, "L"),
    ("butter", 5.0, "KG"),
    ("cream", 10.0, "L"),
    ("almonds", 4.0, "KG"),
]

HOSPITAL_QUERIES: list[tuple[str, float, str]] = [
    ("bread", 10.0, "KG"),
    ("rye bread", 10.0, "KG"),
    ("flour", 10.0, "KG"),
]


class HeaderContext:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = dict(headers)


class HttpTools:
    kind = "http"

    def __init__(self, client: TestClient, customer_id: str, agent_id: str = "buyer-1") -> None:
        self.client = client
        self.customer_id = customer_id
        self.agent_id = gateway_agent_id(agent_id, customer_id)
        self.headers = {
            "X-Customer-Id": self.customer_id,
            "X-Agent-Id": self.agent_id,
        }

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(t["name"], t["description"], t["input_schema"]) for t in TOOL_SCHEMAS]

    def _unwrap(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {}
            raise service.ServiceError(
                resp.status_code,
                str(body.get("error") or "http_error"),
                str(body.get("message") or resp.text),
                body.get("detail"),
            )
        return resp.json()

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name == "search_products":
            params: dict[str, Any] = {
                "q": args["query"],
                "limit": int(args.get("limit") or 10),
            }
            claims = args.get("claims") or []
            if claims:
                params["claims"] = ",".join(str(c) for c in claims)
            resp = self.client.get("/v1/products/search", params=params, headers=self.headers)
            return {"products": self._unwrap(resp)}
        if name == "get_product":
            resp = self.client.get(f"/v1/products/{args['product_id']}", headers=self.headers)
            return self._unwrap(resp)
        if name == "get_offers":
            resp = self.client.post(
                "/v1/offers",
                headers=self.headers,
                json={
                    "product_ids": list(args["product_ids"]),
                    "requested_date": str(args["requested_date"]),
                },
            )
            return {"offers": self._unwrap(resp), "provenance": dict(resp.headers)}
        if name == "plan_need":
            resp = self.client.post("/v1/plan", headers=self.headers, json=args["need"])
            return self._unwrap(resp)
        if name == "create_quote":
            payload: dict[str, Any] = {
                "delivery_date": str(args["delivery_date"]),
                "lines": list(args["lines"]),
            }
            if args.get("need") is not None:
                payload["need"] = args["need"]
            resp = self.client.post("/v1/quotes", headers=self.headers, json=payload)
            return self._unwrap(resp)
        if name == "place_order":
            resp = self.client.post(
                "/v1/orders",
                headers=self.headers,
                json={"quote_id": args["quote_id"], "quote_hash": args["quote_hash"]},
            )
            return self._unwrap(resp)
        if name == "get_order_status":
            resp = self.client.get(f"/v1/orders/{args['order_id']}", headers=self.headers)
            return self._unwrap(resp)
        if name == "get_audit_trail":
            sid = str(args["subject_id"])
            evs = self._unwrap(self.client.get("/v1/audit", params={"subject_id": sid}, headers=self.headers))
            ver = self._unwrap(self.client.get("/v1/audit/verify", headers=self.headers))
            rep_resp = self.client.get(f"/v1/audit/report/{sid}", headers=self.headers)
            return {"events": evs, "report": rep_resp.text, "verify": ver}
        if name == "list_policies":
            custs = self._unwrap(self.client.get("/v1/customers", headers=self.headers))
            learned = self._unwrap(
                self.client.get(f"/v1/customers/{self.customer_id}/learned", headers=self.headers)
            )
            return {"policies": custs, "learned_swaps": learned}
        raise ValueError(f"unsupported tool {name!r}")

    def close(self) -> None:
        return None


def _make_raw_product(
    pid: str,
    name: str,
    category: str,
    *,
    brand: str = "Valio",
    claims: tuple[str, ...] = (),
    allergen_states: dict[str, str] | None = None,
    ingredients: str = "water, salt",
) -> dict[str, Any]:
    states = {g: "free" for g in EU14}
    if allergen_states:
        states.update(allergen_states)
    allergen_vals: list[dict[str, str]] = []
    for g, st in states.items():
        head = GS1_GROUPS[g][0][0]
        if st == "contains":
            allergen_vals.append({"id": head, "unit": "CONTAINS"})
        elif st == "may_contain":
            allergen_vals.append({"id": head, "unit": "MAY_CONTAIN"})
        elif st == "free":
            allergen_vals.append({"id": head, "unit": "FREE_FROM"})
        elif st == "unknown":
            allergen_vals.append({"id": head, "unit": "UNDECLARED"})
    classifications: list[dict[str, Any]] = [{"name": "allergen", "values": allergen_vals}]
    if claims:
        classifications.append({"name": "nutritionalClaim", "values": [{"id": c} for c in claims]})
    return {
        "id": pid,
        "deleted": False,
        "salesUnit": "ST",
        "baseUnit": "ST",
        "category": category,
        "vendorName": "Synthetic Test Wholesaler",
        "allowedLotSize": 1,
        "countryOfOrigin": "FI",
        "units": [{"unitId": "ST", "sizeInBaseUnits": 1}],
        "synkkaData": {
            "names": [{"language": "en", "value": name}, {"language": "fi", "value": name}],
            "brand": brand,
            "classifications": classifications,
            "keyIngredients": [{"language": "en", "value": ingredients}],
            "nutritionalContent": [{"id": "ENERC", "value": 200.0, "basisQuantity": 100.0}],
            "marketingTexts": [{"language": "en", "value": f"{name} for professional kitchens."}],
        },
    }


def _synthetic_catalog_spec() -> tuple[list[dict[str, Any]], dict[str, float], dict[str, tuple[dict[str, str], dict[str, str]]]]:
    products: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
    inferred_patches: dict[str, tuple[dict[str, str], dict[str, str]]] = {}

    def add(
        pid: str,
        name: str,
        category: str,
        price: float,
        *,
        brand: str = "Valio",
        claims: tuple[str, ...] = (),
        allergen_states: dict[str, str] | None = None,
        ingredients: str = "water, salt",
        inferred_groups: tuple[str, ...] = (),
    ) -> None:
        products.append(
            _make_raw_product(
                pid,
                name,
                category,
                brand=brand,
                claims=claims,
                allergen_states=allergen_states,
                ingredients=ingredients,
            )
        )
        prices[pid] = price
        if inferred_groups:
            inf_allergens = {g: "free" for g in EU14}
            inf_sources = {g: ("inferred:gemini" if g in inferred_groups else "structured") for g in EU14}
            inferred_patches[pid] = (inf_allergens, inf_sources)

    add(
        "463038",
        "Valio Eila semi skimmed milk drink 10 L lactose free",
        "10400",
        14.00,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
    )
    add(
        "464306",
        "Valio Eila lactose-free semi skimmed milk drink 1 L",
        "10400",
        1.45,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="milk, vitamin D",
    )
    add(
        "e2e-milk-whole",
        "Valio whole milk 1 L",
        "10400",
        1.15,
        allergen_states={"milk": "contains"},
        ingredients="pasteurised whole milk",
    )
    add(
        "e2e-milk-may",
        "Shared-line whole milk drink 1 L",
        "10400",
        1.05,
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="water, oats",
    )
    add(
        "e2e-milk-unk",
        "Unverified farm whole milk 1 L",
        "10400",
        0.95,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="milk",
    )
    add(
        "e2e-milk-inf",
        "Inferred dairy-style whole milk 1 L",
        "10400",
        0.90,
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="water, oats",
    )
    add(
        "e2e-milk-glutentrap",
        "Cereal-blended dairy milk 1 L",
        "10400",
        0.85,
        brand="Generic",
        allergen_states={"milk": "contains", "gluten": "may_contain"},
        ingredients="milk, barley extract",
    )

    add(
        "470889",
        "Oatly oat drink whole 1 L",
        "10402",
        1.95,
        brand="Oatly",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )
    add(
        "e2e-oat-safe2",
        "Oddlygood oat drink 1 L",
        "10402",
        2.05,
        brand="Oddlygood",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )
    add(
        "e2e-oat-nut",
        "Hazelnut oat drink 1 L",
        "10402",
        1.05,
        brand="Nordic",
        allergen_states={"nuts": "contains"},
        ingredients="water, oats, hazelnut",
    )
    add(
        "e2e-oat-maymilk",
        "Shared-line oat drink 1 L",
        "10402",
        1.10,
        brand="Nordic",
        allergen_states={"milk": "may_contain"},
        ingredients="water, oats, rapeseed oil",
    )
    add(
        "e2e-oat-maynut",
        "Almond-trace oat drink 1 L",
        "10402",
        1.12,
        brand="Nordic",
        allergen_states={"nuts": "may_contain"},
        ingredients="water, oats, rapeseed oil",
    )
    add(
        "e2e-oat-unk",
        "Unverified oat drink 1 L",
        "10402",
        1.00,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown", "gluten": "unknown"},
        ingredients="water, oats",
    )
    add(
        "e2e-oat-inf",
        "Inferred oat drink 1 L",
        "10402",
        0.98,
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="water, oats",
    )

    add(
        "e2e-butter-reg",
        "Valio butter 500 g",
        "10500",
        3.10,
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
    )
    add(
        "e2e-butter-lf",
        "Valio Eila lactose-free butter 500 g",
        "10500",
        3.40,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, salt",
    )
    add(
        "e2e-butter-may",
        "Shared-churn butter blend 500 g",
        "10500",
        2.10,
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="vegetable oil, water",
    )
    add(
        "e2e-butter-unk",
        "Unverified butter block 500 g",
        "10500",
        1.90,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="fat blend",
    )
    add(
        "e2e-butter-inf",
        "Inferred culinary butter block 500 g",
        "10500",
        1.85,
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="vegetable fat, water",
    )
    add(
        "e2e-butter-almond",
        "Roasted almond butter 500 g",
        "10500",
        4.20,
        brand="Nordic",
        allergen_states={"nuts": "contains"},
        ingredients="roasted almonds",
    )
    add(
        "e2e-butter-glutentrap",
        "Herb crumb butter 500 g",
        "10500",
        1.50,
        brand="Generic",
        allergen_states={"milk": "contains", "gluten": "may_contain"},
        ingredients="cream, wheat crumb, herbs",
    )

    add(
        "e2e-marg-may",
        "Blended lactose free margarine 1 kg",
        "1051",
        1.80,
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "may_contain"},
        ingredients="rapeseed oil, water, salt",
    )
    add(
        "e2e-marg-unk",
        "Unverified lactose free margarine 1 kg",
        "1051",
        1.70,
        brand="Generic",
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="rapeseed oil, water",
    )
    add(
        "e2e-marg-safe",
        "Sunnuntai lactose free margarine 1 kg",
        "1051",
        2.90,
        brand="Sunnuntai",
        claims=("FREE_FROM_LACTOSE", "VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="rapeseed oil, water, salt",
    )

    add(
        "e2e-cream-reg",
        "Valio cooking cream 15% 1 L",
        "10401",
        2.70,
        allergen_states={"milk": "contains"},
        ingredients="cream, stabilizer",
    )
    add(
        "e2e-cream-lf",
        "Valio Eila lactose-free cream 15% 1 L",
        "10401",
        2.95,
        claims=("FREE_FROM_LACTOSE",),
        allergen_states={"milk": "contains"},
        ingredients="cream, lactase",
    )
    add(
        "e2e-cream-may",
        "Shared-line cooking cream 1 L",
        "10401",
        2.05,
        brand="Generic",
        allergen_states={"milk": "may_contain"},
        ingredients="water, vegetable oil",
    )
    add(
        "e2e-cream-unk",
        "Unverified cooking cream 1 L",
        "10401",
        1.95,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "unknown"},
        ingredients="cream base",
    )
    add(
        "e2e-cream-inf",
        "Inferred plant cream 1 L",
        "10401",
        1.90,
        brand="Generic",
        inferred_groups=("milk", "nuts"),
        ingredients="water, rapeseed oil",
    )
    add(
        "e2e-oatcook-nut",
        "Cashew oat cooking 1 L",
        "10402",
        1.40,
        brand="Generic",
        allergen_states={"nuts": "may_contain"},
        ingredients="water, pure oats, rapeseed oil",
    )
    add(
        "e2e-oatcook-safe",
        "Oddlygood oat cooking 1 L",
        "10402",
        2.40,
        brand="Oddlygood",
        claims=("VEGAN", "FREE_FROM_GLUTEN"),
        allergen_states={},
        ingredients="water, pure oats, rapeseed oil, salt",
    )

    add(
        "e2e-almonds-contains",
        "Whole almonds 1 kg",
        "21400",
        8.50,
        brand="Nordic",
        allergen_states={"nuts": "contains"},
        ingredients="almonds",
    )
    add(
        "e2e-almonds-may",
        "Sliced almonds 1 kg",
        "21400",
        7.90,
        brand="Nordic",
        allergen_states={"nuts": "may_contain"},
        ingredients="almonds",
    )
    add(
        "e2e-almonds-unk",
        "Unverified almonds 1 kg",
        "21400",
        6.50,
        brand="Generic",
        allergen_states={"nuts": "unknown", "milk": "unknown"},
        ingredients="nuts",
    )
    add(
        "e2e-almonds-inf",
        "Inferred almonds culinary pack 1 kg",
        "21400",
        6.00,
        brand="Generic",
        inferred_groups=("nuts",),
        ingredients="seeds",
    )

    add(
        "473328",
        "Schar Vital dark sliced bread 350 g gluten-free",
        "11000",
        3.40,
        brand="Schar",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="water, rice flour, buckwheat",
    )
    add(
        "e2e-bread-wheat",
        "Fazer wheat toast bread 500 g",
        "11000",
        1.80,
        brand="Fazer",
        allergen_states={"gluten": "contains"},
        ingredients="wheat flour, water, yeast",
    )
    add(
        "e2e-bread-rye",
        "Vaasan sliced rye bread 500 g",
        "11000",
        1.70,
        brand="Vaasan",
        allergen_states={"gluten": "contains"},
        ingredients="wholegrain rye, water, salt",
    )
    add(
        "e2e-bread-rye-may",
        "Artisan rye bread 500 g",
        "11000",
        1.90,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="buckwheat, water",
    )
    add(
        "e2e-bread-rye-unk",
        "Unverified rye bread 500 g",
        "11000",
        1.50,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="dark loaf",
    )
    add(
        "e2e-bread-rye-inf",
        "Inferred rye style bread 500 g",
        "11000",
        1.45,
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="rice flour, molasses",
    )
    add(
        "e2e-bread-may",
        "Shared-bakery sliced bread 500 g",
        "11000",
        1.60,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, water",
    )
    add(
        "e2e-bread-unk",
        "Unverified sliced bread 500 g",
        "11000",
        1.40,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="flour, water",
    )
    add(
        "e2e-bread-inf",
        "Inferred gluten-free bread 500 g",
        "11000",
        1.35,
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="corn starch, water",
    )

    add(
        "e2e-flour-wheat",
        "Sunnuntai wheat flour 2 kg",
        "2130",
        1.75,
        brand="Sunnuntai",
        allergen_states={"gluten": "contains"},
        ingredients="wheat flour",
    )
    add(
        "e2e-flour-nuttrap",
        "Millhouse wheat flour 2 kg",
        "2130",
        1.35,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "may_contain"},
        ingredients="wheat flour",
    )
    add(
        "e2e-flour-may",
        "Cross-line gluten-free flour mix 1 kg",
        "2131",
        1.65,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="rice flour, potato starch",
    )
    add(
        "e2e-flour-unk",
        "Unverified gluten-free flour mix 1 kg",
        "2131",
        1.55,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="rice flour, corn starch",
    )
    add(
        "e2e-flour-inf",
        "Inferred gluten-free flour mix 1 kg",
        "2131",
        1.50,
        brand="Generic",
        inferred_groups=("gluten",),
        ingredients="rice flour, potato starch",
    )
    add(
        "e2e-flour-safe",
        "Semper gluten-free flour mix 1 kg",
        "2131",
        3.10,
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="rice flour, potato starch, corn starch",
    )

    add(
        "e2e-eggs-trap",
        "Cream-blended liquid eggs 1 kg",
        "10590",
        2.90,
        brand="Generic",
        allergen_states={"eggs": "contains", "milk": "may_contain", "gluten": "may_contain"},
        ingredients="egg, cream",
    )
    add(
        "e2e-eggs-safe",
        "Kultamuna pasteurised liquid eggs 1 kg",
        "10590",
        4.10,
        brand="Kultamuna",
        allergen_states={"eggs": "contains"},
        ingredients="pasteurised egg",
    )
    add(
        "e2e-sugar-trap",
        "Unverified bakery sugar 2 kg",
        "22100",
        1.20,
        brand="Generic",
        allergen_states={"milk": "unknown", "nuts": "may_contain", "gluten": "unknown"},
        ingredients="sugar",
    )
    add(
        "e2e-sugar-safe",
        "Dansukker granulated sugar 2 kg",
        "22100",
        2.40,
        brand="Dansukker",
        allergen_states={},
        ingredients="sugar",
    )
    add(
        "e2e-oatflakes-reg",
        "Elovena oat flakes 1 kg",
        "21370",
        1.90,
        brand="Elovena",
        allergen_states={"gluten": "contains"},
        ingredients="rolled oats",
    )
    add(
        "e2e-oatflakes-nuttrap",
        "Nut-mill oat flakes 1 kg",
        "21370",
        1.20,
        brand="Generic",
        allergen_states={"gluten": "contains", "nuts": "may_contain"},
        ingredients="rolled oats",
    )
    add(
        "e2e-gfoats-unk",
        "Unverified gluten-free oats 1 kg",
        "2137",
        1.50,
        brand="Generic",
        allergen_states={"gluten": "unknown"},
        ingredients="oats",
    )
    add(
        "e2e-gfoats-safe",
        "Provenance gluten-free oats 1 kg",
        "2137",
        2.70,
        brand="Provenance",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="pure oats",
    )
    add(
        "e2e-pasta-reg",
        "Myllyn Paras penne pasta 2 kg",
        "21380",
        2.30,
        brand="Myllyn Paras",
        allergen_states={"gluten": "contains"},
        ingredients="durum wheat",
    )
    add(
        "e2e-gfpasta-may",
        "Shared-mill gluten-free pasta 1 kg",
        "21380",
        1.85,
        brand="Generic",
        allergen_states={"gluten": "may_contain"},
        ingredients="corn flour, rice flour",
    )
    add(
        "e2e-gfpasta-safe",
        "Semper gluten-free pasta 1 kg",
        "21380",
        3.40,
        brand="Semper",
        claims=("FREE_FROM_GLUTEN",),
        allergen_states={},
        ingredients="corn flour, rice flour",
    )
    add(
        "e2e-chicken-safe",
        "Kariniemen chicken fillet 2 kg",
        "1875",
        13.20,
        brand="Kariniemen",
        allergen_states={},
        ingredients="chicken fillet, salt",
    )
    add(
        "e2e-onion-safe",
        "Peeled yellow onion 2 kg",
        "17405",
        2.30,
        brand="Tuore",
        allergen_states={},
        ingredients="onion",
    )

    return products, prices, inferred_patches


class StaticOfferBackendClient:
    base_url = "static://mcp-buyer-allergen-e2e"
    ttl_s = 300

    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.stats = {"upstream_calls": 0}

    def _iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _wrap(self, items: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        self.stats["upstream_calls"] += 1
        return {
            **meta,
            "products": [{**it, "_fetched_at": now, "_meta": meta} for it in items],
            "_oldest_fetch": now,
        }

    def price(
        self,
        product_ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        valid_from = "2026-11-01T00:00:00+0200"
        valid_to = "9999-12-31T00:00:00+0200"
        items: list[dict[str, Any]] = []
        for pid in product_ids:
            s_pid = str(pid)
            net = float(self.prices.get(s_pid, 2.50))
            gross = round(net * 1.135, 2)
            items.append(
                {
                    "productId": s_pid,
                    "quantity": 1.0,
                    "catalogPrice": {
                        "value": net,
                        "valueWithTax": gross,
                        "validFrom": valid_from,
                        "validTo": valid_to,
                        "currency": "EUR",
                    },
                    "priceWithTax": {"value": gross, "validFrom": valid_from, "validTo": valid_to, "currency": "EUR"},
                    "listPrice": {
                        "value": net,
                        "valueWithTax": gross,
                        "validFrom": valid_from,
                        "validTo": valid_to,
                        "currency": "EUR",
                    },
                    "tax": {"taxRate": 13.5, "taxValue": round(gross - net, 2)},
                    "netPriceFound": False,
                    "error": False,
                }
            )
        return self._wrap(
            items,
            {
                "customerId": customer or "demo",
                "currency": "EUR",
                "pricingDate": self._iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
            },
        )

    def availability(
        self,
        product_ids: list[str],
        requested_date: date,
        customer: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        close = datetime.combine(requested_date - timedelta(days=1), datetime.min.time(), HKI).replace(hour=14)
        now_dt = datetime.combine(requested_date - timedelta(days=2), datetime.min.time(), HKI).replace(hour=9)
        items = [
            {
                "productId": str(pid),
                "closingTime": self._iso(close),
                "salesBlock": False,
                "logisticModel": "Stock",
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": 999999.0,
                        "availableDate": requested_date.isoformat(),
                        "salesUnit": "ST",
                    }
                ],
            }
            for pid in product_ids
        ]
        return self._wrap(
            items,
            {
                "timeNow": self._iso(now_dt),
                "requestedDate": self._iso(datetime.combine(requested_date, datetime.min.time(), HKI)),
            },
        )


def _close_cached_catalog_con() -> None:
    if textsearch._con.cache_info().currsize:
        try:
            textsearch._con().close()
        except Exception:
            pass
    textsearch._con.cache_clear()
    textsearch._nonfood_categories.cache_clear()


@pytest.fixture(scope="module")
def synthetic_catalog_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    cat_dir = tmp_path_factory.mktemp("mcp_buyer_allergen_catalog")
    products, prices, inferred_patches = _synthetic_catalog_spec()
    prod_json = cat_dir / "valio_aimo_junction26_product_data.json"
    cat_db = cat_dir / "catalog.sqlite"
    rel_csv = cat_dir / "reliability.csv"
    co2_csv = cat_dir / "co2.csv"
    prod_json.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")

    mp = pytest.MonkeyPatch()
    mp.setattr(catalog_store, "PRODUCT_JSON", prod_json)
    mp.setattr(catalog_store, "CATALOG_DB", cat_db)
    mp.setattr(catalog_store, "RELIABILITY_CSV", rel_csv)
    mp.setattr(catalog_store, "CO2_CSV", co2_csv)
    try:
        catalog_store.load_catalog(cat_db, force=True)
    finally:
        mp.undo()

    con = sqlite3.connect(str(cat_db))
    try:
        for pid, (a_map, s_map) in inferred_patches.items():
            con.execute(
                "update products set allergens = ?, allergen_source = ? where id = ?",
                (json.dumps(a_map), json.dumps(s_map), pid),
            )
        con.commit()
    finally:
        con.close()

    return {
        "dir": cat_dir,
        "product_json": prod_json,
        "catalog_db": cat_db,
        "rel_csv": rel_csv,
        "co2_csv": co2_csv,
        "prices": prices,
    }


@pytest.fixture(autouse=True)
def isolated_env(synthetic_catalog_bundle: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _close_cached_catalog_con()
    if hasattr(trust_policy, "_load_policy_cached"):
        trust_policy._load_policy_cached.cache_clear()
    if hasattr(gemini.client, "cache_clear"):
        gemini.client.cache_clear()

    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    if hasattr(config, "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "AIMO_API_KEY", "")
    monkeypatch.setattr(config, "OFFLINE", True)

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("AIMO_API_KEY", "")
    monkeypatch.setenv("VALIO_API_KEY", "")
    monkeypatch.setenv("AIMO_OFFLINE", "1")
    monkeypatch.setenv("AIMO_RECIPE_CHECK", "0")
    monkeypatch.setenv("AIMO_APPROVER_PIN", APPROVER_PIN)
    monkeypatch.delenv("AIMO_LLM_MODE", raising=False)
    monkeypatch.delenv("AIMO_FAULTS", raising=False)
    monkeypatch.delenv("AIMO_REPLAY", raising=False)
    monkeypatch.delenv("AIMO_CUSTOMER_ID", raising=False)
    monkeypatch.delenv("AIMO_AGENT_ID", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEYS", raising=False)
    monkeypatch.delenv("AIMO_AGENT_KEY", raising=False)

    def forbid_gemini(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("aimo.gemini must never be called in keyless offline mode")

    monkeypatch.setattr("aimo.gemini.generate", forbid_gemini)
    monkeypatch.setattr("aimo.gemini.embed", forbid_gemini)
    monkeypatch.setattr(textsearch, "semantic_hits", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "similar_products", lambda *a, **k: [])
    monkeypatch.setattr(gemini_allergens, "cached_inference", lambda pid: None)

    monkeypatch.setattr(config, "DATA_DIR", synthetic_catalog_bundle["dir"])
    monkeypatch.setattr(config, "PRODUCT_JSON", synthetic_catalog_bundle["product_json"])
    monkeypatch.setattr(config, "CATALOG_DB", synthetic_catalog_bundle["catalog_db"])
    monkeypatch.setattr(config, "RELIABILITY_CSV", synthetic_catalog_bundle["rel_csv"])
    monkeypatch.setattr(config, "CO2_CSV", synthetic_catalog_bundle["co2_csv"])
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", synthetic_catalog_bundle["product_json"])
    monkeypatch.setattr(catalog_store, "CATALOG_DB", synthetic_catalog_bundle["catalog_db"])
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", synthetic_catalog_bundle["rel_csv"])
    monkeypatch.setattr(catalog_store, "CO2_CSV", synthetic_catalog_bundle["co2_csv"])
    monkeypatch.setattr(cat, "_SOURCE", "full")

    state_db = tmp_path / "state.sqlite"
    monkeypatch.setattr(config, "STATE_DB", state_db)
    monkeypatch.setattr(store, "DB_PATH", state_db)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")

    human_auth.reset_for_tests()
    bc.set_client(StaticOfferBackendClient(synthetic_catalog_bundle["prices"]))
    yield
    bc.set_client(None)
    _close_cached_catalog_con()


def _assert_structured_free(pid: str, groups: tuple[str, ...]) -> None:
    prod = get_product(pid)
    assert prod is not None, f"missing catalog product {pid}"
    for g in groups:
        st = prod.allergens.get(g)
        val = st.value if hasattr(st, "value") else str(st or "unknown")
        src = prod.allergen_source.get(g)
        assert val == "free", f"product {pid} ({prod.name}) leaked {g}={val!r}"
        assert src == "structured", f"product {pid} ({prod.name}) has non-structured {g} source={src!r}"


def _assert_plan_dict_hardblock(plan: dict[str, Any], groups: tuple[str, ...]) -> None:
    need_excl = set((plan.get("need") or {}).get("excluded_allergens") or [])
    assert set(groups) <= need_excl, f"expected {groups} in plan.need.excluded_allergens, got {need_excl}"
    for ln in plan.get("lines") or []:
        _assert_structured_free(str(ln["product_id"]), groups)
    for cand in plan.get("candidates") or []:
        if cand.get("tier") != "rejected":
            pid = str((cand.get("product") or {}).get("id") or cand.get("product_id"))
            _assert_structured_free(pid, groups)


def _mcp_streamable_call(
    headers: dict[str, str],
    tool_name: str,
    args: dict[str, Any],
) -> tuple[bool, Any]:
    async def _run() -> tuple[bool, Any]:
        app = create_app(with_mcp=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers=headers,
            ) as http:
                async with Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client:
                    res = await client.call_tool(tool_name, args)
                    is_err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
                    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
                    if sc is not None and not is_err:
                        if isinstance(sc, dict) and set(sc) == {"result"}:
                            return False, sc["result"]
                        return False, sc
                    txt = "\n".join(getattr(c, "text", "") or "" for c in (res.content or []))
                    try:
                        return is_err, json.loads(txt)
                    except Exception:
                        return is_err, txt

    return asyncio.run(_run())


def _mcp_handler_call(
    headers: dict[str, str],
    tool_name: str,
    **kwargs: Any,
) -> Any:
    mcp_srv = extras.register_mcp_tools(build_mcp())
    tools = {t.name: t for t in mcp_srv._tool_manager.list_tools()}
    assert tool_name in tools, f"missing MCP tool {tool_name}"
    ctx = HeaderContext(headers)
    return tools[tool_name].fn(**kwargs, ctx=ctx)


def test_mcp_server_streamable_http_and_extras_register_13_tools() -> None:
    mcp_srv = extras.register_mcp_tools(extras.register_mcp_tools(build_mcp()))
    tool_names = [t.name for t in asyncio.run(mcp_srv.list_tools())]
    assert len(tool_names) == 13
    assert "plan_need" in tool_names
    assert "plan_recipe" in tool_names
    assert not [n for n in tool_names if "approv" in n.lower() or "decid" in n.lower()]

    async def _list_over_http() -> list[str]:
        app = create_app(with_mcp=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as http:
                async with Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client:
                    return [t.name for t in (await client.list_tools()).tools]

    http_tools = asyncio.run(_list_over_http())
    assert set(http_tools) == set(tool_names)


@pytest.mark.parametrize(("query", "qty", "unit"), SCHOOL_QUERIES)
def test_mcp_school_tampere_plan_need_hardblock_streamable_http_and_context(
    query: str,
    qty: float,
    unit: str,
) -> None:
    headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
    need_payload = {
        "customer_id": "school-tampere",
        "product_query": query,
        "excluded_allergens": [],
        "quantity": qty,
        "unit": unit,
        "delivery_by": DELIVERY.isoformat(),
    }

    is_err, plan_http = _mcp_streamable_call(headers, "plan_need", {"need": need_payload})
    assert not is_err, plan_http
    assert isinstance(plan_http, dict)
    _assert_plan_dict_hardblock(plan_http, ("milk", "nuts"))

    plan_ctx = _mcp_handler_call(headers, "plan_need", need=need_payload)
    assert isinstance(plan_ctx, dict)
    _assert_plan_dict_hardblock(plan_ctx, ("milk", "nuts"))

    if query in ("milk", "whole milk"):
        assert [ln["product_id"] for ln in plan_http["lines"]] == ["470889"]
        assert "allergen_safe_alternative" in plan_http["lines"][0]["flags"]
    elif query == "oat drink":
        assert [ln["product_id"] for ln in plan_http["lines"]] == ["470889"]
        rejected_ids = {
            (c.get("product") or {}).get("id")
            for c in plan_http["candidates"]
            if c.get("tier") == "rejected"
        }
        assert {"e2e-oat-nut", "e2e-oat-maymilk", "e2e-oat-unk", "e2e-oat-inf"} & rejected_ids
    elif query == "almonds":
        assert plan_http["lines"] == []
        assert plan_http["shortfall"] == pytest.approx(qty)
        assert all(c.get("tier") == "rejected" for c in plan_http["candidates"])


@pytest.mark.parametrize(("query", "qty", "unit"), HOSPITAL_QUERIES)
def test_mcp_hospital_meilahti_plan_need_hardblock_streamable_http_and_context(
    query: str,
    qty: float,
    unit: str,
) -> None:
    headers = {"X-Customer-Id": "hospital-meilahti", "X-Agent-Id": "buyer-1"}
    need_payload = {
        "customer_id": "hospital-meilahti",
        "product_query": query,
        "excluded_allergens": [],
        "quantity": qty,
        "unit": unit,
        "delivery_by": DELIVERY.isoformat(),
    }

    is_err, plan_http = _mcp_streamable_call(headers, "plan_need", {"need": need_payload})
    assert not is_err, plan_http
    assert isinstance(plan_http, dict)
    _assert_plan_dict_hardblock(plan_http, ("gluten",))

    plan_ctx = _mcp_handler_call(headers, "plan_need", need=need_payload)
    assert isinstance(plan_ctx, dict)
    _assert_plan_dict_hardblock(plan_ctx, ("gluten",))

    if query == "bread":
        assert [ln["product_id"] for ln in plan_http["lines"]] == ["473328"]
    elif query == "flour":
        assert [ln["product_id"] for ln in plan_http["lines"]] == ["e2e-flour-safe"]


def test_mcp_and_http_identity_binding_school_header_with_aurora_body_never_leaks() -> None:
    headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
    spoofed_need = {
        "customer_id": "hotel-aurora",
        "product_query": "milk",
        "excluded_allergens": [],
        "preferred_product_ids": ["463038"],
        "quantity": 20.0,
        "unit": "L",
        "delivery_by": DELIVERY.isoformat(),
    }

    is_err, mcp_res = _mcp_streamable_call(headers, "plan_need", {"need": spoofed_need})
    if is_err:
        err_text = json.dumps(mcp_res) if isinstance(mcp_res, dict) else str(mcp_res)
        assert "customer_mismatch" in err_text
    else:
        assert isinstance(mcp_res, dict)
        _assert_plan_dict_hardblock(mcp_res, ("milk", "nuts"))

    try:
        ctx_res = _mcp_handler_call(headers, "plan_need", need=spoofed_need)
    except MCPToolError as exc:
        assert "customer_mismatch" in str(exc)
    else:
        assert isinstance(ctx_res, dict)
        _assert_plan_dict_hardblock(ctx_res, ("milk", "nuts"))

    api = TestClient(create_app(with_mcp=False))
    http_resp = api.post("/v1/plan", headers=headers, json=spoofed_need)
    if http_resp.status_code == 403:
        assert http_resp.json()["error"] == "customer_mismatch"
    else:
        assert http_resp.status_code == 200
        _assert_plan_dict_hardblock(http_resp.json(), ("milk", "nuts"))

    smuggled_pref_need = {
        "customer_id": "school-tampere",
        "product_query": "milk",
        "excluded_allergens": [],
        "preferred_product_ids": ["463038"],
        "quantity": 20.0,
        "unit": "L",
        "delivery_by": DELIVERY.isoformat(),
    }
    is_err2, plan_smuggled = _mcp_streamable_call(headers, "plan_need", {"need": smuggled_pref_need})
    assert not is_err2, plan_smuggled
    assert isinstance(plan_smuggled, dict)
    _assert_plan_dict_hardblock(plan_smuggled, ("milk", "nuts"))
    assert [ln["product_id"] for ln in plan_smuggled["lines"]] == ["470889"]
    cand_by_id = {(c.get("product") or {}).get("id"): c for c in plan_smuggled["candidates"]}
    assert cand_by_id["463038"]["tier"] == "rejected"


@pytest.mark.parametrize(
    ("customer_id", "recipe_id", "forbidden_groups", "expected_swap_targets"),
    [
        ("school-tampere", "pancakes", ("milk", "nuts"), {"oat drink", "lactose free margarine"}),
        ("school-tampere", "oat-porridge", ("milk", "nuts"), {"oat drink", "lactose free margarine"}),
        ("school-tampere", "creamy-chicken-pasta", ("milk", "nuts"), {"oat cooking"}),
        ("hospital-meilahti", "pancakes", ("gluten",), {"gluten-free flour mix"}),
        ("hospital-meilahti", "oat-porridge", ("gluten",), {"gluten-free oats"}),
        ("hospital-meilahti", "creamy-chicken-pasta", ("gluten",), {"gluten-free pasta"}),
    ],
)
def test_mcp_plan_recipe_school_and_hospital_zero_allergen_leaks(
    customer_id: str,
    recipe_id: str,
    forbidden_groups: tuple[str, ...],
    expected_swap_targets: set[str],
) -> None:
    headers = {"X-Customer-Id": customer_id, "X-Agent-Id": "buyer-1"}
    args = {
        "recipe_id": recipe_id,
        "portions": 50,
        "delivery_by": DELIVERY.isoformat(),
        "excluded_allergens": [],
    }

    is_err, res_http = _mcp_streamable_call(headers, "plan_recipe", args)
    assert not is_err, res_http
    assert isinstance(res_http, dict)

    res_ctx = _mcp_handler_call(
        headers,
        "plan_recipe",
        recipe_id=recipe_id,
        portions=50,
        delivery_by=DELIVERY.isoformat(),
        excluded_allergens=[],
    )
    assert isinstance(res_ctx, dict)

    for res in (res_http, res_ctx):
        assert res["customer_id"] == customer_id
        assert set(forbidden_groups) <= set(res["excluded_allergens"])
        swap_targets = {s.get("to") for s in res.get("swaps") or []}
        assert expected_swap_targets <= swap_targets
        assert len(res["ingredients"]) >= 3
        assert len(res["quote_request"]["lines"]) >= 3

        for ing in res["ingredients"]:
            assert set(forbidden_groups) <= set(ing["need"]["excluded_allergens"])
            ing_plan = ing["plan"]
            assert set(forbidden_groups) <= set((ing_plan.get("need") or {}).get("excluded_allergens") or [])
            for ln in ing_plan.get("lines") or []:
                _assert_structured_free(str(ln["product_id"]), forbidden_groups)
            for cand in ing_plan.get("candidates") or []:
                if cand.get("tier") != "rejected":
                    pid = str(cand.get("product_id") or (cand.get("product") or {}).get("id"))
                    _assert_structured_free(pid, forbidden_groups)

        for qline in res["quote_request"]["lines"]:
            _assert_structured_free(str(qline["product_id"]), forbidden_groups)


def _make_buyer_transport(kind: str, customer_id: str) -> tuple[Any, TestClient]:
    api = TestClient(create_app(with_mcp=False))
    if kind == "inproc":
        return InProcessTransport("buyer-1", customer_id, mandate=mandate_for(customer_id)), api
    if kind == "http":
        return HttpTools(api, customer_id, agent_id="buyer-1"), api
    raise ValueError(f"unknown transport kind {kind!r}")


@pytest.mark.parametrize("transport_kind", ["inproc", "http"])
def test_scripted_buyer_school_tampere_milk_quotes_safe_oat_and_lands_in_pending_approval(
    transport_kind: str,
) -> None:
    transport, api = _make_buyer_transport(transport_kind, "school-tampere")
    try:
        agent = BuyerAgent(
            transport,
            "school-tampere",
            clarify=False,
            approval_timeout_s=0.04,
            poll_s=0.01,
            now=SAT,
        )
        assert agent.mock is True
        assert agent.use_gemini_parse is False
        assert agent.model == MOCK_MODEL_NAME

        res = agent.run("Order 20 L milk by Monday")
    finally:
        transport.close()

    assert res["mock"] is True
    assert res["summary"].startswith(MOCK_LABEL)
    assert res["need"]["preferences"]["parsed_by"] == "regex"
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])

    assert res["plan"] is not None
    _assert_plan_dict_hardblock(res["plan"], ("milk", "nuts"))
    assert [ln["product_id"] for ln in res["plan"]["lines"]] == ["470889"]
    assert "allergen_safe_alternative" in res["plan"]["lines"][0]["flags"]

    assert res["quote"] is not None
    assert [ln["product_id"] for ln in res["quote"]["lines"]] == ["470889"]
    assert res["quote"]["lines"][0]["qty_sales_units"] == pytest.approx(20.0)
    for ln in res["quote"]["lines"]:
        _assert_structured_free(str(ln["product_id"]), ("milk", "nuts"))

    quote_obj = Quote.model_validate(res["quote"])
    sig_ver = verify_quote_detailed(quote_obj)
    assert sig_ver["ok"] is True
    assert sig_ver["signature_valid"] is True

    assert res["status"] == "pending_approval"
    assert res["order"] is not None
    assert res["order"]["status"] == "pending_approval"
    assert res["order"]["decision"]["outcome"] == "needs_approval"
    assert res["order"]["decision"]["approver_role"] == "kitchen_manager"

    dec_resp = api.post(
        f"/v1/approvals/{res['order']['order_id']}/decision",
        headers={"X-Approver-Pin": APPROVER_PIN},
        json={
            "decision": "approve",
            "quote_hash": res["quote"]["quote_hash"],
            "approver": "liisa@school-tampere.example",
            "approver_role": "kitchen_manager",
            "note": "Confirmed safe oat drink replacement for school kitchen",
        },
    )
    assert dec_resp.status_code == 200, dec_resp.text
    placed = dec_resp.json()
    assert placed["status"] == "placed"
    assert placed["response_code"] == "AP"
    assert api.get("/v1/audit/verify").json()["ok"] is True


def test_scripted_buyer_school_tampere_milk_clarify_mode_never_quotes_dairy() -> None:
    api = TestClient(create_app(with_mcp=False))
    t_unanswered = HttpTools(api, "school-tampere", agent_id="buyer-1")
    try:
        a_unanswered = BuyerAgent(t_unanswered, "school-tampere", clarify=True, now=SAT)
        res_unanswered = a_unanswered.run("Order 20 L milk by Monday")
    finally:
        t_unanswered.close()

    assert res_unanswered["mock"] is True
    assert res_unanswered["status"] == "needs_clarification"
    assert res_unanswered["quote"] is None
    assert res_unanswered["order"] is None

    def pick_first_option(question: dict[str, Any]) -> dict[str, Any]:
        return {"option_id": question["options"][0]["id"]}

    t_answered = HttpTools(api, "school-tampere", agent_id="buyer-1")
    try:
        a_answered = BuyerAgent(
            t_answered,
            "school-tampere",
            clarify=True,
            answer_fn=pick_first_option,
            approval_timeout_s=0.04,
            poll_s=0.01,
            now=SAT,
        )
        res_answered = a_answered.run("Order 20 L milk by Monday")
    finally:
        t_answered.close()

    assert res_answered["mock"] is True
    assert res_answered["plan"] is not None
    _assert_plan_dict_hardblock(res_answered["plan"], ("milk", "nuts"))
    assert res_answered["quote"] is not None
    for ln in res_answered["quote"]["lines"]:
        _assert_structured_free(str(ln["product_id"]), ("milk", "nuts"))
    assert res_answered["status"] in ("placed", "pending_approval")
    if res_answered["status"] == "pending_approval":
        assert res_answered["order"]["decision"]["approver_role"] == "kitchen_manager"


@pytest.mark.parametrize("transport_kind", ["inproc", "http"])
def test_scripted_buyer_school_tampere_oat_drink_completes_keylessly_without_allergens(
    transport_kind: str,
) -> None:
    transport, api = _make_buyer_transport(transport_kind, "school-tampere")
    try:
        agent = BuyerAgent(transport, "school-tampere", approval_timeout_s=0.04, poll_s=0.01, now=SAT)
        assert agent.mock is True
        res = agent.run("Order 20 L oat drink by Monday")
    finally:
        transport.close()

    assert res["mock"] is True
    assert {"milk", "nuts"} <= set(res["need"]["excluded_allergens"])
    assert res["plan"] is not None
    _assert_plan_dict_hardblock(res["plan"], ("milk", "nuts"))

    assert res["quote"] is not None
    assert [ln["product_id"] for ln in res["quote"]["lines"]] == ["470889"]
    for ln in res["quote"]["lines"]:
        _assert_structured_free(str(ln["product_id"]), ("milk", "nuts"))

    assert res["status"] == "placed"
    assert res["order"] is not None
    assert res["order"]["status"] == "placed"
    assert api.get("/v1/audit/verify").json()["ok"] is True


@pytest.mark.parametrize("transport_kind", ["inproc", "http"])
def test_scripted_buyer_hospital_meilahti_bread_completes_keylessly_without_gluten(
    transport_kind: str,
) -> None:
    transport, api = _make_buyer_transport(transport_kind, "hospital-meilahti")
    try:
        agent = BuyerAgent(transport, "hospital-meilahti", approval_timeout_s=0.04, poll_s=0.01, now=SAT)
        assert agent.mock is True
        res = agent.run("Order 10 kg bread by Monday")
    finally:
        transport.close()

    assert res["mock"] is True
    assert "gluten" in set(res["need"]["excluded_allergens"])
    assert res["plan"] is not None
    _assert_plan_dict_hardblock(res["plan"], ("gluten",))

    assert res["quote"] is not None
    assert [ln["product_id"] for ln in res["quote"]["lines"]] == ["473328"]
    for ln in res["quote"]["lines"]:
        _assert_structured_free(str(ln["product_id"]), ("gluten",))

    assert res["status"] == "placed"
    assert res["order"] is not None
    assert res["order"]["status"] == "placed"
    assert api.get("/v1/audit/verify").json()["ok"] is True


def test_replay_snapshot_mcp_and_scripted_buyer_allergen_hardblock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_data = tmp_path / "replay_data"
    replay_data.mkdir(parents=True, exist_ok=True)
    _close_cached_catalog_con()

    monkeypatch.setenv("AIMO_REPLAY", "1")
    monkeypatch.setenv("AIMO_OFFLINE", "0")
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "DATA_DIR", replay_data)
    monkeypatch.setattr(config, "PRODUCT_JSON", replay_data / "valio_aimo_junction26_product_data.json")
    monkeypatch.setattr(config, "RELIABILITY_CSV", replay_data / "valio_aimo_junction26_delivery_reliability.csv")
    monkeypatch.setattr(config, "CO2_CSV", replay_data / "valio_aimo_junction26_carbon_footprint.csv")
    monkeypatch.setattr(config, "CATALOG_DB", replay_data / "catalog.sqlite")

    src = cat._use_fixture_catalog()
    assert src == "fixture-subset"
    monkeypatch.setattr(cat, "_SOURCE", src)
    monkeypatch.setattr(catalog_store, "PRODUCT_JSON", config.PRODUCT_JSON)
    monkeypatch.setattr(catalog_store, "RELIABILITY_CSV", config.RELIABILITY_CSV)
    monkeypatch.setattr(catalog_store, "CO2_CSV", config.CO2_CSV)
    monkeypatch.setattr(catalog_store, "CATALOG_DB", config.CATALOG_DB)
    bc.set_client(None)
    _close_cached_catalog_con()

    school_headers = {"X-Customer-Id": "school-tampere", "X-Agent-Id": "buyer-1"}
    is_err, milk_plan = _mcp_streamable_call(
        school_headers,
        "plan_need",
        {
            "need": {
                "customer_id": "school-tampere",
                "product_query": "milk",
                "excluded_allergens": [],
                "quantity": 20.0,
                "unit": "L",
                "delivery_by": DELIVERY.isoformat(),
            }
        },
    )
    assert not is_err, milk_plan
    _assert_plan_dict_hardblock(milk_plan, ("milk", "nuts"))
    assert [ln["product_id"] for ln in milk_plan["lines"]] == ["470889"]

    api = TestClient(create_app(with_mcp=False))
    http_tools = HttpTools(api, "school-tampere", agent_id="buyer-1")
    try:
        buyer = BuyerAgent(
            http_tools,
            "school-tampere",
            clarify=False,
            approval_timeout_s=0.04,
            poll_s=0.01,
            now=SAT,
        )
        res = buyer.run("Order 20 L milk by Monday")
    finally:
        http_tools.close()

    assert res["mock"] is True
    assert [ln["product_id"] for ln in res["quote"]["lines"]] == ["470889"]
    for ln in res["quote"]["lines"]:
        _assert_structured_free(str(ln["product_id"]), ("milk", "nuts"))
    assert res["status"] == "pending_approval"
    assert res["order"]["decision"]["approver_role"] == "kitchen_manager"
