"""Protocol clients for national statistics APIs.

Sixty statistics agencies share about five protocols. Implementing the protocols
and describing the agencies as data is the only version of this that stays
maintainable — a bespoke client per country would be thousands of lines that rot
the moment an agency renumbers a table.

  PxWeb  — the Nordic stack, plus Ireland and several others. A POST of a query
           JSON against a table path returns JSON-stat. Once one works they all do.
  SDMX   — Eurostat, OECD, IMF, ABS, ILO. Powerful and genuinely unpleasant:
           dimension order is positional and dotted, and an empty position means
           "all".
  Census — the US Bureau's own shape: a matrix where row 0 is the header.
  OData  — CBS Netherlands and others.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from ..config import api_key_for, settings
from .base import SourceResult

log = logging.getLogger("uscrape.sources")

#: Minimum seconds between consecutive requests, per throttle-prone host.
#: ISTAT's is not a politeness margin — exceeding 5/min there earns a 1-2 day
#: IP block, so 13s keeps a safety factor even with retries.
_PACE_SECONDS: dict[str, float] = {
    "worldbank": 1.5,
    "istat": 13.0,
    "oecd": 60.0,
    "sec": 0.12,
}


class _ThrottledOrDown(Exception):
    """A 5xx/429 that should be retried after a pause."""

    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        try:
            self.retry_after = float(retry_after) if retry_after else None
        except ValueError:
            self.retry_after = None


class StatsClient:
    """Shared HTTP plumbing: retries, timeouts, and a descriptive User-Agent.

    Several agencies (SEC most strictly) reject or throttle requests without a
    contactable User-Agent, so it is set on every call rather than per source.
    """

    def __init__(self, *, timeout: float = 60.0, user_agent: str | None = None) -> None:
        self._last_call: dict[str, float] = {}
        self._pace_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent
                or api_key_for("SEC_USER_AGENT")
                or f"UltimateScrape/0.1 ({settings.referer})",
                "Accept": "application/json, text/csv;q=0.9, */*;q=0.5",
            },
        )

    async def __aenter__(self) -> StatsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self, key: str) -> None:
        """Space consecutive requests to a throttle-prone host.

        Several of these agencies enforce limits by silently degrading rather
        than returning 429 — World Bank answers a burst with 502 HTML pages and
        read timeouts, and ISTAT blocks the IP for a day or two. Spacing is
        cheaper than retrying into a block.
        """
        import asyncio

        delay = _PACE_SECONDS.get(key)
        if not delay:
            return
        async with self._pace_lock:
            last = self._last_call.get(key, 0.0)
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[key] = time.monotonic()

    async def _json(
        self, method: str, url: str, *, pace_key: str | None = None, **kwargs: Any
    ) -> Any:
        import asyncio

        last: Exception | None = None
        for attempt in range(1, 4):
            if pace_key:
                await self._pace(pace_key)
            try:
                resp = await self._client.request(method, url, **kwargs)
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("retry-after")
                    raise _ThrottledOrDown(resp.status_code, retry_after)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 3:
                    wait = getattr(exc, "retry_after", None) or (3 * attempt + 2)
                    await asyncio.sleep(wait)
        raise RuntimeError(f"{method} {url} failed after 3 attempts: {last}")

    # ── US Census Bureau ──────────────────────────────────────────────────────

    async def us_census(
        self,
        variables: Sequence[str],
        *,
        dataset: str = "2023/acs/acs5",
        for_geo: str = "state:*",
        in_geo: str | None = None,
        key: str | None = None,
    ) -> SourceResult:
        """Query the Census API.

        Two traps worth encoding rather than rediscovering: the response is a
        matrix whose first row is the header, not a list of objects; and missing
        or suppressed values come back as large negative sentinels
        (-666666666, -999999999) which will silently wreck any average.
        """
        params: dict[str, str] = {
            "get": ",".join(["NAME", *variables]),
            "for": for_geo,
        }
        if in_geo:
            params["in"] = in_geo
        api_key = key or api_key_for("CENSUS_API_KEY")
        if api_key:
            params["key"] = api_key

        url = f"https://api.census.gov/data/{dataset.strip('/')}"
        try:
            matrix = await self._json("GET", url, params=params)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source="us_census", ok=False, error=str(exc))

        if not isinstance(matrix, list) or len(matrix) < 2:
            return SourceResult(
                source="us_census", ok=False, error=f"unexpected response shape: {str(matrix)[:200]}"
            )
        header, *rows = matrix
        out = [dict(zip(header, row)) for row in rows]
        for record in out:
            for field, value in list(record.items()):
                record[field] = _census_number(value)
        return SourceResult(
            source="us_census",
            ok=True,
            rows=out,
            meta={"dataset": dataset, "geo": for_geo, "variables": list(variables), "keyed": bool(api_key)},
        )

    async def us_census_variables(self, dataset: str = "2023/acs/acs5") -> SourceResult:
        """The variable dictionary for a dataset. Essential: variable codes move
        between vintages and guessing them from memory is how swarms hallucinate."""
        url = f"https://api.census.gov/data/{dataset.strip('/')}/variables.json"
        try:
            data = await self._json("GET", url)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source="us_census_variables", ok=False, error=str(exc))
        rows = [
            {"code": code, **{k: v for k, v in meta.items() if k in ("label", "concept", "group")}}
            for code, meta in (data.get("variables") or {}).items()
        ]
        return SourceResult(source="us_census_variables", ok=True, rows=rows, meta={"dataset": dataset})

    # ── PxWeb (Nordics, Ireland, and friends) ─────────────────────────────────

    async def pxweb(
        self,
        base_url: str,
        table_path: str,
        query: list[dict] | None = None,
        *,
        response_format: str = "json-stat2",
    ) -> SourceResult:
        """POST a PxWeb query and flatten the JSON-stat response.

        An empty query means "everything", which on a large table is a very large
        download and often a 403 for exceeding the cell limit. Always filter.
        """
        url = f"{base_url.rstrip('/')}/{table_path.lstrip('/')}"
        payload = {"query": query or [], "response": {"format": response_format}}
        try:
            data = await self._json("POST", url, json=payload)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source=f"pxweb:{url}", ok=False, error=str(exc))
        return SourceResult(
            source=f"pxweb:{url}",
            ok=True,
            rows=list(_flatten_jsonstat(data)),
            meta={"label": data.get("label"), "updated": data.get("updated"), "url": url},
        )

    async def pxweb2(
        self,
        base_url: str,
        table_id: str,
        *,
        value_codes: dict[str, str | list[str]] | None = None,
        lang: str = "en",
        output_format: str = "json-stat2",
    ) -> SourceResult:
        """PxWebApi 2.0 — stable table IDs and plain GET.

        The 2.0 spec is a different API, not a version bump: tables are addressed
        by a stable ID that survives database-tree restructuring, ``lang`` moves
        from a path segment to a query parameter, and data can be fetched with
        GET. Sweden moved to it in Oct 2025 and Norway in Jan 2026; Finland and
        Switzerland are still on 1.x and need :meth:`pxweb` instead.
        """
        params: dict[str, str] = {"lang": lang, "outputFormat": output_format}
        for code, values in (value_codes or {}).items():
            params[f"valueCodes[{code}]"] = (
                ",".join(values) if isinstance(values, list) else values
            )
        url = f"{base_url.rstrip('/')}/tables/{table_id}/data"
        try:
            data = await self._json("GET", url, params=params)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source=f"pxweb2:{table_id}", ok=False, error=str(exc))
        return SourceResult(
            source=f"pxweb2:{table_id}",
            ok=True,
            rows=list(_flatten_jsonstat(data)),
            meta={"label": data.get("label"), "updated": data.get("updated"), "url": url},
        )

    async def pxweb_config(self, base_url: str) -> SourceResult:
        """Read an instance's live limits.

        Every PxWeb deployment publishes its own maxValues, maxCells, maxCalls
        and timeWindow at the API root, and these differ per country while the
        published prose is frequently stale — Finland's docs understate its cell
        limit by 20%. Read the config rather than hardcoding.
        """
        for path in ("/config", "?config"):
            url = f"{base_url.rstrip('/')}{path}" if path.startswith("/") else f"{base_url}{path}"
            try:
                data = await self._json("GET", url)
            except Exception:  # noqa: BLE001
                continue
            return SourceResult(source=f"pxweb_config:{base_url}", ok=True, rows=[data], meta=data)
        return SourceResult(
            source=f"pxweb_config:{base_url}", ok=False, error="no config endpoint responded"
        )

    async def pxweb_metadata(self, base_url: str, table_path: str) -> SourceResult:
        """GET the table's variables and value codes — the thing you must read
        before writing a query, since codes are neither stable nor guessable."""
        url = f"{base_url.rstrip('/')}/{table_path.lstrip('/')}"
        try:
            data = await self._json("GET", url)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source=f"pxweb_meta:{url}", ok=False, error=str(exc))
        variables = data.get("variables") or []
        rows = [
            {
                "code": v.get("code"),
                "text": v.get("text"),
                "values": (v.get("values") or [])[:60],
                "valueTexts": (v.get("valueTexts") or [])[:60],
                "elimination": v.get("elimination"),
                "time": v.get("time"),
            }
            for v in variables
        ]
        return SourceResult(source=f"pxweb_meta:{url}", ok=True, rows=rows, meta={"title": data.get("title")})

    # ── SDMX (Eurostat, OECD, IMF, ABS, ILO) ──────────────────────────────────

    async def sdmx_json(
        self,
        base_url: str,
        dataflow: str,
        key: str = "",
        *,
        params: dict[str, str] | None = None,
        agency: str | None = None,
    ) -> SourceResult:
        """Fetch SDMX-JSON and flatten it into rows.

        The ``key`` is positional and dot-separated, one slot per dimension in the
        dataflow's declared order, where an empty slot means "all". Get the order
        wrong and you silently query a different series rather than getting an
        error. Read the dataflow's structure first; do not guess.
        """
        path = f"{base_url.rstrip('/')}/{dataflow}"
        if key:
            path = f"{path}/{key}"
        query = {"format": "jsondata", **(params or {})}
        if agency:
            query["agencyId"] = agency
        try:
            data = await self._json("GET", path, params=query)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source=f"sdmx:{dataflow}", ok=False, error=str(exc))
        return SourceResult(
            source=f"sdmx:{dataflow}",
            ok=True,
            rows=list(_flatten_sdmx(data)),
            meta={"dataflow": dataflow, "key": key, "url": path},
        )

    async def eurostat(self, dataset: str, **filters: str) -> SourceResult:
        """Eurostat's dissemination API, which is friendlier than raw SDMX:
        filters are ordinary named query parameters."""
        url = (
            "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
            f"{dataset}"
        )
        params = {"format": "JSON", "lang": "EN", **filters}
        try:
            data = await self._json("GET", url, params=params)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source=f"eurostat:{dataset}", ok=False, error=str(exc))
        return SourceResult(
            source=f"eurostat:{dataset}",
            ok=True,
            rows=list(_flatten_jsonstat(data)),
            meta={"dataset": dataset, "label": data.get("label"), "updated": data.get("updated")},
        )

    # ── Multilateral ──────────────────────────────────────────────────────────

    async def world_bank(
        self,
        indicator: str,
        countries: str = "all",
        *,
        date: str | None = None,
        per_page: int = 1000,
    ) -> SourceResult:
        """World Bank Indicators API. No key, ~1,500 indicators, 189 countries.

        Two traps, both verified against the live API on 2026-08-01:

        * The response is ``[metadata, rows]``. A bare list of rows is never what
          comes back, and treating it as one is the standard first mistake.
        * **Throttling is signalled as a 502 HTML page or a read timeout, never
          as a 429.** Under a burst, perfectly valid URLs start failing and the
          failure looks like a malformed request or an outage — I chased both the
          ``date=2022:2023`` colon and the ``USA;BRA`` separator as suspects
          before establishing that each works fine once requests are spaced. Both
          forms are correct; the pacing is what matters. Hence the deliberate
          inter-request delay below rather than a bigger retry budget, which would
          only deepen the hole.
        """
        params = {"format": "json", "per_page": str(per_page)}
        if date:
            params["date"] = date
        url = f"https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"
        try:
            data = await self._json("GET", url, params=params, pace_key="worldbank")
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source=f"worldbank:{indicator}", ok=False, error=str(exc))
        if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
            message = data[0] if isinstance(data, list) and data else data
            return SourceResult(
                source=f"worldbank:{indicator}", ok=False, error=f"no data: {str(message)[:200]}"
            )
        return SourceResult(
            source=f"worldbank:{indicator}",
            ok=True,
            rows=_worldbank_rows(data[1]),
            meta={"indicator": indicator, "total": data[0].get("total")},
        )

    # ── Company registries ────────────────────────────────────────────────────

    async def companies_house(self, query: str, *, items: int = 20) -> SourceResult:
        """UK Companies House search. HTTP Basic with the key as the username
        and an empty password — not a Bearer token."""
        key = api_key_for("COMPANIES_HOUSE_API_KEY")
        if not key:
            return SourceResult(
                source="companies_house", ok=False, error="COMPANIES_HOUSE_API_KEY not set"
            )
        try:
            resp = await self._client.get(
                "https://api.company-information.service.gov.uk/search/companies",
                params={"q": query, "items_per_page": str(items)},
                auth=(key, ""),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source="companies_house", ok=False, error=str(exc))
        rows = [
            {
                "company_number": i.get("company_number"),
                "title": i.get("title"),
                "status": i.get("company_status"),
                "type": i.get("company_type"),
                "incorporated": i.get("date_of_creation"),
                "address": (i.get("address_snippet") or ""),
                "url": f"https://find-and-update.company-information.service.gov.uk{i.get('links', {}).get('self', '')}",
            }
            for i in (data.get("items") or [])
        ]
        return SourceResult(
            source="companies_house", ok=True, rows=rows, meta={"total": data.get("total_results")}
        )

    async def sec_edgar_search(self, query: str, *, forms: str = "", size: int = 20) -> SourceResult:
        """SEC EDGAR full-text search. No key, but the SEC blocks requests
        without a descriptive User-Agent carrying a contact address — set
        SEC_USER_AGENT. Fair-access limit is 10 requests/second."""
        params = {"q": query, "from": "0", "size": str(size)}
        if forms:
            params["forms"] = forms
        try:
            data = await self._json(
                "GET", "https://efts.sec.gov/LATEST/search-index", params=params
            )
        except Exception:  # noqa: BLE001
            try:
                data = await self._json(
                    "GET", "https://www.sec.gov/cgi-bin/srqsb", params=params
                )
            except Exception as exc:  # noqa: BLE001
                return SourceResult(source="sec_edgar", ok=False, error=str(exc))
        hits = ((data.get("hits") or {}).get("hits")) or []
        rows = [
            {
                "accession": h.get("_id"),
                "form": (h.get("_source") or {}).get("file_type"),
                "filed": (h.get("_source") or {}).get("file_date"),
                "entity": ((h.get("_source") or {}).get("display_names") or [None])[0],
                "cik": ((h.get("_source") or {}).get("ciks") or [None])[0],
            }
            for h in hits
        ]
        return SourceResult(source="sec_edgar", ok=True, rows=rows, meta={"query": query})

    async def gleif_lei(self, query: str, *, size: int = 20) -> SourceResult:
        """GLEIF LEI records. No key, and the best free cross-border way to
        resolve a legal entity to a canonical identifier and parent."""
        try:
            data = await self._json(
                "GET",
                "https://api.gleif.org/api/v1/lei-records",
                params={"filter[entity.legalName]": query, "page[size]": str(size)},
                headers={"Accept": "application/vnd.api+json"},
            )
        except Exception as exc:  # noqa: BLE001
            return SourceResult(source="gleif", ok=False, error=str(exc))
        rows = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            entity = attrs.get("entity") or {}
            legal_address = entity.get("legalAddress") or {}
            rows.append(
                {
                    "lei": attrs.get("lei"),
                    "name": (entity.get("legalName") or {}).get("name"),
                    "status": entity.get("status"),
                    "country": legal_address.get("country"),
                    "city": legal_address.get("city"),
                    "legal_form": ((entity.get("legalForm") or {}).get("id")),
                    "registered_as": entity.get("registeredAs"),
                }
            )
        return SourceResult(source="gleif", ok=True, rows=rows, meta={"query": query})


# ── response flatteners ───────────────────────────────────────────────────────


def _worldbank_rows(raw: list) -> list[dict]:
    return [
        {
            "country": (r.get("country") or {}).get("value"),
            "iso3": r.get("countryiso3code"),
            "indicator": (r.get("indicator") or {}).get("value"),
            "year": r.get("date"),
            "value": r.get("value"),
            "unit": r.get("unit"),
        }
        for r in raw
        if isinstance(r, dict)
    ]


def _census_number(value: Any) -> Any:
    """Census encodes suppressed/unavailable cells as extreme negatives.

    Left as-is they poison every aggregate downstream, and they look like
    legitimate values to an LLM reading the JSON.
    """
    if not isinstance(value, str):
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    if number <= -666666666:
        return None
    return int(number) if number.is_integer() else number


def _flatten_jsonstat(data: dict) -> Iterable[dict]:
    """JSON-stat 2.0 → rows.

    The format stores values in a flat array indexed by the cartesian product of
    dimension sizes in declared order. Decoding that index back into labels is
    the whole job, and it is why raw JSON-stat is unreadable to an LLM.
    """
    if not isinstance(data, dict):
        return []
    dim_ids: list[str] = (data.get("id") or list((data.get("dimension") or {}).get("id") or []))
    sizes: list[int] = data.get("size") or []
    dimension = data.get("dimension") or {}
    values = data.get("value")

    if not dim_ids or not sizes or values is None:
        return []

    labels: list[list[tuple[str, str]]] = []
    for dim_id in dim_ids:
        meta = dimension.get(dim_id) or {}
        category = meta.get("category") or {}
        index = category.get("index") or {}
        label_map = category.get("label") or {}
        if isinstance(index, dict):
            ordered = sorted(index.items(), key=lambda kv: kv[1])
            codes = [code for code, _ in ordered]
        else:
            codes = list(index)
        labels.append([(code, label_map.get(code, code)) for code in codes])

    if isinstance(values, dict):
        value_at = lambda i: values.get(str(i))
        total = 1
        for s in sizes:
            total *= s
    else:
        value_at = lambda i: values[i] if i < len(values) else None
        total = len(values)

    strides: list[int] = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    rows: list[dict] = []
    for flat in range(total):
        value = value_at(flat)
        if value is None:
            continue
        row: dict[str, Any] = {}
        for pos, dim_id in enumerate(dim_ids):
            idx = (flat // strides[pos]) % sizes[pos]
            if idx < len(labels[pos]):
                code, label = labels[pos][idx]
                row[dim_id] = label
                row[f"{dim_id}_code"] = code
        row["value"] = value
        rows.append(row)
    return rows


def _flatten_sdmx(data: dict) -> Iterable[dict]:
    """SDMX-JSON → rows. Observations are keyed by colon-joined dimension
    positions, which must be decoded against the structure block."""
    if not isinstance(data, dict):
        return []
    structure = data.get("structure") or (data.get("data") or {}).get("structures", [{}])[0] or {}
    dims = ((structure.get("dimensions") or {}).get("observation") or []) + (
        (structure.get("dimensions") or {}).get("series") or []
    )
    datasets = data.get("dataSets") or (data.get("data") or {}).get("dataSets") or []
    if not datasets:
        return []

    rows: list[dict] = []
    series = datasets[0].get("series") or {}
    observations = datasets[0].get("observations") or {}

    def decode(key: str, pool: list[dict]) -> dict:
        out: dict[str, Any] = {}
        for pos, raw in enumerate(key.split(":")):
            if pos >= len(pool):
                break
            dim = pool[pos]
            try:
                value = (dim.get("values") or [])[int(raw)]
            except (ValueError, IndexError):
                continue
            out[dim.get("id") or f"dim{pos}"] = value.get("name") or value.get("id")
        return out

    if series:
        obs_dims = (structure.get("dimensions") or {}).get("observation") or []
        series_dims = (structure.get("dimensions") or {}).get("series") or []
        for skey, sval in series.items():
            base = decode(skey, series_dims)
            for okey, oval in (sval.get("observations") or {}).items():
                row = dict(base)
                row.update(decode(okey, obs_dims))
                row["value"] = oval[0] if isinstance(oval, list) and oval else oval
                rows.append(row)
    else:
        for okey, oval in observations.items():
            row = decode(okey, dims)
            row["value"] = oval[0] if isinstance(oval, list) and oval else oval
            rows.append(row)
    return rows
