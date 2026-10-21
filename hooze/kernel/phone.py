"""Phone numbers: the one place that turns what a person TYPES into what WhatsApp USES (international digits, no plus, no spaces).

Evolution reports customers as e.g. 2348012345678, and staff/alert numbers are matched against that. People type 08012345678,
+234 801 234 5678, (0801) 234-5678... A number left in the typed local form silently never matches (alerts never arrive, the owner's
`resume` command is treated as a customer message). Nigeria-first: a number WITHOUT a country code is read as Nigerian (a leading 0, or a bare 10-digit mobile starting 7/8/9).
"""
from __future__ import annotations
import re

DEFAULT_COUNTRY_CODE = "234"


def normalize_phone(raw: str, default_cc: str = DEFAULT_COUNTRY_CODE) -> str:
    s = re.sub(r"\(\s*0\s*\)", "", raw or "")          # "+234 (0) 801..." -> the optional trunk zero written in brackets
    d = re.sub(r"\D", "", s)
    if not d:
        return ""
    if d.startswith("00"):                             # international dialling prefix
        d = d[2:]
    elif d.startswith("0") and len(d) == 11:           # local form 0801 234 5678
        d = default_cc + d[1:]
    elif re.fullmatch(r"[789]\d{9}", d):                # bare 801 234 5678 (Nigerian mobiles start 7/8/9; a 10-digit "47…" is Norwegian, not ours)
        d = default_cc + d
    return d
