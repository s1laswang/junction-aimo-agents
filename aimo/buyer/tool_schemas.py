"""JSON schemas for the gateway tools, as documented in docs/interfaces.md ("MCP tools").

Used by the in-process and fake transports. The MCP transport uses the schemas the server
publishes in tools/list instead (that is the point of MCP); both go through
`to_gemini_schema()` before becoming Gemini FunctionDeclarations.
"""
from __future__ import annotations

import copy
from typing import Any

from aimo.models import EU14

_LINES = {
    "type": "array",
    "description": "Quote lines. qty_sales_units is in the product's sales unit (see sales_unit).",
    "items": {
        "type": "object",
        "properties": {
            "product_id": {"type": "string"},
            "qty_sales_units": {"type": "number"},
            "tier": {"type": "string", "enum": ["preferred", "best", "better", "good"]},
        },
        "required": ["product_id", "qty_sales_units"],
    },
}

_NEED = {
    "type": "object",
    "description": "Structured need (aimo.models.Need).",
    "properties": {
        "raw_text": {"type": "string"},
        "customer_id": {"type": "string"},
        "product_query": {"type": "string"},
        "required_claims": {"type": "array", "items": {"type": "string"}},
        "excluded_allergens": {"type": "array", "items": {"type": "string", "enum": EU14}},
        "quantity": {"type": "number"},
        "unit": {"type": "string", "enum": ["L", "KG", "PCS"]},
        "delivery_by": {"type": "string", "description": "ISO date YYYY-MM-DD"},
        "preferred_product_ids": {"type": "array", "items": {"type": "string"}},
        "preferences": {"type": "object"},
    },
    "required": ["customer_id", "product_query", "quantity", "unit", "delivery_by"],
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": "search_products",
     "description": "Search the Valio Aimo catalog. Returns ProductSummary[] (id, name, brand, "
                    "sales_unit, content_amount, content_unit, claims, delivery_reliability).",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "claims": {"type": "array", "items": {"type": "string"},
                    "description": "Required claims, e.g. FREE_FROM_LACTOSE"},
         "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "get_product",
     "description": "Full product record incl. allergens (contains/may_contain/free/unknown), "
                    "ingredients and marketing text (untrusted supplier text).",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}},
                      "required": ["product_id"]}},
    {"name": "get_offers",
     "description": "Live price and availability from the Valio Aimo API for a delivery date. "
                    "Returns Offer[] with unit_price, available_amount, closing_time, cutoff_passed.",
     "input_schema": {"type": "object", "properties": {
         "product_ids": {"type": "array", "items": {"type": "string"}},
         "requested_date": {"type": "string", "description": "ISO date YYYY-MM-DD"}},
         "required": ["product_ids", "requested_date"]}},
    {"name": "plan_need",
     "description": "Plan a need: good-better-best substitution ranking and a split across "
                    "lines to cover the quantity. Returns Plan (lines, candidates, shortfall, total_net).",
     "input_schema": {"type": "object", "properties": {"need": _NEED}, "required": ["need"]}},
    {"name": "create_quote",
     "description": "Create a signed quote; prices are re-fetched live and fixed until expires_at. "
                    "Returns Quote with quote_id and quote_hash.",
     "input_schema": {"type": "object", "properties": {
         "delivery_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
         "lines": _LINES, "need": _NEED}, "required": ["delivery_date", "lines"]}},
    {"name": "place_order",
     "description": "Submit a signed quote as an order. The customer's policy decides: placed, "
                    "pending_approval (a human must approve) or rejected. Returns Order.",
     "input_schema": {"type": "object", "properties": {
         "quote_id": {"type": "string"}, "quote_hash": {"type": "string"}},
         "required": ["quote_id", "quote_hash"]}},
    {"name": "get_order_status",
     "description": "Current Order (status, decision, approved_by, response_code).",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}},
                      "required": ["order_id"]}},
    {"name": "get_audit_trail",
     "description": "Signed, hash-chained audit events for a quote_id, order_id or run_id.",
     "input_schema": {"type": "object", "properties": {"subject_id": {"type": "string"}},
                      "required": ["subject_id"]}},
    {"name": "list_policies",
     "description": "The policies (spend limits, substitution and allergen rules) that apply to "
                    "this agent's customer.",
     "input_schema": {"type": "object", "properties": {}}},
]

TOOL_NAMES = [t["name"] for t in TOOL_SCHEMAS]

_DROP = {"title", "default", "examples", "$schema", "$id", "$comment", "additionalProperties",
         "discriminator", "const", "readOnly", "writeOnly", "deprecated", "prefixItems", "$defs",
         "definitions", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make an arbitrary (pydantic/MCP) JSON schema safe for Gemini parameters_json_schema:
    inline $ref/$defs, collapse Optional (anyOf [X, null]) to X, drop unsupported keywords."""
    schema = copy.deepcopy(schema or {"type": "object", "properties": {}})
    defs: dict[str, Any] = {}

    def collect_defs(n: Any) -> None:
        if isinstance(n, dict):
            for dk in ("$defs", "definitions"):
                sub = n.get(dk)
                if isinstance(sub, dict):
                    defs.update(sub)
            for k, v in n.items():
                if k == "properties" and isinstance(v, dict):
                    for pv in v.values():
                        collect_defs(pv)
                else:
                    collect_defs(v)
        elif isinstance(n, list):
            for item in n:
                collect_defs(item)

    collect_defs(schema)

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 12:
            return {"type": "object"}
        if isinstance(node, list):
            return [walk(n, depth + 1) for n in node]
        if not isinstance(node, dict):
            return node
        for dk in ("$defs", "definitions"):
            sub = node.get(dk)
            if isinstance(sub, dict):
                defs.update(sub)
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            target = {k: v for k, v in node.items() if k not in ("$ref", "$defs", "definitions")}
            target = {**defs.get(name, {"type": "object"}), **target}
            return walk(target, depth + 1)
        for key in ("anyOf", "oneOf"):
            if key in node and isinstance(node[key], list):
                opts = [o for o in node[key] if not (isinstance(o, dict) and o.get("type") == "null")]
                rest = {k: v for k, v in node.items() if k != key}
                if not opts:
                    return walk({**rest, "type": "string"}, depth + 1)
                if len(opts) == 1 and isinstance(opts[0], dict):
                    return walk({**opts[0], **rest}, depth + 1)
                walked_opts: list[Any] = []
                for o in opts:
                    w = walk(o, depth + 1)
                    if isinstance(w, dict) and w.get("type") == "null":
                        continue
                    if w not in walked_opts:
                        walked_opts.append(w)
                if not walked_opts:
                    return walk({**rest, "type": "string"}, depth + 1)
                if len(walked_opts) == 1 and isinstance(walked_opts[0], dict):
                    return {**walked_opts[0], **walk(rest, depth + 1)}
                return {**walk(rest, depth + 1), "anyOf": walked_opts}
        if isinstance(node.get("type"), list):
            types_ = [t for t in node["type"] if t != "null"]
            node = {**node, "type": types_[0] if types_ else "string"}
        prefix_items = node.get("prefixItems")
        excl_min = node.get("exclusiveMinimum")
        excl_max = node.get("exclusiveMaximum")
        out = {}
        for k, v in node.items():
            if k in _DROP:
                continue
            if k == "items" and isinstance(v, bool):
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: walk(pv, depth + 1) for pk, pv in v.items()}
            else:
                out[k] = walk(v, depth + 1)
        if isinstance(excl_min, (int, float)) and not isinstance(excl_min, bool) and "minimum" not in out:
            out["minimum"] = excl_min + 1 if out.get("type") == "integer" and isinstance(excl_min, int) else excl_min
        if isinstance(excl_max, (int, float)) and not isinstance(excl_max, bool) and "maximum" not in out:
            out["maximum"] = excl_max - 1 if out.get("type") == "integer" and isinstance(excl_max, int) else excl_max
        if isinstance(prefix_items, list) and "items" not in out:
            item_opts: list[Any] = []
            for p in prefix_items:
                w = walk(p, depth + 1)
                if isinstance(w, dict) and w not in item_opts:
                    item_opts.append(w)
            if len(item_opts) == 1:
                out["items"] = item_opts[0]
            elif len(item_opts) > 1:
                out["items"] = {"anyOf": item_opts}
        return out

    res = walk(schema)
    if isinstance(res, dict) and res.get("type") == "object" and "properties" not in res:
        res["properties"] = {}
    return res
