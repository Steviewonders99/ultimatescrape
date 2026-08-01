"""GA4 Data API client — no Google SDK, no key file, no gcloud.

Built for a market-research team on Windows laptops. Both halves of Google's
auth are ordinary HTTP: a refresh token exchanges for an access token at
``oauth2.googleapis.com/token``, and the Data API is REST over JSON. Using
``google-analytics-data`` would add ~40 MB of protobuf dependencies and a
native-build failure mode for exactly nothing gained.

Credentials are the same three values the internal MCP server already uses:

    GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN

Note that a refresh token is tied to the Google account that minted it, so it
stops working if that account's password changes, if access is revoked, or after
roughly six months unused. When that happens the error is ``invalid_grant`` and
the fix is a new token, not a code change.

Three API behaviours that cost people time, handled here:

* **Nine dimensions maximum** per request. The API rejects a tenth rather than
  truncating, so we fail early with a message that says which to drop.
* **GA4 returns the literal string ``(not set)``, never null.** Coercing it to
  None silently merges "unknown" into "missing" and breaks any unique key.
* **Row limits are per-request.** Anything wide needs offset pagination, which
  the client does transparently.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..config import api_key_for
from ..projconfig import project

log = logging.getLogger("uscrape.ga4")

TOKEN_URL = "https://oauth2.googleapis.com/token"
DATA_API = "https://analyticsdata.googleapis.com/v1beta"
ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"

MAX_DIMENSIONS = 9
NOT_SET = "(not set)"
PAGE_SIZE = 100_000


class GA4Error(RuntimeError):
    pass


@dataclass
class ReportResult:
    rows: list[dict] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    property_id: str = ""
    date_range: tuple[str, str] = ("", "")
    row_count: int = 0
    sampled: bool = False
    quota: dict[str, Any] = field(default_factory=dict)
    totals: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "property_id": self.property_id,
            "date_range": list(self.date_range),
            "dimensions": self.dimensions,
            "metrics": self.metrics,
            "row_count": self.row_count,
            "sampled": self.sampled,
            "totals": self.totals,
            "rows": self.rows,
        }


def resolve_date(value: str) -> str:
    """Accept ISO dates and the relative tokens the team will actually type.

    ``7d`` means a seven-day window starting six days ago; ``7dAgo`` means the
    single day seven days ago. That distinction is easy to get backwards, so both
    are supported explicitly rather than guessed at.
    """
    token = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        return token
    lowered = token.lower()
    if lowered in ("today", "yesterday"):
        return lowered
    if match := re.fullmatch(r"(\d+)dago", lowered):
        return f"{match.group(1)}daysAgo"
    if match := re.fullmatch(r"(\d+)d", lowered):
        days = int(match.group(1))
        # UTC rather than local: a report run from two timezones must cover the
        # same window, and GA4 resolves the boundary in the property's timezone
        # regardless of what we send.
        today = datetime.now(UTC).date()
        return (today - timedelta(days=max(0, days - 1))).isoformat()
    if lowered.endswith("daysago"):
        return lowered.replace("daysago", "daysAgo")
    raise GA4Error(
        f"could not read date {value!r}. Use YYYY-MM-DD, 'today', 'yesterday', "
        f"'28d' (a 28-day window) or '7dAgo' (one day, a week back)."
    )


class GA4Client:
    def __init__(self, property_id: str | None = None) -> None:
        self.client_id = api_key_for("GOOGLE_OAUTH_CLIENT_ID") or api_key_for("GOOGLE_CLIENT_ID")
        self.client_secret = api_key_for("GOOGLE_OAUTH_CLIENT_SECRET") or api_key_for(
            "GOOGLE_CLIENT_SECRET"
        )
        self.refresh_token = api_key_for("GOOGLE_OAUTH_REFRESH_TOKEN") or api_key_for(
            "GOOGLE_REFRESH_TOKEN"
        )
        self.property_id = str(
            property_id or project.ga4.default_property or api_key_for("GA4_PROPERTY_ID") or ""
        ).replace("properties/", "")
        self._token: str | None = None
        self._expires: float = 0.0
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0))

    async def __aenter__(self) -> GA4Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    async def token(self) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        if not self.configured:
            raise GA4Error(
                "GA4 needs GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and "
                "GOOGLE_OAUTH_REFRESH_TOKEN. See docs/API_KEYS.md for how to mint them."
            )
        resp = await self._http.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            detail = resp.text[:300]
            if "invalid_grant" in detail:
                raise GA4Error(
                    "refresh token rejected (invalid_grant). These expire after ~6 months "
                    "unused, and are revoked when the owner changes their Google password. "
                    "Mint a new one — see docs/API_KEYS.md."
                )
            raise GA4Error(f"token refresh failed ({resp.status_code}): {detail}")
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return self._token

    async def _post(self, url: str, body: dict) -> dict:
        for attempt in range(1, 4):
            resp = await self._http.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {await self.token()}"},
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(2**attempt * 2, 30)
                log.warning("[ga4] %s — retrying in %ds", resp.status_code, wait)
                import asyncio

                await asyncio.sleep(wait)
                continue
            if resp.status_code == 403:
                raise GA4Error(
                    f"access denied to property {self.property_id}. The account behind the "
                    f"refresh token needs at least Viewer on it. ({resp.text[:200]})"
                )
            raise GA4Error(f"GA4 API error {resp.status_code}: {resp.text[:400]}")
        raise GA4Error("GA4 API kept failing after 3 attempts")

    async def run_report(
        self,
        dimensions: Sequence[str],
        metrics: Sequence[str],
        *,
        start_date: str = "28d",
        end_date: str = "today",
        limit: int = 10_000,
        order_by_metric: str | None = None,
        descending: bool = True,
        dimension_filter: dict | None = None,
        keep_not_set: bool = False,
        property_id: str | None = None,
    ) -> ReportResult:
        pid = str(property_id or self.property_id).replace("properties/", "")
        if not pid:
            raise GA4Error(
                "no GA4 property set. Pass --property, set ga4.default_property in "
                "uscrape.toml, or set GA4_PROPERTY_ID."
            )
        dims = list(dict.fromkeys(dimensions))
        if len(dims) > MAX_DIMENSIONS:
            raise GA4Error(
                f"GA4 allows at most {MAX_DIMENSIONS} dimensions per request; you asked for "
                f"{len(dims)}: {', '.join(dims)}. Split into two reports and join them on a "
                f"shared dimension such as date."
            )

        start, end = resolve_date(start_date), resolve_date(end_date)
        body: dict[str, Any] = {
            "dateRanges": [{"startDate": start, "endDate": end}],
            "dimensions": [{"name": d} for d in dims],
            "metrics": [{"name": m} for m in metrics],
            "limit": min(limit, PAGE_SIZE),
            "returnPropertyQuota": True,
        }
        if order_by_metric:
            body["orderBys"] = [
                {"metric": {"metricName": order_by_metric}, "desc": descending}
            ]
        if dimension_filter:
            body["dimensionFilter"] = dimension_filter

        rows: list[dict] = []
        offset = 0
        quota: dict[str, Any] = {}
        totals: dict[str, Any] = {}
        sampled = False

        while len(rows) < limit:
            body["offset"] = offset
            payload = await self._post(f"{DATA_API}/properties/{pid}:runReport", body)
            dim_headers = [h["name"] for h in payload.get("dimensionHeaders", [])]
            met_headers = [h["name"] for h in payload.get("metricHeaders", [])]

            batch = payload.get("rows", [])
            for row in batch:
                record: dict[str, Any] = {}
                for name, cell in zip(dim_headers, row.get("dimensionValues", [])):
                    record[name] = cell.get("value")
                for name, cell in zip(met_headers, row.get("metricValues", [])):
                    record[name] = _numeric(cell.get("value"))
                rows.append(record)

            quota = payload.get("propertyQuota", quota)
            sampled = sampled or bool(payload.get("metadata", {}).get("samplingMetadatas"))
            if payload.get("totals"):
                total_row = payload["totals"][0]
                totals = {
                    name: _numeric(cell.get("value"))
                    for name, cell in zip(met_headers, total_row.get("metricValues", []))
                }
            total_rows = int(payload.get("rowCount", len(rows)))
            offset += len(batch)
            if not batch or offset >= min(total_rows, limit):
                break

        if not keep_not_set:
            # "(not set)" is GA4's own string for unknown. It is kept as a value
            # rather than nulled, but rows that are entirely unknown are noise in
            # a research report.
            rows = [r for r in rows if not all(str(r.get(d)) == NOT_SET for d in dims)]

        return ReportResult(
            rows=rows[:limit],
            dimensions=dims,
            metrics=list(metrics),
            property_id=pid,
            date_range=(start, end),
            row_count=len(rows[:limit]),
            sampled=sampled,
            quota=quota,
            totals=totals,
        )

    async def run_realtime(
        self,
        dimensions: Sequence[str],
        metrics: Sequence[str],
        *,
        limit: int = 100,
        property_id: str | None = None,
    ) -> ReportResult:
        pid = str(property_id or self.property_id).replace("properties/", "")
        payload = await self._post(
            f"{DATA_API}/properties/{pid}:runRealtimeReport",
            {
                "dimensions": [{"name": d} for d in dimensions],
                "metrics": [{"name": m} for m in metrics],
                "limit": limit,
            },
        )
        dim_headers = [h["name"] for h in payload.get("dimensionHeaders", [])]
        met_headers = [h["name"] for h in payload.get("metricHeaders", [])]
        rows = []
        for row in payload.get("rows", []):
            record = {n: c.get("value") for n, c in zip(dim_headers, row.get("dimensionValues", []))}
            record |= {
                n: _numeric(c.get("value")) for n, c in zip(met_headers, row.get("metricValues", []))
            }
            rows.append(record)
        return ReportResult(
            rows=rows,
            dimensions=list(dimensions),
            metrics=list(metrics),
            property_id=pid,
            date_range=("realtime", "now"),
            row_count=len(rows),
        )

    async def list_properties(self) -> list[dict]:
        """Every property the credential can see. Run this first to find IDs."""
        resp = await self._http.get(
            f"{ADMIN_API}/accountSummaries?pageSize=200",
            headers={"Authorization": f"Bearer {await self.token()}"},
        )
        if resp.status_code != 200:
            raise GA4Error(f"could not list properties ({resp.status_code}): {resp.text[:250]}")
        out: list[dict] = []
        for summary in resp.json().get("accountSummaries", []):
            for prop in summary.get("propertySummaries", []):
                out.append(
                    {
                        "property_id": prop.get("property", "").split("/")[-1],
                        "name": prop.get("displayName"),
                        "account": summary.get("displayName"),
                    }
                )
        return out

    async def metadata(self, property_id: str | None = None) -> dict:
        """Valid dimension and metric names for a property.

        Worth calling once and keeping: it is the difference between a team that
        can pick fields and a team guessing at API names.
        """
        pid = str(property_id or self.property_id).replace("properties/", "")
        resp = await self._http.get(
            f"{DATA_API}/properties/{pid}/metadata",
            headers={"Authorization": f"Bearer {await self.token()}"},
        )
        if resp.status_code != 200:
            raise GA4Error(f"metadata failed ({resp.status_code}): {resp.text[:250]}")
        payload = resp.json()
        return {
            "dimensions": [
                {"api_name": d.get("apiName"), "ui_name": d.get("uiName"), "category": d.get("category")}
                for d in payload.get("dimensions", [])
            ],
            "metrics": [
                {"api_name": m.get("apiName"), "ui_name": m.get("uiName"), "category": m.get("category")}
                for m in payload.get("metrics", [])
            ],
        }


def _numeric(value: Any) -> Any:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else round(number, 4)
