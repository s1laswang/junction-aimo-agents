"""Backend seam: the Valio Aimo mock API client. Owner: gateway. See docs/interfaces.md.

    from aimo.backend import get_client, get_offers
    offers = get_offers(["463038", "464306"], date(2026, 11, 16))

`BackendClient` is a Protocol so another implementation (a recorded fixture client, a
real SAP/webshop adapter, Momin's client) can be swapped in with `set_client()`.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from aimo.models import Offer


@runtime_checkable
class BackendClient(Protocol):
    def price(self, product_ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        """Raw /api/price JSON ({customerId, currency, pricingDate, products[]})."""
        ...

    def availability(self, product_ids: list[str], requested_date: date, customer: str | None = None) -> dict:
        """Raw /api/availability JSON ({timeNow, requestedDate, products[]})."""
        ...


def get_client() -> BackendClient:
    from aimo.backend.client import get_client as _gc

    return _gc()


def set_client(client: BackendClient | None) -> None:
    """Swap the process-wide client (tests, alternative backends). None resets to ValioClient."""
    from aimo.backend.client import set_client as _sc

    _sc(client)


def get_offers(
    product_ids: list[str],
    requested_date: date,
    customer_id: str | None = None,
    quantities: dict[str, float] | None = None,
) -> "dict[str, Offer]":
    """Merged live price + availability as `Offer`s, keyed by product id (faults applied)."""
    from aimo.backend.client import get_offers as _go

    return _go(product_ids, requested_date, customer_id=customer_id, quantities=quantities)


__all__ = ["BackendClient", "get_client", "set_client", "get_offers"]
