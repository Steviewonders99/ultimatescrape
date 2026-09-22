"""Adapters for each access path.

Five ATS adapters and four custom ones cover the whole competitor set. Every
adapter normalises to the same :class:`Listing` shape so a spreadsheet of Appen
roles and a spreadsheet of iMerit gigs line up column for column — which is the
only way rate comparison across platforms is actually usable.

Pay parsing deserves a note. Where a feed gives structured compensation we use
it; where it only gives prose we extract with a conservative regex and mark the
source, because a wrong rate is worse than a blank one when the output is a
competitive pricing comparison.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from ..config import settings
from .registry import Access, Platform, get

log = logging.getLogger("uscrape.jobboards")

_MONEY = re.compile(
    r"(?P<cur>[$€£₹]|USD|EUR|GBP|INR)\s?(?P<low>\d[\d,]*(?:\.\d+)?)(?P<lowmul>[KkMm])?"
    r"(?:\s*(?:-|–|—|to)\s*(?:[$€£₹])?\s?(?P<high>\d[\d,]*(?:\.\d+)?)(?P<highmul>[KkMm])?)?"
    r"\s*(?P<unit>/\s?(?:hr|hour|h)\b|per hour|hourly|/\s?(?:yr|year)\b|per year|annually"
    r"|/\s?task|per task|/\s?day|per day)?",
    re.IGNORECASE,
)

_MULTIPLIER = {"k": 1_000, "m": 1_000_000}

#: Pay intervals that only ever appear on crowd/contributor work. Salaried
#: corporate roles publish yearly figures; a one-time or per-hour/day/task
#: payment on a mixed board (Appen's Lever board carries both) is a gig even
#: when the platform-level flag is off.
_GIG_PAY_UNITS = {"one-time", "hour", "hour-wage", "hourly", "day", "task", "minute"}


@dataclass
class Listing:
    platform: str
    company: str
    title: str
    url: str = ""
    location: str = ""
    department: str = ""
    employment_type: str = ""
    posted_at: str = ""
    pay_min: float | None = None
    pay_max: float | None = None
    pay_currency: str = ""
    pay_unit: str = ""
    pay_raw: str = ""
    pay_source: str = ""       # "structured" | "parsed" | ""
    remote: str = ""
    worker_gig: bool = False
    external_id: str = ""
    description_excerpt: str = ""
    description_full: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def parse_pay(text: str) -> dict:
    """Best-effort pay extraction from prose. Marked as parsed, never as fact."""
    if not text:
        return {}
    match = _MONEY.search(text)
    if not match:
        return {}
    symbols = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
    currency = match.group("cur")
    unit_raw = (match.group("unit") or "").lower()
    unit = (
        "hour"
        if any(u in unit_raw for u in ("hr", "hour", "h"))
        else "year"
        if any(u in unit_raw for u in ("yr", "year", "annual"))
        else "task"
        if "task" in unit_raw
        else "day"
        if "day" in unit_raw
        else ""
    )
    try:
        low = float(match.group("low").replace(",", ""))
        high = float(match.group("high").replace(",", "")) if match.group("high") else None
    except ValueError:
        return {}

    # "$110K – $130K" must not become $110. A K/M suffix also tells us the unit:
    # nobody quotes an hourly rate in thousands.
    low_mul = _MULTIPLIER.get((match.group("lowmul") or "").lower(), 1)
    high_mul = _MULTIPLIER.get((match.group("highmul") or "").lower(), low_mul)
    low *= low_mul
    if high is not None:
        high *= high_mul
    if low_mul > 1 and not unit:
        unit = "year"

    # A bare number under 100 with no unit is more likely a version, a headcount,
    # or a percentage than a salary. Refuse it rather than publish a wrong rate
    # into a competitive pricing comparison.
    if unit == "" and low < 100:
        return {}
    return {
        "pay_min": low,
        "pay_max": high,
        "pay_currency": symbols.get(currency, currency.upper() if currency else ""),
        "pay_unit": unit,
        "pay_raw": match.group(0).strip(),
        "pay_source": "parsed",
    }


def numeric_pay(value: Any, *, currency: str = "USD", unit: str = "hour") -> dict:
    """Pay from a bare numeric field.

    Some feeds publish just ``"16"`` with the currency and period implied by
    context. Those cannot go through :func:`parse_pay`, which requires a currency
    symbol precisely so it does not invent one — here the caller supplies it.
    """
    amount = _as_float(value)
    if amount is None or amount <= 0:
        return {}
    return {
        "pay_min": amount,
        "pay_currency": currency,
        "pay_unit": unit,
        "pay_raw": str(value),
        "pay_source": "structured",
    }


def _strip_html(text: str, limit: int = 400) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = re.sub(r"&[a-z]+;|&#\d+;", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()[:limit]


def _full_text(text: str) -> str:
    """Plain text of an HTML fragment, untruncated (full-JD capture)."""
    return _strip_html(text, limit=10_000_000)


class JobBoardClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=15.0),
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/html;q=0.9,*/*;q=0.5",
            },
        )

    async def __aenter__(self) -> JobBoardClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self, platform_key: str) -> list[Listing]:
        platform = get(platform_key)
        try:
            if platform.access is Access.ATS:
                handler = {
                    "greenhouse": self._greenhouse,
                    "lever": self._lever,
                    "ashby": self._ashby,
                    "join": self._join,
                    "workday": self._workday,
                }.get(platform.ats)
                if handler is None:
                    log.warning("[jobs] no adapter for ATS %r", platform.ats)
                    return []
                return await handler(platform)
            if platform.access is Access.CUSTOM_JSON:
                handler = {
                    "outlier": self._outlier,
                    "mercor": self._mercor,
                    "micro1": self._micro1,
                    "imerit": self._imerit,
                }.get(platform.key)
                return await handler(platform) if handler else []
            if platform.access is Access.BROWSER:
                return await self._browser(platform)
        except Exception as exc:  # noqa: BLE001
            log.warning("[jobs] %s failed: %s", platform.key, exc)
        return []

    async def fetch_many(self, keys: Sequence[str]) -> list[Listing]:
        import asyncio

        batches = await asyncio.gather(*(self.fetch(k) for k in keys), return_exceptions=True)
        out: list[Listing] = []
        for key, batch in zip(keys, batches):
            if isinstance(batch, Exception):
                log.warning("[jobs] %s raised: %s", key, batch)
                continue
            out.extend(batch)
        for listing in out:
            if not listing.worker_gig and listing.pay_unit in _GIG_PAY_UNITS:
                listing.worker_gig = True
        return out

    async def fetch_all(
        self, keys: Sequence[str]
    ) -> tuple[dict[str, list[Listing]], dict[str, str]]:
        """Per-platform results AND errors — lifecycle sync needs to know
        which platforms succeeded, which fetch_many discards."""
        import asyncio

        batches = await asyncio.gather(
            *(self.fetch(k) for k in keys), return_exceptions=True
        )
        ok: dict[str, list[Listing]] = {}
        errors: dict[str, str] = {}
        for key, batch in zip(keys, batches):
            if isinstance(batch, BaseException):
                errors[key] = f"{type(batch).__name__}: {batch}"
                continue
            for listing in batch:
                if not listing.worker_gig and listing.pay_unit in _GIG_PAY_UNITS:
                    listing.worker_gig = True
            ok[key] = batch
        return ok, errors

    # ── ATS adapters ──────────────────────────────────────────────────────────

    async def _greenhouse(self, p: Platform) -> list[Listing]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{p.token}/jobs?content=true"
        resp = await self._http.get(url)
        if resp.status_code != 200:
            log.warning("[jobs] greenhouse %s → %s", p.token, resp.status_code)
            return []
        out = []
        for job in resp.json().get("jobs", []):
            content = _strip_html(job.get("content", ""))
            pay = parse_pay(content)
            meta = {m.get("name"): m.get("value") for m in job.get("metadata") or []}
            out.append(
                Listing(
                    platform=p.key,
                    company=p.company,
                    title=job.get("title", ""),
                    url=job.get("absolute_url", ""),
                    location=(job.get("location") or {}).get("name", ""),
                    department=", ".join(d.get("name", "") for d in job.get("departments") or []),
                    posted_at=(job.get("updated_at") or "")[:10],
                    external_id=str(job.get("id", "")),
                    worker_gig=p.worker_gigs,
                    description_excerpt=content[:300],
                    description_full=_full_text(job.get("content", "")),
                    employment_type=str(meta.get("Employment Type", "")),
                    **pay,
                )
            )
        return out

    async def _lever(self, p: Platform) -> list[Listing]:
        resp = await self._http.get(f"https://api.lever.co/v0/postings/{p.token}?mode=json")
        if resp.status_code != 200:
            return []
        out = []
        for job in resp.json():
            categories = job.get("categories") or {}
            salary = job.get("salaryRange") or {}
            pay: dict[str, Any] = {}
            if salary.get("min") or salary.get("max"):
                pay = {
                    "pay_min": salary.get("min"),
                    "pay_max": salary.get("max"),
                    "pay_currency": salary.get("currency", ""),
                    "pay_unit": (salary.get("interval") or "").replace("per-", ""),
                    "pay_raw": json.dumps(salary),
                    "pay_source": "structured",
                }
            else:
                pay = parse_pay(_strip_html(job.get("descriptionPlain", "")))
            out.append(
                Listing(
                    platform=p.key,
                    company=p.company,
                    title=job.get("text", ""),
                    url=job.get("hostedUrl", ""),
                    location=categories.get("location", ""),
                    department=categories.get("team", "") or categories.get("department", ""),
                    employment_type=categories.get("commitment", ""),
                    posted_at=str(job.get("createdAt", ""))[:10],
                    external_id=job.get("id", ""),
                    worker_gig=p.worker_gigs,
                    remote=categories.get("allLocations", [""])[0] if categories.get("allLocations") else "",
                    description_excerpt=_strip_html(job.get("descriptionPlain", ""), 300),
                    description_full=_full_text(job.get("descriptionPlain", "")),
                    **pay,
                )
            )
        return out

    async def _ashby(self, p: Platform) -> list[Listing]:
        # includeCompensation is off by default and is the whole reason to use Ashby.
        url = (
            f"https://api.ashbyhq.com/posting-api/job-board/{p.token}"
            f"?includeCompensation=true"
        )
        resp = await self._http.get(url)
        if resp.status_code != 200:
            return []
        out = []
        for job in resp.json().get("jobs", []):
            comp = job.get("compensation") or {}
            summary = comp.get("compensationTierSummary") or ""
            pay = parse_pay(summary) if summary else {}
            if pay:
                pay["pay_source"] = "structured"
                pay["pay_raw"] = summary
            out.append(
                Listing(
                    platform=p.key,
                    company=p.company,
                    title=job.get("title", ""),
                    url=job.get("jobUrl", ""),
                    location=job.get("location", ""),
                    department=job.get("department", "") or job.get("team", ""),
                    employment_type=job.get("employmentType", ""),
                    posted_at=str(job.get("publishedAt", ""))[:10],
                    external_id=job.get("id", ""),
                    remote="remote" if job.get("isRemote") else "",
                    worker_gig=p.worker_gigs,
                    description_excerpt=_strip_html(job.get("descriptionPlain", ""), 300),
                    description_full=_full_text(job.get("descriptionPlain", "")),
                    **pay,
                )
            )
        return out

    async def _join(self, p: Platform) -> list[Listing]:
        resp = await self._http.get(f"https://join.com/companies/{p.token}")
        if resp.status_code != 200:
            return []
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL
        )
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        jobs = _deep_find_list(data, "jobs") or []
        return [
            Listing(
                platform=p.key,
                company=p.company,
                title=j.get("title", ""),
                url=j.get("url", "") or f"https://join.com/companies/{p.token}",
                location=(j.get("location") or {}).get("city", "")
                if isinstance(j.get("location"), dict)
                else str(j.get("location", "")),
                employment_type=str(j.get("employmentType", "")),
                external_id=str(j.get("id", "")),
                worker_gig=p.worker_gigs,
            )
            for j in jobs
            if isinstance(j, dict) and j.get("title")
        ]

    async def _workday(self, p: Platform) -> list[Listing]:
        """Workday needs a per-tenant host, which differs per employer.

        Left as a stub returning nothing rather than guessing: a wrong tenant URL
        returns an empty 200 that looks identical to "no open roles".
        """
        log.info(
            "[jobs] %s uses Workday, which needs its tenant host configured; skipping",
            p.key,
        )
        return []

    # ── custom feeds ──────────────────────────────────────────────────────────

    async def _outlier(self, p: Platform) -> list[Listing]:
        resp = await self._http.post(
            "https://app.outlier.ai/internal/experts/job-board/jobs",
            json={},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return []
        payload = resp.json()
        jobs = payload.get("jobs") or payload.get("data") or []
        out = []
        for job in jobs if isinstance(jobs, list) else []:
            # payRate/hourlyRate/rate/url/description do not exist on any
            # live job object (verified 22 Sep 2026, 8/8 listings) — found
            # while checking outlier's structured-pay coverage (it read as
            # 0/8, silently, via the same "field the feed doesn't have"
            # pattern as mercor/imerit). The real fields: "maxHourlyRateUsd"
            # (a plain int, explicitly USD, 8/8 present — a ceiling, not a
            # floor; stored in pay_min like every other single-number rate
            # in this codebase, so treat it as a point estimate, not a
            # guaranteed minimum), "absolute_url", and "content" (the JD
            # HTML — "description" isn't a key at all, so JD capture was
            # silently empty for every outlier listing until this fix).
            rate = job.get("maxHourlyRateUsd")
            pay = (
                {
                    "pay_min": _as_float(rate),
                    "pay_unit": "hour",
                    "pay_currency": "USD",
                    "pay_raw": str(rate),
                    "pay_source": "structured",
                }
                if rate is not None
                else {}
            )
            content = str(job.get("content", ""))
            out.append(
                Listing(
                    platform=p.key,
                    company=p.company,
                    title=job.get("title") or job.get("name", ""),
                    url=job.get("absolute_url", "")
                    or "https://app.outlier.ai/en/expert/opportunities",
                    location=_location_text(job.get("location")) or "remote",
                    external_id=str(job.get("id", "")),
                    worker_gig=True,
                    description_excerpt=_strip_html(content, 300),
                    description_full=_full_text(content),
                    **pay,
                )
            )
        return out

    async def _mercor(self, p: Platform) -> list[Listing]:
        resp = await self._http.get("https://work.mercor.com/explore")
        if resp.status_code != 200:
            return []
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL
        )
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        listings = _deep_find_list(data, "listings") or _deep_find_list(data, "jobs") or []
        out = []
        for job in listings:
            if not isinstance(job, dict) or not job.get("title"):
                continue
            # "hourlyRate"/"payRate"/"rate" do not exist on any live job
            # object (0/374, verified). The real fields are rateMin/
            # rateMax (present on 374/374) plus payRateFrequency — values
            # observed live: hourly 332, per-task 30, one-time 9, yearly 3.
            # "hourlyPayRate" also exists in the schema but was null on
            # every listing sampled; ignored. No currency field anywhere —
            # USD assumed (Mercor is US-headquartered; same assumption the
            # pre-fix code already made).
            rate_min, rate_max = job.get("rateMin"), job.get("rateMax")
            frequency = (job.get("payRateFrequency") or "").lower()
            pay_unit = {
                "hourly": "hour", "yearly": "year",
                "per-task": "task", "one-time": "one-time",
            }.get(frequency, "")
            pay = (
                {
                    "pay_min": _as_float(rate_min),
                    "pay_max": _as_float(rate_max),
                    "pay_unit": pay_unit,
                    "pay_currency": "USD",
                    "pay_raw": f"{rate_min}-{rate_max}/{frequency}",
                    "pay_source": "structured",
                }
                if rate_min is not None
                else {}
            )
            # Mercor's __NEXT_DATA__ job objects carry no "id" field at all
            # (verified live, 375/375 listings) — the stable identifier is
            # "listingId" (e.g. "list_AAABoMZlDn5LJvVj3XBA3YGz"). The old
            # job.get("id", "") always fell through to "" for every listing,
            # so every mercor row collided on (platform="mercor",
            # external_id="") the moment more than one was fetched in the
            # same run, and every url was empty too.
            listing_id = job.get("listingId") or ""
            title = str(job.get("title", ""))
            url = f"https://work.mercor.com/jobs/{listing_id}" if listing_id else ""
            out.append(
                Listing(
                    platform=p.key,
                    company=p.company,
                    title=title,
                    url=url,
                    location=str(job.get("location", "") or "remote"),
                    # "createdAt" (e.g. "2026-09-21T23:55:06") is present on
                    # 374/374 live listings; wasn't wired up before.
                    posted_at=str(job.get("createdAt", ""))[:10],
                    external_id=_stable_external_id(listing_id, url=url, title=title),
                    worker_gig=True,
                    description_excerpt=_strip_html(str(job.get("description", "")), 300),
                    description_full=_full_text(str(job.get("description", ""))),
                    **pay,
                )
            )
        return out

    async def _micro1(self, p: Platform) -> list[Listing]:
        """micro1's job-portal API changed its contract (confirmed live
        22 Sep 2026): the old request 400s with "Invalid action! Available
        actions: get_all_jobs, ..." until the JSON body carries
        ``{"action": "get_all_jobs"}``. Pagination also moved from the
        JSON body to the URL QUERY STRING — a body ``{"page":.., "limit":
        ..}`` is silently ignored and every "page" returns the identical
        first 10 rows (confirmed: 7 body-paginated calls collected only 10
        unique job_ids); ``?page=&limit=`` in the URL is what actually
        pages through the 369 live listings (confirmed: page/limit=100
        query params collect all 369, no duplicates, natural empty-page
        stop). The response shape changed entirely too — job objects are
        now keyed job_id/job_name/apply_url/date_posted/ideal_hourly_rate
        (a {"min":..,"max":..} dict); none of the old title/hourly_rate/
        rate/salary/id/location/description field names exist anymore.

        ideal_monthly_salary_min/max and ideal_yearly_compensation exist
        in the schema but were null on all 369 live listings sampled —
        not extracted here (no real payload to build a fixture from); add
        them if they're ever observed populated. The list view carries no
        description field at all — a real one lives behind a separate
        get_job_details(job_id) call, which would be 369 extra requests
        per sync against an undocumented internal API. Left unfetched
        (description_full/excerpt honestly empty rather than a fabricated
        stand-in built from skills/domain) — flagged for a future task if
        full JD capture for micro1 is wanted.
        """
        out: list[Listing] = []
        page = 1
        while page <= 10:  # safety cap; 369 live listings need 4 pages @ limit=100
            resp = await self._http.post(
                f"https://prod-api.micro1.ai/api/v1/job/portal?page={page}&limit=100",
                json={"action": "get_all_jobs"},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                break
            jobs = (resp.json() or {}).get("data") or []
            if not jobs:
                break
            for job in jobs:
                hourly = job.get("ideal_hourly_rate") or {}
                pay = (
                    {
                        "pay_min": _as_float(hourly.get("min")),
                        "pay_max": _as_float(hourly.get("max")),
                        "pay_unit": "hour",
                        # No currency field anywhere in the payload; micro1
                        # is a US-headquartered platform — same USD
                        # assumption this adapter already made pre-fix.
                        "pay_currency": "USD",
                        "pay_raw": json.dumps(hourly),
                        "pay_source": "structured",
                    }
                    if hourly.get("min") is not None
                    else {}
                )
                title = job.get("job_name", "")
                url = job.get("apply_url", "") or "https://jobs.micro1.ai"
                out.append(
                    Listing(
                        platform=p.key,
                        company=p.company,
                        title=title,
                        url=url,
                        location=job.get("location_type") or "remote",
                        posted_at=str(job.get("date_posted", ""))[:10],
                        external_id=_stable_external_id(
                            job.get("job_id"), url=url, title=title),
                        worker_gig=True,
                        **pay,
                    )
                )
            page += 1
        return out

    async def _imerit(self, p: Platform) -> list[Listing]:
        resp = await self._http.get("https://imerit.ai/jobs.json")
        if resp.status_code != 200:
            return []
        out = []
        for job in resp.json().get("jobs", []):
            # pay_rate carries two real formats live (verified 22 Sep 2026,
            # 36 listings): a bare number like "16" — no symbol, no period,
            # no currency field anywhere, which is why the same task reads
            # 15 in the UK and 4 in Thailand — recorded as "local" currency
            # rather than assumed USD (15/36 listings); or a pre-formatted
            # "$7/Hour" string with an explicit symbol+unit (20/36
            # listings), which numeric_pay() can't parse (not a bare float)
            # but parse_pay()'s regex fallback already handles correctly as
            # USD/hour. One listing had a genuinely empty pay_rate — no
            # pay extracted, honestly (not a bug, an upstream gap).
            raw_rate = str(job.get("pay_rate", "") or "")
            pay = numeric_pay(
                raw_rate, currency=str(job.get("currency", "") or "local"), unit="hour"
            ) or parse_pay(raw_rate)
            # iMerit's jobs.json has no "id" field at all (verified live,
            # 36/36 listings) — the stable identifier is "job_id" (e.g.
            # "GLO-057"), with "slug" as a secondary native candidate. The
            # old job.get("id", "") always fell through to "" for every
            # listing, so every imerit row collided on (platform="imerit",
            # external_id="") the moment more than one was fetched.
            job_id = job.get("job_id") or job.get("slug") or ""
            title = job.get("title", "")
            url = job.get("apply_link", "") or "https://imerit.ai/jobs.json"
            out.append(
                Listing(
                    platform=p.key,
                    company=p.company,
                    title=title,
                    url=url,
                    location=job.get("location", ""),
                    # "job_type" does not exist on any live job object;
                    # the real field is "type" (e.g. "Independent
                    # Contractor (Scholar)", verified 36/36 live).
                    employment_type=job.get("type", ""),
                    # No "posted_date" field exists upstream either. The
                    # only date-shaped field is "expiry_date", which is
                    # semantically different (when the posting closes, not
                    # when it opened) AND was empty on every listing
                    # sampled — not a substitute. iMerit genuinely
                    # publishes no posted-date; left honestly empty.
                    posted_at="",
                    external_id=_stable_external_id(job_id, url=url, title=title),
                    worker_gig=True,
                    description_excerpt=_strip_html(str(job.get("description", "")), 300),
                    description_full=_full_text(str(job.get("description", ""))),
                    **pay,
                )
            )
        return out

    async def _browser(self, p: Platform) -> list[Listing]:
        from ..fetch import browser as browser_mod

        if not browser_mod.available():
            log.warning(
                "[jobs] %s sits behind bot protection and needs the browser tier — "
                'uv pip install -e ".[crawl]" && crawl4ai-setup',
                p.key,
            )
            return []
        async with browser_mod.BrowserFetcher() as bf:
            result = await bf.fetch(p.careers_url)
        if not result.ok or not result.doc:
            return []
        listings = []
        for line in result.doc.markdown.splitlines():
            text = line.strip("-* ").strip()
            if 12 < len(text) < 140 and any(
                w in text.lower()
                for w in ("analyst", "evaluator", "rater", "assessor", "annotator", "specialist")
            ):
                listings.append(
                    Listing(
                        platform=p.key,
                        company=p.company,
                        title=text,
                        url=p.careers_url,
                        worker_gig=True,
                        **parse_pay(line),
                    )
                )
        return listings


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _location_text(value: Any) -> str:
    """Extract a human-readable location string from a feed field.

    Most feeds publish location as a plain string. Outlier's job-board API
    has been observed wrapping it as ``{"name": "Remote - France"}`` instead
    of a bare string — extract the name rather than let a raw dict reach a
    TEXT column (or crash normalize.country_from_location downstream with
    ``'dict' object has no attribute 'lower'``). Any other shape (missing
    key, non-str name, list, ...) falls back to "" — an honest unknown beats
    a str(dict) repr masquerading as a location.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str):
            return name
    return ""


def _stable_external_id(native_id: Any, *, url: str, title: str) -> str:
    """Deterministic external_id for a listing.

    Prefer a native id straight from the feed — call this with whatever
    field is genuinely that feed's stable identifier. When that is
    genuinely absent (the field is empty/missing, not merely mis-named —
    fix the adapter for that case instead), derive one so lifecycle
    tracking (insert vs update vs delist, keyed on (platform, external_id))
    still works run-to-run rather than colliding on "":

      1. the URL's last path segment, if it looks like a distinct slug/tail
      2. sha256 of the URL, if a URL exists but has no usable tail
      3. sha256 of the title, as the last resort

    Never returns "" when url or title is non-empty — an empty
    external_id collides with every OTHER empty external_id on the same
    platform under the (platform, external_id) UNIQUE constraint (this is
    exactly the mercor/imerit production incident this function exists to
    prevent).
    """
    if native_id:
        return str(native_id)
    if url:
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail and tail != url:
            return tail
        return hashlib.sha256(url.encode()).hexdigest()
    if title:
        return hashlib.sha256(title.encode()).hexdigest()
    return ""


def _deep_find_list(node: Any, key: str, depth: int = 0) -> list | None:
    """Locate a named list inside a deeply nested Next.js payload."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
        for child in node.values():
            found = _deep_find_list(child, key, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for child in node[:50]:
            found = _deep_find_list(child, key, depth + 1)
            if found:
                return found
    return None
