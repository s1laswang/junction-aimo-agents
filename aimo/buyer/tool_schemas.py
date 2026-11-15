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


def _merge_schemas(parts: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    merged_props: dict[str, Any] = {}
    has_obj = False
    merged_req: list[str] = []
    for s in parts:
        if s.get("type") == "object" or "properties" in s:
            has_obj = True
        props = s.get("properties")
        if isinstance(props, dict):
            for pk, pv in props.items():
                if pk in merged_props and isinstance(merged_props[pk], dict) and isinstance(pv, dict):
                    merged_props[pk] = _merge_schemas([merged_props[pk], pv])
                else:
                    merged_props[pk] = pv
        req = s.get("required")
        if isinstance(req, list):
            for r in req:
                if isinstance(r, str) and r not in merged_req:
                    merged_req.append(r)
        for k, v in s.items():
            if k in ("properties", "required"):
                continue
            if k == "enum" and isinstance(merged.get("enum"), list) and isinstance(v, list):
                inter = [x for x in merged["enum"] if x in v]
                merged["enum"] = inter if inter else v
            else:
                merged[k] = v
    if has_obj:
        merged["type"] = "object"
        if merged_props or any("properties" in s for s in parts):
            merged["properties"] = merged_props
    if merged_req:
        merged["required"] = merged_req
    return merged or {"type": "object"}


def to_gemini_schema(schema: Any) -> dict[str, Any]:
    """Make an arbitrary (pydantic/MCP) JSON schema safe for Gemini parameters_json_schema:
    inline $ref/$defs, collapse Optional (anyOf [X, null]) to X, drop unsupported keywords."""
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    schema = copy.deepcopy(schema)
    defs: dict[str, Any] = {}
    seen_ids: set[int] = set()

    def collect_defs(n: Any) -> None:
        oid = id(n)
        if oid in seen_ids:
            return
        seen_ids.add(oid)
        if isinstance(n, dict):
            for dk in ("$defs", "definitions"):
                sub = n.get(dk)
                if isinstance(sub, dict):
                    for dname, dval in sub.items():
                        if isinstance(dval, dict):
                            defs[dname] = dval
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

    def walk(node: Any, depth: int = 0, seen_refs: frozenset[str] = frozenset({"\x23"})) -> Any:
        if depth > 12:
            return {"type": "object"}
        if isinstance(node, list):
            return [walk(n, depth + 1, seen_refs) for n in node]
        if not isinstance(node, dict):
            return node
        for dk in ("$defs", "definitions"):
            sub = node.get(dk)
            if isinstance(sub, dict):
                for dname, dval in sub.items():
                    if isinstance(dval, dict):
                        defs[dname] = dval
        if "$ref" in node:
            raw_ref = node.get("$ref")
            sibling = {k: v for k, v in node.items() if k not in ("$ref", "$defs", "definitions")}
            if not isinstance(raw_ref, str) or not raw_ref.strip():
                return walk(sibling or {"type": "object"}, depth + 1, seen_refs)
            ref_str = raw_ref.strip()
            ref_name = ref_str.rsplit("/", 1)[-1] if "/" in ref_str else ref_str
            if not ref_name or ref_str == "\x23" or ref_name in seen_refs:
                walked_sib = walk(sibling, depth + 1, seen_refs) if sibling else {}
                if isinstance(walked_sib, dict):
                    return {**walked_sib, "type": walked_sib.get("type", "object")}
                return {"type": "object"}
            resolved = defs.get(ref_name)
            if not isinstance(resolved, dict):
                resolved = {"type": "object"}
            target = {**resolved, **sibling}
            return walk(target, depth + 1, seen_refs | {ref_name})
        if "allOf" in node:
            raw_all = node.get("allOf")
            rest_raw = {k: v for k, v in node.items() if k != "allOf"}
            if not isinstance(raw_all, list):
                return walk(rest_raw or {"type": "object"}, depth + 1, seen_refs)
            parts: list[dict[str, Any]] = []
            for sub in raw_all:
                w = walk(sub, depth + 1, seen_refs)
                if isinstance(w, dict) and w:
                    parts.append(w)
            if rest_raw:
                w_rest = walk(rest_raw, depth + 1, seen_refs)
                if isinstance(w_rest, dict) and w_rest:
                    parts.append(w_rest)
            if not parts:
                return {"type": "object"}
            return _merge_schemas(parts)
        for key in ("anyOf", "oneOf"):
            if key in node:
                rest = {k: v for k, v in node.items() if k != key}
                if not isinstance(node[key], list):
                    return walk(rest or {"type": "object"}, depth + 1, seen_refs)
                opts = [
                    o for o in node[key]
                    if isinstance(o, dict)
                    and not (
                        o.get("type") == "null"
                        or ("const" in o and o["const"] is None and "enum" not in o)
                    )
                ]
                if not opts:
                    return walk({**rest, "type": "string"}, depth + 1, seen_refs)
                if len(opts) == 1:
                    return walk({**opts[0], **rest}, depth + 1, seen_refs)
                walked_opts: list[Any] = []
                for o in opts:
                    w = walk(o, depth + 1, seen_refs)
                    if not isinstance(w, dict) or not w or w.get("type") == "null":
                        continue
                    if w not in walked_opts:
                        walked_opts.append(w)
                if not walked_opts:
                    return walk({**rest, "type": "string"}, depth + 1, seen_refs)
                walked_rest = walk(rest, depth + 1, seen_refs) if rest else {}
                if not isinstance(walked_rest, dict):
                    walked_rest = {}
                if len(walked_opts) == 1:
                    return {**walked_opts[0], **walked_rest}
                return {**walked_rest, "anyOf": walked_opts}
        if isinstance(node.get("type"), list):
            types_ = [t for t in node["type"] if t != "null"]
            node = {**node, "type": types_[0] if types_ else "string"}
        has_const = "const" in node
        const_val = node.get("const")
        prefix_items = node.get("prefixItems")
        excl_min = node.get("exclusiveMinimum")
        excl_max = node.get("exclusiveMaximum")
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _DROP:
                continue
            if k == "items" and isinstance(v, bool):
                continue
            if k == "properties" and isinstance(v, dict):
                props_out: dict[str, Any] = {}
                for pk, pv in v.items():
                    wpv = walk(pv, depth + 1, seen_refs)
                    props_out[pk] = wpv if isinstance(wpv, dict) and wpv else {"type": "object"}
                out[k] = props_out
            else:
                out[k] = walk(v, depth + 1, seen_refs)
        if has_const and const_val is not None and "enum" not in out:
            out["enum"] = [const_val]
        if isinstance(excl_min, (int, float)) and not isinstance(excl_min, bool) and "minimum" not in out:
            out["minimum"] = excl_min + 1 if out.get("type") == "integer" and isinstance(excl_min, int) else excl_min
        if isinstance(excl_max, (int, float)) and not isinstance(excl_max, bool) and "maximum" not in out:
            out["maximum"] = excl_max - 1 if out.get("type") == "integer" and isinstance(excl_max, int) else excl_max
        if isinstance(prefix_items, list) and "items" not in out:
            item_opts: list[Any] = []
            for p in prefix_items:
                w = walk(p, depth + 1, seen_refs)
                if isinstance(w, dict) and w not in item_opts:
                    item_opts.append(w)
            if len(item_opts) == 1:
                out["items"] = item_opts[0]
            elif len(item_opts) > 1:
                out["items"] = {"anyOf": item_opts}
        return out

    res = walk(schema)
    if not isinstance(res, dict) or not res:
        return {"type": "object", "properties": {}}
    if "type" not in res and "anyOf" not in res and "enum" not in res:
        res["type"] = "object"
    if res.get("type") == "object" and "properties" not in res:
        res["properties"] = {}
    return res
