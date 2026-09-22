"""Pure pay normalization. Deterministic code, never an LLM (spec §5.3).

Rules: hour is the base unit. Annual converts (/2080) only for salaried
corporate roles. Per-task/word/item/day units are stored but never converted
— a fudged conversion is worse than an honest NULL. Unknown or missing
currency -> NULL. Negative or >$2,000/hr -> quarantined.
"""

from __future__ import annotations

import re

from ultimatescrape.pricing.fx import FxTable

HOURS_PER_YEAR = 2080
MAX_USD_HOUR = 2000.0
HOUR_UNITS = {"hour", "hourly", "hour-wage"}
ANNUAL_UNITS = {"year", "yearly", "annual", "annually"}


def usd_hourly(pay: dict, *, worker_gig: bool, fx: FxTable) -> dict:
    out = {"pay_usd_hour_min": None, "pay_usd_hour_max": None,
           "pay_fx_as_of": None, "quarantined": False}
    lo, hi = pay.get("pay_min"), pay.get("pay_max")
    unit = (pay.get("pay_unit") or "").lower()
    currency = (pay.get("pay_currency") or "").upper()
    if lo is None:
        return out
    if lo < 0 or (hi is not None and hi < 0):
        out["quarantined"] = True
        return out

    if unit in HOUR_UNITS:
        divisor = 1.0
    elif unit in ANNUAL_UNITS and not worker_gig:
        divisor = float(HOURS_PER_YEAR)
    else:
        return out  # task/word/day/one-time/unknown: stored, never converted

    def _one(v):
        if v is None:
            return None
        hourly = float(v) / divisor
        return fx.convert(hourly, currency) if currency else None

    lo_usd, hi_usd = _one(lo), _one(hi)
    if lo_usd is None:
        return out
    if lo_usd > MAX_USD_HOUR or (hi_usd is not None and hi_usd > MAX_USD_HOUR):
        out["quarantined"] = True
        return out
    out["pay_usd_hour_min"], out["pay_usd_hour_max"] = round(lo_usd, 2), (
        None if hi_usd is None else round(hi_usd, 2))
    if currency != "USD":
        out["pay_fx_as_of"] = fx.as_of
    return out


#: Deterministic country resolution from free-text locations. Deliberately
#: only unambiguous full names/codes — anything else stays "" (unknown).
_COUNTRIES = {
    "united states": "US", "usa": "US", "u.s.": "US", "united kingdom": "GB",
    "uk": "GB", "canada": "CA", "australia": "AU", "new zealand": "NZ",
    "ireland": "IE", "germany": "DE", "france": "FR", "spain": "ES",
    "portugal": "PT", "italy": "IT", "netherlands": "NL", "poland": "PL",
    "romania": "RO", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "finland": "FI", "brazil": "BR", "mexico": "MX", "argentina": "AR",
    "colombia": "CO", "chile": "CL", "peru": "PE", "india": "IN",
    "philippines": "PH", "indonesia": "ID", "vietnam": "VN", "thailand": "TH",
    "malaysia": "MY", "singapore": "SG", "japan": "JP", "south korea": "KR",
    "china": "CN", "taiwan": "TW", "hong kong": "HK", "pakistan": "PK",
    "bangladesh": "BD", "nigeria": "NG", "kenya": "KE", "south africa": "ZA",
    "egypt": "EG", "turkey": "TR", "ukraine": "UA", "israel": "IL",
    "saudi arabia": "SA", "united arab emirates": "AE", "uae": "AE",
}


def country_from_location(location_raw: str) -> str:
    text = (location_raw or "").lower()
    for name, iso in _COUNTRIES.items():
        # word-ish boundary: avoid 'uk' matching inside 'ukraine'
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text):
            return iso
    return ""
