"""Statutory minimum wages, per country and sub-national.

Two layers, honestly separated:

  Country  — live from ILOSTAT dataflow ``DF_EAR_INEE_CUR_NB`` (monthly minimum
             wage in local currency, PPP and USD). Keyless, global, and the
             latest year sometimes lacks the USD series, in which case we
             convert at today's ECB reference rate and say so in ``usd_basis``.
  Local    — sub-national rates (US states, Japanese prefectures, Chinese
             municipalities, Vietnamese regions, ...) have no API anywhere, so
             they live in ``minwage_local.json`` as curated rows, each carrying
             its own ``source`` URL and ``as_of`` date. Treat a stale ``as_of``
             as a prompt to re-verify, not as truth.

Countries with no statutory minimum wage at all (Italy, the UAE, the Nordics)
are reported as such rather than as an absent row — "no data" and "no minimum
wage exists" are different findings.

Hourly figures derive from monthly ones at 40 h x 4.33 weeks = 173.2 h/month,
which is ILO's own convention (their notes say so per country).
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

import httpx

log = logging.getLogger("uscrape.minwage")

_ILO_DATA = "https://sdmx.ilo.org/rest/data/ILO,DF_EAR_INEE_CUR_NB"
#: The ILO gateway 403s non-browser User-Agents on /rest/data (structure
#: endpoints are fine) and also 403s keys much beyond ~11 ref areas, so we
#: send a browser UA and batch.
_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_BATCH = 8

_FRANKFURTER = "https://api.frankfurter.dev/v1/latest"
_WORLD_BANK_FX = "https://api.worldbank.org/v2/country/{iso3}/indicator/PA.NUS.FCRF"

HOURS_PER_MONTH = 173.2  # 40 h x 4.33 weeks, ILO's published convention

#: Hard USD pegs, so a dead FX API cannot block the Gulf currencies.
_PEGGED_PER_USD = {"AED": 3.6725, "SAR": 3.75, "QAR": 3.64, "BHD": 0.376, "OMR": 0.3845}

#: Countries where the correct answer is "there is no statutory minimum wage",
#: with what sets pay floors instead. ILOSTAT simply has no row for these.
NO_STATUTORY: dict[str, str] = {
    "ITA": "none — sectoral collective agreements (CCNL) set the pay floors",
    "ARE": "none — no general statutory minimum for private-sector workers",
    "AUT": "none — near-universal collective agreement coverage",
    "DNK": "none — collective agreements",
    "FIN": "none — collective agreements",
    "SWE": "none — collective agreements",
    "NOR": "none — sectoral extended collective agreements",
    "ISL": "none — collective agreements",
    "CHE": "none federally — a few cantons legislate their own (Geneva highest)",
    "SGP": "none — Progressive Wage Model covers selected sectors only",
}

#: ISO2 -> ISO3 for the markets this team actually works, so locale codes like
#: "ja-JP" resolve without a lookup table dependency.
_ISO2_TO_3 = {
    "US": "USA", "GB": "GBR", "UK": "GBR", "FR": "FRA", "CA": "CAN", "DE": "DEU",
    "IT": "ITA", "ES": "ESP", "NL": "NLD", "BR": "BRA", "IN": "IND", "JP": "JPN",
    "KR": "KOR", "CN": "CHN", "TH": "THA", "VN": "VNM", "TR": "TUR", "IL": "ISR",
    "GR": "GRC", "AE": "ARE", "EG": "EGY", "SA": "SAU", "PT": "PRT", "PL": "POL",
    "MX": "MEX", "ID": "IDN", "PH": "PHL", "MY": "MYS", "AU": "AUS", "NZ": "NZL",
}

_CURRENCY = {
    "USA": "USD", "GBR": "GBP", "FRA": "EUR", "CAN": "CAD", "DEU": "EUR",
    "ESP": "EUR", "NLD": "EUR", "GRC": "EUR", "PRT": "EUR", "BRA": "BRL",
    "IND": "INR", "JPN": "JPY", "KOR": "KRW", "CHN": "CNY", "THA": "THB",
    "VNM": "VND", "TUR": "TRY", "ISR": "ILS", "EGY": "EGP", "SAU": "SAR",
    "ARE": "AED", "POL": "PLN", "MEX": "MXN", "IDN": "IDR", "PHL": "PHP",
    "MYS": "MYR", "AUS": "AUD", "NZL": "NZD",
}

#: Countries whose statutory minimum is set sub-nationally, so the national
#: figure ILOSTAT reports is an average or floor, not what anyone is paid.
#: (ILO's own notes say this too, but the server drops NOTE_* columns on
#: multi-country CSV queries, so the classification lives here.)
_SUBNATIONAL = {
    "USA": "Federal floor + state/city rates",
    "CAN": "Federal + provincial rates",
    "JPN": "Prefectural rates (ILO figure is the weighted average)",
    "CHN": "Provincial/municipal rates, no national rate",
    "THA": "Provincial daily rates",
    "VNM": "Four regional rates",
    "IND": "State x skill-level matrix; ILO figure is the non-binding floor",
    "IDN": "Provincial rates",
    "PHL": "Regional wage boards",
    "BRA": "National rate + higher state floors",
}

_LOCAL_PATH = Path(__file__).with_name("minwage_local.json")


def to_iso3(code: str) -> str:
    """Accept ISO3 ("JPN"), ISO2 ("JP") or a locale ("ja-JP")."""
    c = code.strip()
    if "-" in c or "_" in c:
        c = c.replace("_", "-").rsplit("-", 1)[-1]
    c = c.upper()
    return _ISO2_TO_3.get(c, c)


def local_rates(iso3: str) -> list[dict]:
    """Curated sub-national rows for one country; [] when none are curated."""
    if not _LOCAL_PATH.exists():
        return []
    data = json.loads(_LOCAL_PATH.read_text())
    return data.get(iso3, [])


class MinWageClient:
    def __init__(self, *, timeout: float = 60.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _BROWSER_UA}
        )
        self._fx_cache: dict[str, float | None] = {}

    async def __aenter__(self) -> MinWageClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    # ── FX ────────────────────────────────────────────────────────────────────

    async def usd_per_unit(self, currency: str, iso3: str) -> float | None:
        """How many USD one unit of ``currency`` buys. None if unresolvable."""
        currency = currency.upper()
        if currency == "USD":
            return 1.0
        if currency in self._fx_cache:
            return self._fx_cache[currency]
        rate: float | None = None
        if currency in _PEGGED_PER_USD:
            rate = 1.0 / _PEGGED_PER_USD[currency]
        else:
            try:  # ECB reference rates, keyless
                resp = await self._client.get(
                    _FRANKFURTER, params={"base": "USD", "symbols": currency}
                )
                if resp.status_code == 200:
                    per_usd = resp.json().get("rates", {}).get(currency)
                    if per_usd:
                        rate = 1.0 / float(per_usd)
            except httpx.HTTPError as exc:
                log.debug("frankfurter %s: %s", currency, exc)
        if rate is None:
            try:  # World Bank official rate (annual) covers non-ECB currencies (VND)
                resp = await self._client.get(
                    _WORLD_BANK_FX.format(iso3=iso3),
                    params={"format": "json", "mrnev": 1},
                )
                body = resp.json()
                if isinstance(body, list) and len(body) > 1 and body[1]:
                    val = body[1][0].get("value")
                    if val:
                        rate = 1.0 / float(val)
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("world bank fx %s: %s", iso3, exc)
        self._fx_cache[currency] = rate
        return rate

    # ── Country layer ─────────────────────────────────────────────────────────

    async def country(self, codes: list[str]) -> list[dict]:
        """Latest statutory monthly minimum wage per country.

        Returns one row per country: monthly LCU/PPP/USD, derived hourly USD,
        the minimum-wage type from ILO's notes (National / Regional / ...), and
        ``usd_basis`` saying whether USD came from ILO or today's FX.
        Countries in NO_STATUTORY come back with ``statutory: False``.
        """
        iso3s: list[str] = []
        for c in codes:
            i3 = to_iso3(c)
            if i3 not in iso3s:
                iso3s.append(i3)

        rows: dict[str, dict] = {}
        queryable = [i for i in iso3s if i not in NO_STATUTORY]
        for start in range(0, len(queryable), _BATCH):
            batch = queryable[start : start + _BATCH]
            key = "+".join(batch) + ".A.."
            resp = await self._client.get(
                f"{_ILO_DATA}/{key}", params={"format": "csv", "lastNObservations": 1}
            )
            if resp.status_code != 200 or resp.text.startswith("<"):
                log.warning("ILOSTAT batch %s -> HTTP %s", batch, resp.status_code)
                continue
            for rec in csv.DictReader(io.StringIO(resp.text)):
                iso3 = rec["REF_AREA"]
                year = rec["TIME_PERIOD"]
                row = rows.setdefault(iso3, {"iso3": iso3, "year": year})
                if year > row["year"]:  # keep only the latest period
                    row.clear()
                    row.update({"iso3": iso3, "year": year})
                elif year < row["year"]:
                    continue
                try:
                    value = float(rec["OBS_VALUE"])
                except ValueError:
                    continue
                cur = rec["CUR"]
                if cur == "CUR_TYPE_LCU":
                    row["monthly_lcu"] = value
                    note = rec.get("NOTE_CLASSIF", "")
                    for part in note.split("|"):
                        part = part.strip()
                        if part.startswith("Type of minimum wage:"):
                            row["mw_type"] = part.split(":", 1)[1].strip()
                elif cur == "CUR_TYPE_PPP":
                    row["monthly_ppp"] = value
                elif cur == "CUR_TYPE_USD":
                    row["monthly_usd"] = value
                    row["usd_basis"] = "ilostat"

        out: list[dict] = []
        for iso3 in iso3s:
            if iso3 in NO_STATUTORY:
                out.append(
                    {"iso3": iso3, "statutory": False, "note": NO_STATUTORY[iso3],
                     "source": "https://ilostat.ilo.org/topics/wages/"}
                )
                continue
            row = rows.get(iso3)
            if not row or "monthly_lcu" not in row:
                out.append(
                    {"iso3": iso3, "statutory": None,
                     "note": "no ILOSTAT row — verify whether a statutory minimum exists",
                     "source": "https://ilostat.ilo.org/topics/wages/"}
                )
                continue
            row["statutory"] = True
            row.setdefault("mw_type", _SUBNATIONAL.get(iso3, "National"))
            row["currency"] = _CURRENCY.get(iso3, "")
            if "monthly_usd" not in row and row["currency"]:
                fx = await self.usd_per_unit(row["currency"], iso3)
                if fx:
                    row["monthly_usd"] = round(row["monthly_lcu"] * fx, 2)
                    row["usd_basis"] = "fx_today"
            if "monthly_usd" in row:
                row["hourly_usd"] = round(row["monthly_usd"] / HOURS_PER_MONTH, 2)
            row["source"] = "https://ilostat.ilo.org/ (DF_EAR_INEE_CUR_NB)"
            out.append(row)
        return out

    # ── Local layer ───────────────────────────────────────────────────────────

    async def local(self, code: str) -> list[dict]:
        """Curated sub-national rows with hourly USD computed at today's FX."""
        iso3 = to_iso3(code)
        out = []
        for raw in local_rates(iso3):
            row = dict(raw)
            fx = await self.usd_per_unit(row["currency"], iso3)
            if fx:
                rate = float(row["rate"])
                if row["unit"] == "hour":
                    hourly = rate
                elif row["unit"] == "day":
                    hourly = rate / 8.0
                else:  # month
                    hourly = rate / HOURS_PER_MONTH
                row["hourly_usd"] = round(hourly * fx, 2)
            row["iso3"] = iso3
            out.append(row)
        return out

    # ── Benchmark ─────────────────────────────────────────────────────────────

    @staticmethod
    def benchmark(amount_usd: float, rows: list[dict], minutes: float | None = None) -> list[dict]:
        """Compare a flat payment against each row's minimum wage.

        Adds ``covers_minutes`` (how long the payment stays at or above the
        local minimum) and, when a task duration is given, ``implied_hourly_usd``
        and ``vs_min_wage`` (implied hourly / minimum hourly).
        """
        out = []
        for row in rows:
            row = dict(row)
            hourly = row.get("hourly_usd")
            if hourly:
                row["covers_minutes"] = round(amount_usd / hourly * 60)
                if minutes:
                    implied = amount_usd / (minutes / 60.0)
                    row["implied_hourly_usd"] = round(implied, 2)
                    row["vs_min_wage"] = round(implied / hourly, 2)
            out.append(row)
        return out
