"""Supplier-side and monitoring agents ported from Momin's junction26-valio (`agents/`), with credit.

- drift.py: standing-order drift detector (price creep, volume creep, availability) -> plain-language flags.
- seller.py: proactive seller agent. It can only PROPOSE; the customer's buyer side quotes and orders.
"""
