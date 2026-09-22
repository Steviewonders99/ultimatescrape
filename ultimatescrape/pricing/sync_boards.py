"""Writer 1: competitor boards -> competitor_listing (+history).

One write path for this source (spec §0 rule 1). Public function wraps
sync_runs and re-raises on failure. dry_run writes NOTHING, not even a
sync_runs row (canary never writes state).

Column<->value pairing is the riskiest part of this module (see task brief).
Rather than build INSERT/UPDATE argument lists by positional slicing, a
single named dict (`_listing_values`) maps column name -> value once; the
INSERT and UPDATE SQL text is generated from explicit column-name lists
(`INSERT_COLUMNS` / `UPDATE_COLUMNS`) so the placeholder count and the
argument list are always derived from the same list and cannot drift apart.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from ultimatescrape.jobboards import registry as jb_registry
from ultimatescrape.jobboards.fetchers import JobBoardClient, Listing
from ultimatescrape.pricing import diff, normalize, runs
from ultimatescrape.pricing.fx import load_fx

log = logging.getLogger("uscrape.pricing")

FX_CACHE = Path.home() / "UltimateScrape" / "knowledge" / "fx_usd.json"

# Column order mirrors the task brief's INSERT_SQL exactly (minus
# first_seen_at, which is derived from last_seen_at at insert time — see
# _insert_args).
INSERT_COLUMNS: list[str] = [
    "platform", "company", "external_id", "url", "title", "department",
    "employment_type", "remote", "worker_gig", "location_raw",
    "country_code", "description_full", "description_excerpt",
    "pay_min", "pay_max", "pay_currency", "pay_unit", "pay_raw",
    "pay_source", "pay_usd_hour_min", "pay_usd_hour_max",
    "pay_fx_as_of", "posted_at",
]

# Column order mirrors the task brief's UPDATE_SQL exactly. platform,
# company, external_id and first_seen_at are identity/provenance columns and
# are never rewritten by an update.
UPDATE_COLUMNS: list[str] = [
    "url", "title", "department", "employment_type", "remote", "worker_gig",
    "location_raw", "country_code", "description_full", "description_excerpt",
    "pay_min", "pay_max", "pay_currency", "pay_unit", "pay_raw", "pay_source",
    "pay_usd_hour_min", "pay_usd_hour_max", "pay_fx_as_of", "posted_at",
    "last_seen_at", "content_hash",
]


def _insert_sql() -> str:
    all_cols = INSERT_COLUMNS + ["first_seen_at", "last_seen_at", "content_hash"]
    n = len(INSERT_COLUMNS)
    placeholders = [f"${i}" for i in range(1, n + 1)]
    # first_seen_at = last_seen_at at insert time: reuse the same placeholder.
    placeholders += [f"${n + 1}", f"${n + 1}", f"${n + 2}"]
    return (
        f"INSERT INTO competitor_listing ({', '.join(all_cols)}) "
        f"VALUES ({', '.join(placeholders)}) RETURNING id"
    )


def _update_sql() -> str:
    set_clause = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(UPDATE_COLUMNS))
    return f"UPDATE competitor_listing SET {set_clause}, delisted_at=NULL WHERE id=$1"


INSERT_SQL = _insert_sql()
UPDATE_SQL = _update_sql()


def _posted(l: Listing):
    try:
        return datetime.strptime(l.posted_at[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


#: Listing attributes that must land in a TEXT column. Checked before any
#: further processing (e.g. normalize.country_from_location) touches them —
#: an adapter that hands back a dict instead of a string (observed live on
#: outlier: location={"name": "..."}) must fail loud and specific, not as a
#: mid-transaction AttributeError three calls downstream.
_TEXT_FIELDS = (
    "platform", "company", "external_id", "url", "title", "department",
    "employment_type", "remote", "location", "description_full",
    "description_excerpt", "pay_currency", "pay_unit", "pay_raw",
    "pay_source",
)


def _assert_text_fields(l: Listing) -> None:
    for field in _TEXT_FIELDS:
        value = getattr(l, field)
        if value is not None and not isinstance(value, str):
            raise TypeError(
                f"competitor_boards: platform={l.platform!r} "
                f"external_id={l.external_id!r} field {field!r} is "
                f"{type(value).__name__}, not str/None: {value!r} — "
                f"the {l.platform!r} adapter must emit a string; fix it at "
                "the source, do not coerce here"
            )


def _listing_values(l: Listing, norm: dict, now: datetime) -> dict:
    """Column name -> value for one listing. Single source of truth for the
    column<->value pairing; INSERT and UPDATE both read from this dict by
    name, never by position."""
    _assert_text_fields(l)
    pay_source = "quarantined" if norm["quarantined"] else l.pay_source
    return {
        "platform": l.platform,
        "company": l.company,
        "external_id": l.external_id,
        "url": l.url,
        "title": l.title,
        "department": l.department,
        "employment_type": l.employment_type,
        "remote": l.remote,
        "worker_gig": l.worker_gig,
        "location_raw": l.location,
        "country_code": normalize.country_from_location(l.location),
        "description_full": l.description_full,
        "description_excerpt": l.description_excerpt,
        "pay_min": l.pay_min,
        "pay_max": l.pay_max,
        "pay_currency": l.pay_currency,
        "pay_unit": l.pay_unit,
        "pay_raw": l.pay_raw,
        "pay_source": pay_source,
        "pay_usd_hour_min": None if norm["quarantined"] else norm["pay_usd_hour_min"],
        "pay_usd_hour_max": None if norm["quarantined"] else norm["pay_usd_hour_max"],
        "pay_fx_as_of": norm["pay_fx_as_of"],
        "posted_at": _posted(l),
        "last_seen_at": now,
        "content_hash": diff.listing_content_hash(l),
    }


def _insert_args(values: dict) -> list:
    # $24 (first_seen_at = last_seen_at) appears twice in INSERT_SQL's text
    # but is ONE positional parameter to asyncpg/postgres — pass its value
    # once, not once per appearance.
    row = [values[c] for c in INSERT_COLUMNS]
    row.append(values["last_seen_at"])
    row.append(values["content_hash"])
    return row


def _update_args(listing_id: int, values: dict) -> list:
    return [listing_id] + [values[c] for c in UPDATE_COLUMNS]


async def sync_boards(
    pool: asyncpg.Pool,
    *,
    platforms: list[str] | None = None,
    dry_run: bool = False,
    fetch=None,
) -> dict:
    keys = platforms or [p.key for p in jb_registry.PLATFORMS]
    now = datetime.now(timezone.utc)

    if fetch is None:
        async def fetch(ks):
            async with JobBoardClient() as client:
                return await client.fetch_all(ks)

    ok, errors = await fetch(keys)

    # zero-listing anomaly guard: 0 rows from a platform holding live rows
    # is a swallowed failure, not a mass delisting.
    #
    # dry_run only: competitor_listing may not exist yet (init-db has not
    # run). A dry-run reads only to compute a diff preview, so an
    # UndefinedTableError here is honestly "zero existing rows", not a bug —
    # treat it as such rather than creating the table to make it go away. A
    # real (non-dry-run) sync always runs ddl.apply() first (see cli.py), so
    # this branch never masks a genuine missing-table failure on write.
    try:
        live = {
            r["platform"]: r["n"]
            for r in await pool.fetch(
                "SELECT platform, count(*) AS n FROM competitor_listing "
                "WHERE delisted_at IS NULL GROUP BY platform")
        }
    except asyncpg.UndefinedTableError:
        if not dry_run:
            raise
        log.info("[pricing] tables not yet created — dry-run diff computed "
                  "against empty state")
        live = {}
    for key in list(ok):
        if not ok[key] and live.get(key, 0) > 0:
            errors[key] = "zero-listing anomaly: fetch returned 0 while DB has live rows"
            del ok[key]

    fx = await load_fx(FX_CACHE)
    fetched: list[Listing] = [l for batch in ok.values() for l in batch]

    # Writer-level guard: an adapter bug (wrong/absent id field) or a feed
    # quirk can hand back an empty or repeated external_id — observed live
    # on mercor/imerit (empty id: both adapters read a field the feed
    # doesn't have). The UNIQUE constraint on (platform, external_id) is
    # the backstop, but a constraint violation aborts the WHOLE transaction
    # (spec: loud failure, never silent data loss) — one bad listing must
    # not block every OTHER listing on the same platform from syncing.
    # Drop the offenders here instead, loudly counted in the summary/
    # sync_runs metadata rather than silently. "First wins" is deterministic
    # given one fetch: platform order follows `keys`, and within a platform
    # the adapter's own list order.
    missing_external_id: dict[str, int] = {}
    duplicate_keys_dropped = 0
    seen_keys: set[tuple[str, str]] = set()
    deduped: list[Listing] = []
    for l in fetched:
        if not l.external_id:
            missing_external_id[l.platform] = missing_external_id.get(l.platform, 0) + 1
            continue
        key = (l.platform, l.external_id)
        if key in seen_keys:
            duplicate_keys_dropped += 1
            continue
        seen_keys.add(key)
        deduped.append(l)
    fetched = deduped

    # Mass-delist side door (production incident class, twice this cycle):
    # the zero-listing anomaly guard above runs BEFORE this drop/dedup step,
    # so a platform whose fetch returns listings that ALL fail the
    # missing-external_id check still reads as "succeeded" up there (ok[key]
    # was non-empty) while contributing ZERO surviving listings here. Left
    # alone, plan_changes would then see that platform as both succeeded AND
    # missing every one of its existing rows from `fetched` -> delists all
    # of them. A platform that dropped even one listing to a missing id this
    # run cannot be trusted to say "yes, that other listing is really gone"
    # — withhold ONLY its delisting authority; inserts/updates of whatever
    # DID survive still proceed normally.
    delist_withheld = sorted(missing_external_id)
    succeeded_platforms = set(ok) - set(delist_withheld)

    norms = {
        id(l): normalize.usd_hourly(
            {"pay_min": l.pay_min, "pay_max": l.pay_max,
             "pay_currency": l.pay_currency, "pay_unit": l.pay_unit},
            worker_gig=l.worker_gig, fx=fx)
        for l in fetched
    }

    # Same missing-table tolerance as above, same dry_run-only scope.
    try:
        rows = await pool.fetch(
            "SELECT id, platform, external_id, content_hash, delisted_at,"
            " pay_min, pay_max, pay_currency, pay_unit FROM competitor_listing")
    except asyncpg.UndefinedTableError:
        if not dry_run:
            raise
        rows = []
    existing = {
        (r["platform"], r["external_id"]): diff.ExistingRow(
            id=r["id"], content_hash=r["content_hash"],
            delisted=r["delisted_at"] is not None,
            pay_key=(
                None if r["pay_min"] is None else float(r["pay_min"]),
                None if r["pay_max"] is None else float(r["pay_max"]),
                r["pay_currency"], r["pay_unit"]),
        )
        for r in rows
    }
    cs = diff.plan_changes(existing, fetched, succeeded_platforms, now)

    summary = {
        "platforms_ok": sorted(ok), "platforms_failed": dict(sorted(errors.items())),
        "inserted": len(cs.inserts), "updated": len(cs.updates),
        "touched": len(cs.touches), "delisted": len(cs.delists),
        "relisted": len(cs.relists),
        "quarantined": sum(1 for n in norms.values() if n["quarantined"]),
        "missing_external_id": dict(sorted(missing_external_id.items())),
        "duplicate_keys_dropped": duplicate_keys_dropped,
        "delist_withheld": delist_withheld,
    }
    if dry_run:
        log.info("[pricing] DRY RUN boards: %s", summary)
        return summary
    if not ok:
        # every platform failed: loud failure, nothing written
        run_id = await runs.start(pool, name="competitor_boards", source="uscrape pricing")
        await runs.finish(pool, run_id, "failed",
                          error=f"all platforms failed: {errors}")
        raise RuntimeError(f"competitor_boards: all platforms failed: {errors}")

    run_id = await runs.start(pool, name="competitor_boards",
                              source="uscrape pricing")
    try:
        async with pool.acquire() as conn, conn.transaction():
            history_rows: list[dict] = list(cs.history)
            for l in cs.inserts:
                values = _listing_values(l, norms[id(l)], now)
                new_id = await conn.fetchval(INSERT_SQL, *_insert_args(values))
                history_rows.append({
                    "listing_id": new_id, "observed_at": now,
                    "change_type": "new", "old_pay": None,
                    "new_pay": diff._pay_json(l),
                    "content_hash": values["content_hash"],
                })
            for lid, l in cs.updates + cs.relists:
                values = _listing_values(l, norms[id(l)], now)
                await conn.execute(UPDATE_SQL, *_update_args(lid, values))
            if cs.touches:
                await conn.execute(
                    "UPDATE competitor_listing SET last_seen_at=$1 WHERE id = ANY($2)",
                    now, cs.touches)
            if cs.delists:
                await conn.execute(
                    "UPDATE competitor_listing SET delisted_at=$1 WHERE id = ANY($2)",
                    now, cs.delists)
            # a changed JD invalidates the classification (spec §5.4: new OR
            # content-hash-changed listings go to the LLM)
            jd_changed = [h["listing_id"] for h in cs.history
                          if h["change_type"] in ("jd_change", "relisted")]
            if jd_changed:
                await conn.execute(
                    "DELETE FROM competitor_listing_class"
                    " WHERE taxonomy_version='v1' AND listing_id = ANY($1)",
                    jd_changed)
            for h in history_rows:
                # A Python None means "no prior/new pay to record" and must
                # land as SQL NULL, not a JSONB null scalar — json.dumps(None)
                # == "null", and "null"::jsonb IS NOT NULL in Postgres, which
                # would make `WHERE old_pay IS NULL` silently miss these rows.
                old_pay = None if h["old_pay"] is None else json.dumps(h["old_pay"])
                new_pay = None if h["new_pay"] is None else json.dumps(h["new_pay"])
                await conn.execute(
                    "INSERT INTO competitor_listing_history"
                    " (listing_id, observed_at, change_type, old_pay, new_pay, content_hash)"
                    " VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6)"
                    " ON CONFLICT DO NOTHING",
                    h["listing_id"], h["observed_at"], h["change_type"],
                    old_pay, new_pay, h["content_hash"])
        await runs.finish(pool, run_id, "ok",
                          rows_inserted=summary["inserted"],
                          rows_updated=summary["updated"] + summary["relisted"],
                          rows_deleted=summary["delisted"],
                          metadata=summary)
        return summary
    except Exception as exc:
        await runs.finish(pool, run_id, "failed", error=str(exc)[:500],
                          metadata=summary)
        raise
