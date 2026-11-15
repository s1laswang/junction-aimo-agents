"""Request bodies for the gateway (HTTP and MCP share them). Response models are in aimo.models."""
from __future__ import annotations

from datetime import date, datetime
from itertools import islice
import json
import math
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, StringConstraints, field_validator

from aimo.models import Need, Tier

MAX_QTY = 1e6


def _has_control_char(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if code < 32 or 127 <= code <= 159 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _has_null_or_surrogate(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if code == 0 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _has_unsafe_text_char(s: str) -> bool:
    for ch in s:
        code = ord(ch)
        if (code < 32 and ch not in "\t\n\r") or 127 <= code <= 159 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _reject_control_chars(v: str) -> str:
    if _has_control_char(v):
        raise ValueError("control characters are not allowed")
    return v


def _reject_unsafe_text_chars(v: str) -> str:
    if _has_unsafe_text_char(v):
        raise ValueError("control characters or lone surrogates are not allowed")
    return v


def _reject_lone_surrogates(v: str) -> str:
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in v):
        raise ValueError("lone UTF-16 surrogates are not allowed")
    return v


def _reject_numeric_or_datetime_date(v: Any) -> Any:
    if isinstance(v, (bool, int, float)) or isinstance(v, datetime):
        raise ValueError("date must be an ISO YYYY-MM-DD date")
    if isinstance(v, str) and _has_control_char(v.strip()):
        raise ValueError("date must not contain control characters")
    return v


def _validate_preferences_tree(val: Any, depth: int = 0) -> None:
    if depth > 6:
        raise ValueError("preferences nesting depth exceeds 6")
    if val is None or isinstance(val, bool):
        return
    if isinstance(val, int):
        if val < -(10**12) or val > 10**12:
            raise ValueError("preferences integer value out of range")
        return
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError("preferences float value must be finite")
        return
    if isinstance(val, str):
        if len(val) > 1000 or _has_unsafe_text_char(val):
            raise ValueError("preferences string contains invalid characters or exceeds 1,000 characters")
        return
    if isinstance(val, (list, tuple)):
        if len(val) > 50:
            raise ValueError("preferences list exceeds 50 items")
        for item in val:
            _validate_preferences_tree(item, depth + 1)
        return
    if isinstance(val, dict):
        if len(val) > 50:
            raise ValueError("preferences dict exceeds 50 keys")
        for k, sub in val.items():
            if not isinstance(k, str) or len(k) > 100 or _has_control_char(k):
                raise ValueError("preferences keys must be clean strings (max 100 chars)")
            _validate_preferences_tree(sub, depth + 1)
        return
    raise ValueError("preferences contains an unsupported value type")


def validate_preferences(prefs: Any) -> None:
    if not isinstance(prefs, dict):
        raise ValueError("preferences must be a JSON object")
    _validate_preferences_tree(prefs, 0)
    fp = prefs.get("fat_pct")
    if fp is not None:
        if isinstance(fp, bool) or not isinstance(fp, (int, float, str)):
            raise ValueError("preferences.fat_pct must be a finite number between 0 and 100")
        if isinstance(fp, str) and not fp.strip():
            raise ValueError("preferences.fat_pct must be a finite number between 0 and 100")
        try:
            fpf = float(fp)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("preferences.fat_pct must be a finite number between 0 and 100") from None
        if not math.isfinite(fpf) or fpf < 0.0 or fpf > 100.0:
            raise ValueError("preferences.fat_pct must be between 0 and 100")


IsoDate = Annotated[date, BeforeValidator(_reject_numeric_or_datetime_date)]
ProductId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[^\s\x00-\x1f\x7f-\x9f]+$"),
    AfterValidator(_reject_control_chars),
]
ShortId = Annotated[str, Field(max_length=128), AfterValidator(_reject_control_chars)]
QuoteId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[^\s\x00-\x1f\x7f-\x9f]+$"),
    AfterValidator(_reject_control_chars),
]
SafeHash = Annotated[str, Field(max_length=128), AfterValidator(_reject_control_chars)]
CleanText300 = Annotated[str, Field(max_length=300), AfterValidator(_reject_unsafe_text_chars)]
NoteText2000 = Annotated[str, Field(max_length=2000), AfterValidator(_reject_lone_surrogates)]


class OffersRequest(BaseModel):
    product_ids: list[ProductId] = Field(..., min_length=1, max_length=1000, description="Valio Aimo product codes (max 1,000)")
    requested_date: IsoDate = Field(..., description="Requested delivery date (YYYY-MM-DD)")
    customer_id: Optional[str] = Field(None, description="Defaults to the X-Customer-Id header")

    model_config = {"json_schema_extra": {"examples": [
        {"product_ids": ["463038", "464306", "469363"], "requested_date": "2026-11-16", "customer_id": "hotel-aurora"}]}}


class QuoteLineIn(BaseModel):
    product_id: ProductId = Field(..., description="Valio Aimo product code")
    qty_sales_units: float = Field(
        ...,
        gt=0,
        le=MAX_QTY,
        allow_inf_nan=False,
        description="Quantity in the product's sales unit (e.g. ST = piece, KI = box). Count "
                    "units (ST, KI, RAS, BOT...) must be whole numbers unless the product's "
                    "allowedLotSize is a fractional step; > 1,000,000 is refused (422)",
    )
    tier: Optional[Tier] = Field(None, description="preferred | best | better | good (from plan_need); drives substitution rules")
    flags: list[Annotated[str, Field(max_length=100), AfterValidator(_reject_control_chars)]] = Field(
        default_factory=list,
        max_length=50,
        description="Planner flags carried into policy evaluation",
    )
    product_name: Optional[CleanText300] = Field(None)

    @field_validator("qty_sales_units", mode="before")
    @classmethod
    def _reject_bool_qty(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("qty_sales_units must be a number, not a boolean")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("qty_sales_units must be a finite number > 0")
        return v


class QuoteRequest(BaseModel):
    customer_id: Optional[str] = Field(None, description="Defaults to X-Customer-Id")
    agent_id: Optional[ShortId] = Field(None, description="Defaults to X-Agent-Id")
    delivery_date: IsoDate
    lines: list[QuoteLineIn] = Field(..., min_length=1, max_length=200)
    need: Optional[Need] = None

    @field_validator("need")
    @classmethod
    def _validate_need(cls, v: Optional[Need]) -> Optional[Need]:
        if v is None:
            return None
        qty = getattr(v, "quantity", None)
        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
            raise ValueError("quantity must be a finite number > 0")
        try:
            qf = float(qty)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("quantity must be a finite number > 0") from None
        if not math.isfinite(qf) or qf <= 0 or qf > MAX_QTY:
            raise ValueError(f"quantity must be > 0, finite, and <= {int(MAX_QTY):,}")
        unit = getattr(v, "unit", None)
        if not isinstance(unit, str) or unit not in ("L", "KG", "PCS"):
            raise ValueError("unit must be L, KG, or PCS")
        db = getattr(v, "delivery_by", None)
        if not isinstance(db, date) or isinstance(db, datetime):
            raise ValueError("delivery_by must be a valid date")
        pq = getattr(v, "product_query", "")
        if pq is not None and not isinstance(pq, str):
            raise ValueError("product_query must be a string")
        if len(pq or "") > 500 or _has_unsafe_text_char(pq or ""):
            raise ValueError("product_query must be at most 500 valid characters")
        rt = getattr(v, "raw_text", "")
        if rt is not None and not isinstance(rt, str):
            raise ValueError("raw_text must be a string")
        if len(rt or "") > 4000 or _has_unsafe_text_char(rt or ""):
            raise ValueError("raw_text must be at most 4,000 valid characters")
        for list_field in ("required_claims", "excluded_allergens"):
            seq = getattr(v, list_field, None)
            if seq is not None:
                if not isinstance(seq, (list, tuple)) or len(seq) > 50:
                    raise ValueError(f"{list_field} must be a list of at most 50 strings")
                for item in seq:
                    if not isinstance(item, str) or len(item) > 100 or _has_control_char(item):
                        raise ValueError(f"{list_field} contains an invalid entry")
        prefs = getattr(v, "preferred_product_ids", None)
        if prefs is not None:
            if not isinstance(prefs, (list, tuple)) or len(prefs) > 200:
                raise ValueError("preferred_product_ids must be a list of at most 200 product IDs")
            for pid in prefs:
                if not isinstance(pid, str):
                    raise ValueError("preferred_product_ids entries must be strings")
                sp = pid.strip()
                if not sp or len(sp) > 64 or any(c.isspace() for c in sp) or _has_control_char(sp):
                    raise ValueError("preferred_product_ids contains an invalid product ID")
        pref_map = getattr(v, "preferences", None)
        if pref_map is not None:
            validate_preferences(pref_map)
        if not (pq or "").strip() and not prefs:
            raise ValueError("product_query or preferred_product_ids is required")
        return v

    model_config = {"json_schema_extra": {"examples": [
        {"customer_id": "hotel-aurora", "agent_id": "buyer-1", "delivery_date": "2026-11-16",
         "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]}]}}


class OrderRequest(BaseModel):
    quote_id: QuoteId
    quote_hash: SafeHash = Field(..., description="Must equal the quote's quote_hash: binds the order to the exact signed quote")

    model_config = {"json_schema_extra": {"examples": [{"quote_id": "Q-20261114-1a2b3c4d", "quote_hash": "<sha256 from the quote>"}]}}


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    approver: Annotated[str, Field(max_length=200), AfterValidator(_reject_control_chars)] = Field(
        "",
        description="Human approver identity, e.g. anna@aurora. With X-Approver-Session it defaults to (and must match) the signed-in approver",
    )
    note: Optional[NoteText2000] = Field(None)
    quote_hash: SafeHash = Field(..., description="The quote_hash the approver reviewed; 409 if it does not match the order")
    approver_role: Optional[Annotated[str, Field(max_length=100), AfterValidator(_reject_control_chars)]] = Field(
        None,
        description="Role the approver acts in (e.g. procurement_manager). Defaults to the role in the customer's approver directory.",
    )
    remember: bool = Field(False, description="Learned approvals: remember this approved swap so the policy can "
                                              "auto-allow it next time (aimo.trust.learned)")
    remember_scope: Literal["product", "product_type"] = Field(
        "product", description="product = exactly this substitute; product_type = any equivalent of this kind")

    model_config = {"json_schema_extra": {"examples": [
        {"decision": "approve", "approver": "anna@hotel-aurora.example", "approver_role": "procurement_manager",
         "note": "OK for Monday", "quote_hash": "<sha256>"}]}}


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    501: "not_implemented",
    502: "bad_gateway",
    503: "unavailable",
}


def _utf8_clean(s: str) -> str:
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", errors="replace").decode("utf-8")


def _safe_str(val: Any, max_len: int = 300) -> str:
    try:
        s = str(val)
    except Exception:
        s = type(val).__name__
    return _utf8_clean(s[:max_len])


def _json_safe(val: Any, depth: int = 0) -> Any:
    if depth > 4:
        return _safe_str(val, 200)
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val if -10**15 <= val <= 10**15 else _safe_str(val, 80)
    if isinstance(val, float):
        return val if math.isfinite(val) else _safe_str(val, 40)
    if isinstance(val, str):
        return _utf8_clean(val[:300])
    if isinstance(val, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(val.items()):
            if idx >= 20:
                break
            out[_safe_str(k, 80)] = _json_safe(v, depth + 1)
        return out
    if isinstance(val, (list, tuple)):
        return [_json_safe(x, depth + 1) for x in list(val)[:20]]
    return _safe_str(val, 200)


def json_clean_payload(val: Any, depth: int = 0) -> Any:
    if depth > 12:
        return _safe_str(val, 300)
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val if -(10**100) <= val <= 10**100 else _safe_str(val, 120)
    if isinstance(val, float):
        return val if math.isfinite(val) else _safe_str(val, 40)
    if isinstance(val, str):
        return _utf8_clean(val)
    if isinstance(val, dict):
        return {_safe_str(k, 200): json_clean_payload(v, depth + 1) for k, v in list(val.items())[:500]}
    if isinstance(val, (list, tuple)):
        return [json_clean_payload(x, depth + 1) for x in list(val)[:1000]]
    return _safe_str(val, 300)


def _clean_errors(errors: list) -> list[dict]:
    out = []
    for e in errors[:50]:
        if isinstance(e, dict):
            raw_loc = e.get("loc")
            if isinstance(raw_loc, (list, tuple)):
                loc = [
                    x if (isinstance(x, int) and not isinstance(x, bool) and -10**9 <= x <= 10**9) else _safe_str(x, 120)
                    for x in raw_loc[:20]
                ]
            elif raw_loc is None or raw_loc == "":
                loc = []
            else:
                loc = [_safe_str(raw_loc, 120)]
            msg = _safe_str(e.get("msg", ""), 300)
            if not msg and isinstance(e.get("ctx"), dict) and "error" in e["ctx"]:
                msg = _safe_str(e["ctx"]["error"], 300)
            err_type = _safe_str(e.get("type", "validation_error"), 80) or "validation_error"
            item: dict[str, Any] = {"loc": loc, "msg": msg, "type": err_type}
            if "ctx" in e and e["ctx"] is not None:
                item["ctx"] = _json_safe(e["ctx"])
            out.append(item)
        else:
            out.append({"loc": [], "msg": _safe_str(e, 300), "type": type(e).__name__[:80]})
    return out


def validation_message(errors: list[dict], total: Optional[int] = None) -> str:
    n = len(errors) if total is None else max(total, len(errors))
    parts = []
    for e in errors[:5]:
        if isinstance(e, dict):
            loc = [_safe_str(x, 80) for x in (e.get("loc") or []) if x not in ("body", "query", "path", "header")]
            msg = _safe_str(e.get("msg", ""), 300)
        else:
            loc, msg = [], _safe_str(e, 300)
        parts.append(f"{'.'.join(loc) or 'request'}: {msg}")
    if not parts:
        return "Invalid request."
    more = f" (+{n - 5} more)" if n > 5 else ""
    return ("Invalid request. " + "; ".join(parts) + more)[:1000]


def validation_error_body(errors: Any) -> dict:
    if errors is None:
        total, raw_head = 0, []
    elif isinstance(errors, (list, tuple)):
        total, raw_head = len(errors), list(errors[:50])
    elif isinstance(errors, (str, bytes, dict)):
        total, raw_head = 1, [errors]
    else:
        try:
            sampled = list(islice(iter(errors), 1001))
            total, raw_head = len(sampled), sampled[:50]
        except TypeError:
            total, raw_head = 1, [errors]
    errs = _clean_errors(raw_head)
    return {"error": "validation_error", "message": validation_message(errs, total=total), "detail": errs}


def install_error_handlers(app) -> None:
    import logging

    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import ValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.requests import ClientDisconnect

    log = logging.getLogger("aimo.gateway")

    @app.exception_handler(RequestValidationError)
    async def _req_validation(_req, exc: RequestValidationError):
        try:
            raw_errors = exc.errors()
        except Exception:
            raw_errors = [_safe_str(exc, 300)]
        return JSONResponse(validation_error_body(raw_errors), status_code=422)

    @app.exception_handler(ValidationError)
    async def _model_validation(_req, exc: ValidationError):
        try:
            raw_errors = exc.errors()
        except Exception:
            raw_errors = [_safe_str(exc, 300)]
        return JSONResponse(validation_error_body(raw_errors), status_code=422)

    @app.exception_handler(json.JSONDecodeError)
    async def _json_decode(_req, exc: json.JSONDecodeError):
        detail = _safe_str(exc, 300) or "Malformed JSON"
        return JSONResponse(
            {"error": "invalid_json", "message": f"Malformed JSON in request: {_safe_str(exc.msg, 200)}", "detail": detail},
            status_code=400,
        )

    @app.exception_handler(UnicodeDecodeError)
    async def _unicode_decode(_req, exc: UnicodeDecodeError):
        detail = _safe_str(exc, 300) or "Invalid UTF-8"
        return JSONResponse(
            {"error": "invalid_encoding", "message": "Request could not be decoded as valid UTF-8.", "detail": detail},
            status_code=400,
        )

    @app.exception_handler(UnicodeEncodeError)
    async def _unicode_encode(_req, exc: UnicodeEncodeError):
        detail = _safe_str(exc, 300) or "Invalid Unicode"
        return JSONResponse(
            {"error": "invalid_encoding", "message": "Request contains invalid Unicode characters.", "detail": detail},
            status_code=422,
        )

    @app.exception_handler(OverflowError)
    async def _overflow_error(_req, exc: OverflowError):
        msg = _safe_str(exc, 300).strip() or "Numeric value out of range"
        return JSONResponse({"error": "invalid_request", "message": msg, "detail": msg}, status_code=422)

    @app.exception_handler(RecursionError)
    async def _recursion_error(_req, _exc: RecursionError):
        return JSONResponse(
            {"error": "invalid_json", "message": "Request nesting depth exceeded.", "detail": "recursion_limit"},
            status_code=400,
        )

    @app.exception_handler(ValueError)
    async def _val_error(_req, exc: ValueError):
        msg = _safe_str(exc, 300).strip() or "Invalid request value"
        return JSONResponse({"error": "invalid_request", "message": msg, "detail": msg}, status_code=422)

    @app.exception_handler(ClientDisconnect)
    async def _client_disconnect(_req, _exc: ClientDisconnect):
        return JSONResponse(
            {"error": "bad_request", "message": "Client disconnected before request body was read.", "detail": "client_disconnect"},
            status_code=400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req, exc: StarletteHTTPException):
        sc = exc.status_code if isinstance(exc.status_code, int) and not isinstance(exc.status_code, bool) and 400 <= exc.status_code <= 599 else 400
        d = exc.detail
        if (
            isinstance(d, dict)
            and isinstance(d.get("error"), str)
            and d["error"].strip()
            and isinstance(d.get("message"), str)
            and d["message"].strip()
        ):
            body = _json_safe(d)
        else:
            code = _HTTP_CODES.get(sc, f"http_{sc}")
            msg = _safe_str(d, 500).strip() if d else ""
            if not msg:
                msg = code.replace("_", " ")
            body = {"error": code, "message": msg, "detail": _json_safe(d) if isinstance(d, (dict, list)) else msg}
        raw_headers = getattr(exc, "headers", None)
        safe_headers: dict[str, str] | None = None
        if isinstance(raw_headers, dict):
            safe_headers = {}
            for hk, hv in raw_headers.items():
                ks = _safe_str(hk, 100).strip()
                vs = _safe_str(hv, 300).strip()
                if not ks or _has_control_char(ks) or _has_control_char(vs):
                    continue
                try:
                    ks.encode("ascii")
                    vs.encode("ascii")
                    safe_headers[ks] = vs
                except UnicodeEncodeError:
                    continue
        return JSONResponse(body, status_code=sc, headers=safe_headers)

    @app.exception_handler(Exception)
    async def _unhandled(req, exc: Exception):
        log.exception("unhandled error on %s %s", req.method, req.url.path)
        return JSONResponse(
            {
                "error": "internal_error",
                "message": f"Unexpected server error ({type(exc).__name__}); "
                "it has been logged. Retry, or report it with the time of the request.",
            },
            status_code=500,
        )
