"""Async client for the onetake platform-DB proxy (read-only shapes).

Request shape ported from the known-working ~/Oneformadata/scripts/
dc_cohort_attributes.py `q()` (lines 1-35, read 2026-09-22): POST
{PROXY}/db/{db}/query with body {"sql": sql}, Bearer auth, a curl
User-Agent (the proxy rejects/mishandles some other UAs), and the
response body is `{"rows": [...]}` — NOT a bare list.

Retry/backoff mirrors q() exactly: 8 tries, sleeping `3 + 5*attempt`
seconds between attempts (0s is never slept; only between tries), then
raising the last exception. q() uses `time.sleep`; this is the async
equivalent, `await asyncio.sleep`.

Statement timeout ≈27s server-side: callers keep to single-table,
keyset-paginated slices.
"""

from __future__ import annotations

import asyncio

import httpx

from ultimatescrape.pricing import config


async def query(db: str, sql: str, *, timeout: float = 60.0, tries: int = 8) -> list[dict]:
    last_exc: Exception | None = None
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                resp = await http.post(
                    f"{config.proxy_base()}/db/{db}/query",
                    json={"sql": sql},
                    headers={
                        "Authorization": f"Bearer {config.proxy_secret()}",
                        "User-Agent": "curl/8.7.1",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else data.get("rows", [])
        except Exception as exc:  # noqa: BLE001 - retry then surface loudly
            last_exc = exc
            if attempt == tries - 1:
                raise
            await asyncio.sleep(3 + 5 * attempt)
    raise last_exc  # pragma: no cover - unreachable, satisfies type checkers
