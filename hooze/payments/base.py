"""Payment provider abstraction. Money is INTEGER MINOR UNITS everywhere in the platform
(kobo for NGN). savoury stored Naira as numeric and converted to kobo in exactly one place
(`to_kobo`); integer minor units removes the conversion, and the float/Decimal question, entirely."""
from __future__ import annotations
from typing import Optional, Protocol


class PaymentProvider(Protocol):
    name: str
    async def initialize(self, *, reference: str, amount_minor: int, currency: str, phone: str, metadata: dict) -> dict: ...   # -> {"authorization_url", "reference"}
    async def verify(self, reference: str) -> dict: ...                                                                          # -> {"status", "amount_minor"}
    def verify_signature(self, headers: dict, body: bytes) -> bool: ...
    def extract_reference(self, event: dict) -> Optional[str]: ...                                                               # only for successful-charge events


def fmt_money(minor: int, currency: str = "NGN") -> str:
    sym = {"NGN": "₦", "USD": "$", "GBP": "£", "GHS": "GH₵"}.get(currency, f"{currency} ")
    return f"{sym}{minor // 100:,}" if minor % 100 == 0 else f"{sym}{minor / 100:,.2f}"
