"""Paystack provider (ported from savoury `paystack.py`, secret is now per-tenant).

Kept exactly: signature is HMAC-SHA512 over the RAW body; the webhook is never trusted on its own —
the caller ALWAYS re-verifies with the Verify Transaction API and compares the amount (status-only
verification is the classic underpayment bug). Kept the placeholder-email fix: `.local` is a
reserved TLD Paystack rejects, which broke every checkout in savoury's first live test.
"""
from __future__ import annotations
import hmac
from typing import Optional
import httpx

from hooze.kernel.retry import with_retry
from hooze.security import hmac_sha512_hex

_BASE = "https://api.paystack.co"


class PaystackProvider:
    name = "paystack"

    def __init__(self, secret_key: str, email_domain: str = "hooze-orders.com"):
        self.secret, self.email_domain = secret_key, email_domain

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"}

    @with_retry(max_attempts=3, base_delay=1.5)
    async def initialize(self, *, reference, amount_minor, currency, phone, metadata) -> dict:
        digits = "".join(c for c in phone if c.isdigit()) or "guest"
        payload = {"email": f"wa{digits}@{self.email_domain}", "amount": int(amount_minor), "currency": currency, "reference": reference, "metadata": metadata}
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_BASE}/transaction/initialize", json=payload, headers=self._h())
        if r.status_code != 200:
            raise RuntimeError(f"Paystack initialize failed ({r.status_code}): {r.text[:200]}")
        d = r.json()["data"]
        return {"authorization_url": d["authorization_url"], "reference": d["reference"]}

    @with_retry(max_attempts=3, base_delay=1.5)
    async def verify(self, reference: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{_BASE}/transaction/verify/{reference}", headers=self._h())
        if r.status_code != 200:
            raise RuntimeError(f"Paystack verify failed ({r.status_code}): {r.text[:200]}")
        d = r.json()["data"]
        return {"status": d.get("status"), "amount_minor": int(d.get("amount", 0))}

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        sig = {k.lower(): v for k, v in headers.items()}.get("x-paystack-signature", "")
        return bool(sig and self.secret) and hmac.compare_digest(sig, hmac_sha512_hex(self.secret, body))

    def extract_reference(self, event: dict) -> Optional[str]:
        return (event.get("data") or {}).get("reference") if event.get("event") == "charge.success" else None
