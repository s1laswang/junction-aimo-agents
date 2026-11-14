from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from typing import Any

import pytest
from google.genai import types

from aimo.buyer.agent import short_description
from aimo.buyer.need import HELSINKI, parse_need
from aimo.buyer.profiles import PROFILES, get_profile, mandate_for
from aimo.buyer.tool_schemas import TOOL_NAMES, TOOL_SCHEMAS, to_gemini_schema
from aimo.gateway.mcp_server import build_mcp
from aimo.models import Mandate

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)
FORBIDDEN_SCHEMA_KEYS = {
    "$ref",
    "$defs",
    "definitions",
    "additionalProperties",
    "title",
    "prefixItems",
    "default",
    "$schema",
}


def _collect_all_keys(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            found |= _collect_all_keys(v)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_all_keys(item)
    return found


def _collect_schema_keyword_keys(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k == "properties" and isinstance(v, dict):
                for pv in v.values():
                    found |= _collect_schema_keyword_keys(pv)
            else:
                found |= _collect_schema_keyword_keys(v)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_schema_keyword_keys(item)
    return found


def test_known_profiles_and_mandates():
    aurora = get_profile("hotel-aurora")
    assert aurora is PROFILES["hotel-aurora"]
    assert aurora["customer_id"] == "hotel-aurora"
    assert aurora["agent_id"] == "hotel-aurora/buyer-1"
    assert aurora["policy_id"] == "hotel-aurora"
    assert aurora["preferred_product_ids"] == ["463038"]
    assert aurora["excluded_allergens"] == []
    assert aurora["standing_order"] == {"product_id": "463038", "qty_per_week": 300, "unit": "L"}
    assert mandate_for("hotel-aurora") == Mandate(
        customer_id="hotel-aurora",
        agent_id="hotel-aurora/buyer-1",
        policy_id="hotel-aurora",
    )

    school = get_profile("school-tampere")
    assert school is PROFILES["school-tampere"]
    assert school["customer_id"] == "school-tampere"
    assert school["agent_id"] == "school-tampere/buyer-1"
    assert school["policy_id"] == "school-tampere"
    assert school["preferred_product_ids"] == []
    assert school["excluded_allergens"] == ["milk", "nuts"]
    assert school["standing_order"] is None
    assert mandate_for("school-tampere") == Mandate(
        customer_id="school-tampere",
        agent_id="school-tampere/buyer-1",
        policy_id="school-tampere",
    )

    hospital = get_profile("hospital-meilahti")
    assert hospital is PROFILES["hospital-meilahti"]
    assert hospital["customer_id"] == "hospital-meilahti"
    assert hospital["agent_id"] == "hospital-meilahti/buyer-1"
    assert hospital["policy_id"] == "hospital-meilahti"
    assert hospital["preferred_product_ids"] == []
    assert hospital["excluded_allergens"] == ["gluten"]
    assert hospital["standing_order"] is None
    assert mandate_for("hospital-meilahti") == Mandate(
        customer_id="hospital-meilahti",
        agent_id="hospital-meilahti/buyer-1",
        policy_id="hospital-meilahti",
    )


def test_unknown_customer_profile_and_mandate_fallback():
    unknown = get_profile("bistro-kallio")
    assert unknown["customer_id"] == "bistro-kallio"
    assert unknown["name"] == "bistro-kallio"
    assert unknown["segment"] == "unknown"
    assert unknown["agent_id"] == "bistro-kallio/buyer-1"
    assert unknown["policy_id"] == "bistro-kallio"
    assert unknown["site"] == "bistro-kallio"
    assert unknown["preferred_product_ids"] == []
    assert unknown["preferences"] == {}
    assert unknown["excluded_allergens"] == []
    assert unknown["standing_order"] is None
    assert mandate_for("bistro-kallio") == Mandate(
        customer_id="bistro-kallio",
        agent_id="bistro-kallio/buyer-1",
        policy_id="bistro-kallio",
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "oat milk",
        "kauramaito",
        "kauramaitoa",
        "havremjölk",
        "soy milk",
        "soijamaito",
        "almond milk",
        "mantelimaito",
        "coconut milk",
        "kookosmaito",
        "rice milk",
        "riisimaito",
    ],
)
def test_hotel_aurora_plant_drinks_do_not_attach_dairy_usual_product(phrase: str):
    need = parse_need(f"150 L {phrase} by Monday", "hotel-aurora", SAT, use_gemini=False)
    assert need.customer_id == "hotel-aurora"
    assert need.quantity == 150
    assert need.unit == "L"
    assert need.preferred_product_ids == []
    assert "463038" not in need.preferred_product_ids
    assert "standing_order_ratio" not in need.preferences
    assert "standing_order_qty_per_week" not in need.preferences
    assert "pack" not in need.preferences
    assert "brand" not in need.preferences


@pytest.mark.parametrize(
    "phrase,qty,expected_ratio",
    [
        ("lactose-free milk", 600, 2.0),
        ("laktoositonta maitoa", 300, 1.0),
        ("laktosfri mjölk", 450, 1.5),
        ("Valio Eila", 150, 0.5),
    ],
)
def test_hotel_aurora_dairy_milk_attaches_usual_product_and_standing_order_ratio(
    phrase: str,
    qty: int,
    expected_ratio: float,
):
    need = parse_need(f"{qty} L {phrase} by Monday", "hotel-aurora", SAT, use_gemini=False)
    assert need.customer_id == "hotel-aurora"
    assert need.quantity == qty
    assert need.unit == "L"
    assert need.preferred_product_ids == ["463038"]
    assert need.preferences.get("brand") == "Valio"
    assert need.preferences.get("pack") == "10 l novobox for the milk dispenser"
    assert need.preferences.get("standing_order_qty_per_week") == 300
    assert need.preferences.get("standing_order_ratio") == expected_ratio


def test_hotel_aurora_dairy_milk_non_litre_unit_omits_standing_order_ratio():
    need = parse_need("20 kg lactose-free milk powder by Monday", "hotel-aurora", SAT, use_gemini=False)
    assert need.unit == "KG"
    assert need.preferred_product_ids == ["463038"]
    assert "standing_order_ratio" not in need.preferences
    assert "standing_order_qty_per_week" not in need.preferences


def test_to_gemini_schema_handles_prefix_items_multi_anyof_and_nested_defs():
    raw = {
        "type": "object",
        "title": "ComplexInput",
        "additionalProperties": False,
        "properties": {
            "hetero_tuple": {
                "type": "array",
                "title": "HeteroTuple",
                "prefixItems": [
                    {"type": "string", "title": "Code"},
                    {"type": "integer", "title": "Count", "default": 1},
                ],
                "items": False,
                "minItems": 2,
                "maxItems": 2,
            },
            "homo_tuple": {
                "type": "array",
                "prefixItems": [
                    {"type": "number", "title": "Lat"},
                    {"type": "number", "title": "Lon"},
                ],
                "minItems": 2,
                "maxItems": 2,
            },
            "choice": {
                "oneOf": [
                    {"$ref": "#/$defs/Alpha"},
                    {"$ref": "#/definitions/Beta"},
                    {"type": "null"},
                ],
                "default": None,
                "title": "Choice",
                "description": "Pick alpha or beta",
            },
            "nested_container": {
                "$defs": {
                    "Alpha": {
                        "type": "object",
                        "title": "AlphaTitle",
                        "additionalProperties": False,
                        "properties": {"a": {"type": "string", "title": "A"}},
                        "required": ["a"],
                    }
                },
                "definitions": {
                    "Beta": {
                        "type": "object",
                        "title": "BetaTitle",
                        "additionalProperties": True,
                        "properties": {"b": {"type": "integer", "default": 0}},
                    }
                },
                "type": "object",
                "title": "NestedContainer",
                "properties": {
                    "sub": {"$ref": "#/$defs/Alpha"},
                    "title": {"type": "string", "title": "FieldNamedTitle"},
                },
            },
        },
    }
    original = copy.deepcopy(raw)
    cleaned = to_gemini_schema(raw)
    assert raw == original

    assert _collect_schema_keyword_keys(cleaned).isdisjoint(FORBIDDEN_SCHEMA_KEYS)
    assert cleaned["properties"]["hetero_tuple"]["items"] == {
        "anyOf": [{"type": "string"}, {"type": "integer"}]
    }
    assert "prefixItems" not in cleaned["properties"]["hetero_tuple"]
    assert cleaned["properties"]["homo_tuple"]["items"] == {"type": "number"}
    assert "prefixItems" not in cleaned["properties"]["homo_tuple"]

    choice = cleaned["properties"]["choice"]
    assert choice["description"] == "Pick alpha or beta"
    assert "oneOf" not in choice
    assert choice["anyOf"] == [
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"type": "object", "properties": {"b": {"type": "integer"}}},
    ]

    nested = cleaned["properties"]["nested_container"]
    assert nested["properties"]["sub"] == {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    }
    assert nested["properties"]["title"] == {"type": "string"}

    decl = types.FunctionDeclaration(
        name="complex_tool",
        description="Complex schema test",
        parameters_json_schema=cleaned,
    )
    tool = types.Tool(function_declarations=[decl])
    assert len(tool.function_declarations) == 1


def test_to_gemini_schema_handles_empty_and_cyclic_refs():
    assert to_gemini_schema({}) == {"type": "object", "properties": {}}
    cyclic = {
        "$defs": {
            "Node": {
                "type": "object",
                "title": "Node",
                "properties": {"next": {"$ref": "#/$defs/Node"}},
            }
        },
        "$ref": "#/$defs/Node",
    }
    cleaned = to_gemini_schema(cyclic)
    assert cleaned["type"] == "object"
    assert _collect_all_keys(cleaned).isdisjoint(FORBIDDEN_SCHEMA_KEYS)


def test_all_13_mcp_tools_convert_to_gemini_function_declarations():
    mcp_tools = asyncio.run(build_mcp().list_tools())
    assert len(mcp_tools) == 13

    names = [t.name for t in mcp_tools]
    expected = set(TOOL_NAMES) | {
        "list_recipes",
        "plan_recipe",
        "check_standing_order_drift",
        "propose_offer",
    }
    assert set(names) == expected

    decls: list[types.FunctionDeclaration] = []
    for t in mcp_tools:
        desc = short_description(t.description or "")
        assert desc
        assert len(desc) <= 321
        assert "Args:" not in desc
        assert "Returns:" not in desc

        schema = to_gemini_schema(t.input_schema)
        assert schema.get("type") == "object"
        assert "properties" in schema
        leaked = _collect_all_keys(schema) & FORBIDDEN_SCHEMA_KEYS
        assert not leaked, f"{t.name} leaked forbidden schema keys: {leaked}"

        decls.append(
            types.FunctionDeclaration(
                name=t.name,
                description=desc,
                parameters_json_schema=schema,
            )
        )

    gemini_tool = types.Tool(function_declarations=decls)
    assert gemini_tool.function_declarations is not None
    assert len(gemini_tool.function_declarations) == 13

    static_decls = [
        types.FunctionDeclaration(
            name=s["name"],
            description=short_description(s["description"]),
            parameters_json_schema=to_gemini_schema(s["input_schema"]),
        )
        for s in TOOL_SCHEMAS
    ]
    static_tool = types.Tool(function_declarations=static_decls)
    assert static_tool.function_declarations is not None
    assert len(static_tool.function_declarations) == len(TOOL_SCHEMAS)
