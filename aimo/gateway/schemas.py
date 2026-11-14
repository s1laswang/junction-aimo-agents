"""Request bodies for the gateway (HTTP and MCP share them). Response models are in aimo.models."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field
from typing_extensions import Annotated

from aimo.models import Need, Tier

# harden/S2: bounded inputs. Ids are short codes; quantities are finite, > 0 and <= 1e6 (anything larger is a
# typo or an attack and gets 422); free text is capped so nothing hostile or huge reaches the planner or the LLM.
MAX_QTY = 1e6
ProductId = Annotated[str, Field(min_length=1, max_length=64)]
ShortId = Annotated[str, Field(max_length=128)]


class OffersRequest(BaseModel):
    product_ids: list[ProductId] = Field(..., min_length=1, max_length=1000, description="Valio Aimo product codes (max 1,000)")
    requested_date: date = Field(..., description="Requested delivery date (YYYY-MM-DD)")
    customer_id: Optional[ShortId] = Field(None, description="Defaults to the X-Customer-Id header")

    model_config = {"json_schema_extra": {"examples": [
        {"product_ids": ["463038", "464306", "469363"], "requested_date": "2026-11-16", "customer_id": "hotel-aurora"}]}}


class QuoteLineIn(BaseModel):
    product_id: ProductId = Field(..., description="Valio Aimo product code")
    qty_sales_units: float = Field(..., gt=0, le=MAX_QTY, allow_inf_nan=False,
                                   description="Quantity in the product's sales unit (e.g. ST = piece, KI = box). Count "
                                               "units (ST, KI, RAS, BOT...) must be whole numbers unless the product's "
                                               "allowedLotSize is a fractional step; > 1,000,000 is refused (422)")
    tier: Optional[Tier] = Field(None, description="preferred | best | better | good (from plan_need); drives substitution rules")
    flags: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=50,
                                                               description="Planner flags carried into policy evaluation")
    product_name: Optional[str] = Field(None, max_length=300)


class QuoteRequest(BaseModel):
    customer_id: Optional[ShortId] = Field(None, description="Defaults to X-Customer-Id")
    agent_id: Optional[ShortId] = Field(None, description="Defaults to X-Agent-Id")
    delivery_date: date
    lines: list[QuoteLineIn] = Field(..., min_length=1, max_length=200)
    need: Optional[Need] = None

    model_config = {"json_schema_extra": {"examples": [
        {"customer_id": "hotel-aurora", "agent_id": "buyer-1", "delivery_date": "2026-11-16",
         "lines": [{"product_id": "464306", "qty_sales_units": 1000, "tier": "best"}]}]}}


class OrderRequest(BaseModel):
    quote_id: str = Field(..., min_length=1, max_length=100)
    quote_hash: str = Field(..., max_length=128, description="Must equal the quote's quote_hash: binds the order to the exact signed quote")

    model_config = {"json_schema_extra": {"examples": [{"quote_id": "Q-20261114-1a2b3c4d", "quote_hash": "<sha256 from the quote>"}]}}


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str = Field("", max_length=200, description="Human approver identity, e.g. anna@aurora. With X-Approver-Session it defaults to (and must match) the signed-in approver")
    note: Optional[str] = Field(None, max_length=2000)
    quote_hash: str = Field(..., max_length=128, description="The quote_hash the approver reviewed; 409 if it does not match the order")
    approver_role: Optional[str] = Field(None, max_length=100, description="Role the approver acts in (e.g. procurement_manager). "
                                         "Defaults to the role in the customer's approver directory.")
    remember: bool = Field(False, description="Learned approvals: remember this approved swap so the policy can "
                                              "auto-allow it next time (aimo.trust.learned)")
    remember_scope: Literal["product", "product_type"] = Field(
        "product", description="product = exactly this substitute; product_type = any equivalent of this kind")

    model_config = {"json_schema_extra": {"examples": [
        {"decision": "approve", "approver": "anna@hotel-aurora.example", "approver_role": "procurement_manager",
         "note": "OK for Monday", "quote_hash": "<sha256>"}]}}


# ------------------------------------------------------------------ error shape (owner: harden / S2)
# Every error the gateway returns is JSON with `error` (stable code) + `message` (plain language). Validation
# errors keep pydantic's list under `detail` (loc/msg/type, inputs dropped so huge or hostile values are not
# echoed back). Mounted by aimo.gateway.app.create_app via install_error_handlers(app).
_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
               406: "not_acceptable", 409: "conflict", 410: "gone", 413: "payload_too_large",
               415: "unsupported_media_type", 422: "invalid_request", 429: "rate_limited", 501: "not_implemented",
               503: "unavailable"}


def _clean_errors(errors: list) -> list[dict]:
    out = []
    for e in errors[:50]:
        loc = [x if isinstance(x, (int, str)) else str(x) for x in (e.get("loc") or ())]
        out.append({"loc": loc, "msg": str(e.get("msg", ""))[:300], "type": str(e.get("type", ""))[:80]})
    return out


def validation_message(errors: list[dict]) -> str:
    """'lines.0.qty_sales_units: Input should be greater than 0; ...' without the transport prefix (body/query)."""
    parts = []
    for e in errors[:5]:
        loc = [str(x) for x in e.get("loc", []) if x not in ("body", "query", "path", "header")]
        parts.append(f"{'.'.join(loc) or 'request'}: {e.get('msg')}")
    more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
    return ("Invalid request. " + "; ".join(parts) + more)[:1000]


def validation_error_body(errors: list) -> dict:
    errs = _clean_errors(list(errors))
    return {"error": "validation_error", "message": validation_message(errs), "detail": errs}


def install_error_handlers(app) -> None:
    import logging

    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import ValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    log = logging.getLogger("aimo.gateway")

    @app.exception_handler(RequestValidationError)
    async def _req_validation(_req, exc: RequestValidationError):
        return JSONResponse(validation_error_body(exc.errors()), status_code=422)

    @app.exception_handler(ValidationError)
    async def _model_validation(_req, exc: ValidationError):  # model_validate() inside a handler
        return JSONResponse(validation_error_body(exc.errors()), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req, exc: StarletteHTTPException):
        d = exc.detail
        if isinstance(d, dict) and d.get("error") and d.get("message"):
            body = d
        else:
            body = {"error": _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}"),
                    "message": str(d) if d else _HTTP_CODES.get(exc.status_code, "error").replace("_", " ")}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(req, exc: Exception):  # last resort: still JSON, never a stack trace
        log.exception("unhandled error on %s %s", req.method, req.url.path)
        return JSONResponse({"error": "internal_error", "message": f"Unexpected server error ({type(exc).__name__}); "
                             "it has been logged. Retry, or report it with the time of the request."}, status_code=500)
