"""Writer 3: DealForce catalogue JSON -> dealforce_opportunity mirror.

297 opportunity-level records. NO gm field exists in this feed — anything
downstream presenting margin from it is wrong (spec §3). Records absent
from the feed are marked missing_since, never deleted.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import httpx

from ultimatescrape.pricing import runs

CATALOGUE_URL = "https://centificatalogue.z22.web.core.windows.net/all_data.json"


async def _fetch_catalogue() -> list[dict]:
    async with httpx.AsyncClient(timeout=60) as http:
        resp = await http.get(CATALOGUE_URL, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError("catalogue payload is not a non-empty JSON array")
    return data


async def sync_dealforce(pool: asyncpg.Pool, *, fetch=None) -> dict:
    run_id = await runs.start(pool, name="dealforce_catalogue",
                              source="uscrape pricing")
    now = datetime.now(timezone.utc)
    try:
        records = await (fetch or _fetch_catalogue)()
        upserted = 0
        for r in records:
            await pool.execute(
                """
                INSERT INTO dealforce_opportunity
                  (client, project, quarter, value_usd, weighted_usd, status,
                   service_line, tags, asset_type, loc, synced_at, missing_since)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NULL)
                ON CONFLICT (client, project, quarter) DO UPDATE SET
                  value_usd=EXCLUDED.value_usd, weighted_usd=EXCLUDED.weighted_usd,
                  status=EXCLUDED.status, service_line=EXCLUDED.service_line,
                  tags=EXCLUDED.tags, asset_type=EXCLUDED.asset_type,
                  loc=EXCLUDED.loc, synced_at=EXCLUDED.synced_at,
                  missing_since=NULL
                """,
                str(r.get("c") or ""), str(r.get("p") or ""),
                str(r.get("q") or ""), r.get("v"), r.get("w"),
                str(r.get("s") or ""), str(r.get("sl") or ""),
                [str(t) for t in (r.get("tags") or [])],
                str(r.get("assetType") or ""), str(r.get("loc") or ""), now,
            )
            upserted += 1
        result = await pool.execute(
            "UPDATE dealforce_opportunity SET missing_since=$1 "
            "WHERE synced_at < $1 AND missing_since IS NULL", now)
        missing = int(result.split()[-1])
        await runs.finish(pool, run_id, "ok", rows_updated=upserted,
                          metadata={"upserted": upserted, "missing_marked": missing})
        return {"upserted": upserted, "missing_marked": missing}
    except Exception as exc:
        await runs.finish(pool, run_id, "failed", error=str(exc)[:500])
        raise
