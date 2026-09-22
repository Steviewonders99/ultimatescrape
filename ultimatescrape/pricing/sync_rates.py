"""Writer 2: projects_db rate tables -> our_buy_rate mirror.

Keyset-paginated single-table pulls (proxy timeout ~=27s). rate_usd_hour is
computed for ANY decoded-hourly rate with a KNOWN currency:

  - currency == 'USD'      -> rate_usd_hour = rate as-is, rate_fx_as_of NULL
  - currency != 'USD'      -> rate_usd_hour = fx.convert(rate, currency),
                               rate_fx_as_of = the FxTable's as_of date
  - currency unknown/blank -> rate_usd_hour = NULL, rate_fx_as_of = NULL
                               (abstain, never guess)

Controller ruling 2026-09-22 (see task-8-report.md "Round 2 review fixes"):
Task 8's original cut set rate_usd_hour only for currency=='USD', which was
the right instinct (discovery found the four rate source tables are NOT
plain-USD numerics -- project_lang_pair is only 44.6% USD within its own
"Per Hour" rows) but threw away ~55% of hourly coverage rather than
converting it. This version reuses the repo's existing, already-vetted FX
pipeline (fx.py / normalize.py, the same one sync_boards.py uses for
competitor_listing) instead of treating non-USD as unrecoverable. `rate`
itself is still mirrored in its source currency for every row (now WITH a
`currency` column recording which, added in this same round -- see ddl.py),
so any consumer can independently verify or re-derive rate_usd_hour.

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

from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg

from ultimatescrape.pricing import proxydb, runs
from ultimatescrape.pricing.fx import FxTable, load_fx
from ultimatescrape.pricing.rate_units import RATE_SOURCES, decode

PAGE = 5000

#: Same daily-cached FX table sync_boards.py uses for competitor_listing --
#: one shared cache file, refreshed at most once/day regardless of which
#: writer runs first.
FX_CACHE = Path.home() / "UltimateScrape" / "knowledge" / "fx_usd.json"

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


def _rate_usd_hour(
    unit: str | None, currency: str | None, rate, fx: FxTable
) -> tuple[float | None, date | None]:
    """USD -> rate as-is, no stamp. Non-USD -> fx-converted, stamped with
    fx.as_of. Not hourly, or currency unknown/missing, or fx has no rate for
    it -> (None, None). Never guesses a conversion."""
    if unit != "hour" or rate is None or not currency:
        return None, None
    if currency == "USD":
        return float(rate), None
    converted = fx.convert(float(rate), currency)
    if converted is None:
        return None, None
    return round(converted, 2), fx.as_of


async def sync_rates(pool: asyncpg.Pool) -> dict:
    run_id = await runs.start(pool, name="our_buy_rate_mirror",
                              source="uscrape pricing")
    started = datetime.now(timezone.utc)
    fx = await load_fx(FX_CACHE)
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
                    raw_unit_code = r.get("rate_unit")
                    unit = decode(raw_unit_code)
                    # never store the literal string "None" — a missing
                    # code stays SQL NULL, not str(None).
                    rate_unit_code = (
                        str(raw_unit_code) if raw_unit_code is not None else None
                    )
                    rate = r.get("rate")
                    currency = (r.get("currency") or "").strip().upper() or None
                    locale = r.get(src.locale_col) if src.locale_col else None
                    usd_hour, fx_as_of = _rate_usd_hour(unit, currency, rate, fx)
                    project_id = (
                        (str(r.get(src.project_col) or "") or None)
                        if src.project_col else None
                    )
                    await pool.execute(
                        """
                        INSERT INTO our_buy_rate
                          (source_table, source_pk, side, project_id, locale,
                           country_code, currency, rate, rate_unit_code,
                           rate_unit_decoded, rate_usd_hour, rate_fx_as_of,
                           synced_at, is_current)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,TRUE)
                        ON CONFLICT (source_table, source_pk) DO UPDATE SET
                          side=EXCLUDED.side, project_id=EXCLUDED.project_id,
                          locale=EXCLUDED.locale, country_code=EXCLUDED.country_code,
                          currency=EXCLUDED.currency,
                          rate=EXCLUDED.rate, rate_unit_code=EXCLUDED.rate_unit_code,
                          rate_unit_decoded=EXCLUDED.rate_unit_decoded,
                          rate_usd_hour=EXCLUDED.rate_usd_hour,
                          rate_fx_as_of=EXCLUDED.rate_fx_as_of,
                          synced_at=EXCLUDED.synced_at, is_current=TRUE
                        """,
                        src.table, str(r[src.pk_col]), src.side,
                        project_id,
                        locale,
                        _country_code(locale),
                        currency,
                        rate, rate_unit_code, unit,
                        usd_hour, fx_as_of,
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
