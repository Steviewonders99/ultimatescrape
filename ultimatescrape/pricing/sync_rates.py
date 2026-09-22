"""Writer 2: projects_db rate tables -> our_buy_rate mirror.

Keyset-paginated single-table pulls (proxy timeout ~=27s). rate_usd_hour is
set ONLY when the unit decodes to 'hour' AND the source row's currency is
literally 'USD'.

That second condition is a deliberate departure from a naive "unit=='hour'
implies rate_usd_hour=rate": discovery (2026-09-22, see rate_units.py and
task-8-report.md) found the four rate source tables are NOT plain-USD
numerics. Within rate_unit='1' ("Per Hour") specifically -- the only slice
this writer would otherwise label as a USD hourly rate -- project_lang_pair
is only 44.6% USD (2,322/6,036); EUR is the plurality at 43.5%, CNY 16%.
Treating those raw numbers as dollars would silently overstate/understate
by the EUR/CNY/USD spread, in direct violation of the standing "Currency =
USD only" rule and the "loud failure, never silent wrong numbers" rule.

This module does NOT invent an FX conversion (the repo has one -- fx.py /
normalize.py, used by sync_boards.py for competitor_listing -- reusing it
here would need a currency column on our_buy_rate, which is out of this
writer's scope). It abstains instead: non-USD hourly rows still mirror
(rate, rate_unit_code, rate_unit_decoded all populated) but rate_usd_hour
stays NULL, same as an undecodable unit. `rate` itself, for every row, is
mirrored in ITS SOURCE CURRENCY -- our_buy_rate has no currency column to
record which -- so `rate` alone is not safely comparable across rows or to
the (already-USD) competitor benchmark. Only `rate_usd_hour` is a trustworthy
USD figure. See task-8-report.md Discovery Evidence for the full currency
breakdown per table.

Three of the four source tables carry an `is_deleted` flag
(project_lang_pair, project_resource_info, configuration_client_rate);
project_resource_crowd_service_rate does not. Soft-deleted/superseded rate
rows are excluded at the source query so a stale rate never lands in the
mirror flagged is_current=TRUE.

Keyset pagination orders/compares pk_col NUMERICALLY, not ::text. All four
tables' `id` is bigint (confirmed via information_schema, 2026-09-22), and
project_resource_info's ids mix 16- and 18-digit values -- a text-cast
ORDER BY would misorder those (e.g. '100...' < '1815...' as text despite
being numerically far larger) and silently skip or loop rows mid-page.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg

from ultimatescrape.pricing import proxydb, runs
from ultimatescrape.pricing.rate_units import RATE_SOURCES, decode

PAGE = 5000

#: Source tables that carry a soft-delete flag -- excluded at the source
#: query (WHERE is_deleted = 0) so deleted/superseded rows never mirror as
#: current. project_resource_crowd_service_rate has no such column.
TABLES_WITH_IS_DELETED = {
    "project_lang_pair",
    "project_resource_info",
    "configuration_client_rate",
}


def _country_code(locale: str | None) -> str | None:
    """xx_YY -> YY, only for a well-formed 5-char locale. Anything else
    (None, empty, malformed) stays unresolved rather than guessed."""
    if locale and len(locale) == 5 and locale[2] == "_":
        return locale[-2:].upper()
    return None


async def sync_rates(pool: asyncpg.Pool) -> dict:
    run_id = await runs.start(pool, name="our_buy_rate_mirror",
                              source="uscrape pricing")
    started = datetime.now(timezone.utc)
    summary: dict = {"tables": {}}
    upserted = 0
    try:
        for src in RATE_SOURCES:
            cols = [src.pk_col, "rate", "rate_unit", "currency"]
            if src.project_col:
                cols.append(src.project_col)
            if src.locale_col:
                cols.append(src.locale_col)
            deleted_clause = (
                "is_deleted = 0" if src.table in TABLES_WITH_IS_DELETED else ""
            )
            last, n = "", 0
            while True:
                clauses = []
                if last:
                    # numeric compare, NOT ::text — every pk_col is bigint
                    # (verified via information_schema, 2026-09-22) and
                    # project_resource_info mixes 16- and 18-digit ids;
                    # text-ordering those would misorder the keyset and
                    # silently skip or loop rows.
                    clauses.append(f"{src.pk_col} > {last}")
                if deleted_clause:
                    clauses.append(deleted_clause)
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = await proxydb.query(
                    "projects_db",
                    f"SELECT {', '.join(cols)} FROM {src.table} {where} "
                    f"ORDER BY {src.pk_col} LIMIT {PAGE}")
                if not rows:
                    break
                for r in rows:
                    unit = decode(r.get("rate_unit"))
                    rate = r.get("rate")
                    currency = (r.get("currency") or "").strip().upper()
                    locale = r.get(src.locale_col) if src.locale_col else None
                    is_hourly_usd = unit == "hour" and currency == "USD" and rate is not None
                    project_id = (
                        (str(r.get(src.project_col) or "") or None)
                        if src.project_col else None
                    )
                    await pool.execute(
                        """
                        INSERT INTO our_buy_rate
                          (source_table, source_pk, side, project_id, locale,
                           country_code, rate, rate_unit_code, rate_unit_decoded,
                           rate_usd_hour, synced_at, is_current)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE)
                        ON CONFLICT (source_table, source_pk) DO UPDATE SET
                          side=EXCLUDED.side, project_id=EXCLUDED.project_id,
                          locale=EXCLUDED.locale, country_code=EXCLUDED.country_code,
                          rate=EXCLUDED.rate, rate_unit_code=EXCLUDED.rate_unit_code,
                          rate_unit_decoded=EXCLUDED.rate_unit_decoded,
                          rate_usd_hour=EXCLUDED.rate_usd_hour,
                          synced_at=EXCLUDED.synced_at, is_current=TRUE
                        """,
                        src.table, str(r[src.pk_col]), src.side,
                        project_id,
                        locale,
                        _country_code(locale),
                        rate, str(r.get("rate_unit")), unit,
                        (float(rate) if is_hourly_usd else None),
                        started,
                    )
                n += len(rows)
                last = str(rows[-1][src.pk_col])
            # rows this sync did not touch are no longer current
            await pool.execute(
                "UPDATE our_buy_rate SET is_current=FALSE "
                "WHERE source_table=$1 AND synced_at < $2", src.table, started)
            summary["tables"][src.table] = n
            upserted += n
        await runs.finish(pool, run_id, "ok", rows_updated=upserted,
                          metadata=summary)
        summary["upserted"] = upserted
        return summary
    except Exception as exc:
        await runs.finish(pool, run_id, "failed", error=str(exc)[:500],
                          metadata=summary)
        raise
