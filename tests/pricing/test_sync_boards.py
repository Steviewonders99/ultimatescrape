import json

import pytest

from tests.pricing.conftest import requires_pg
from ultimatescrape.jobboards.fetchers import Listing
from ultimatescrape.pricing import ddl
from ultimatescrape.pricing.sync_boards import _assert_text_fields, sync_boards


def L(ext="a1", pay_min=25.0, title="Rater", desc="desc"):
    return Listing(platform="outlier", company="Outlier", title=title,
                   external_id=ext, pay_min=pay_min, pay_currency="USD",
                   pay_unit="hour", pay_source="structured", worker_gig=True,
                   location="Remote - United States", description_full=desc,
                   description_excerpt=desc[:300])


def fake_fetch(listings, errors=None):
    async def _f(keys):
        return {"outlier": listings}, (errors or {})
    return _f


@requires_pg
async def test_full_lifecycle(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)

    s1 = await sync_boards(pool, platforms=["outlier"], fetch=fake_fetch([L()]))
    assert s1["inserted"] == 1
    country = await pool.fetchval("SELECT country_code FROM competitor_listing")
    assert country == "US"
    # old_pay for a 'new' row must be SQL NULL, not a JSONB null scalar —
    # WHERE old_pay IS NULL must find it.
    assert await pool.fetchval(
        "SELECT old_pay FROM competitor_listing_history"
        " WHERE change_type='new'") is None

    s2 = await sync_boards(pool, platforms=["outlier"],
                           fetch=fake_fetch([L(pay_min=30.0)]))
    assert s2["updated"] == 1
    h = await pool.fetchval(
        "SELECT count(*) FROM competitor_listing_history WHERE change_type='pay_change'")
    assert h == 1

    # zero-listing anomaly: platform 'succeeds' with 0 rows while 1 live row
    # exists -> demoted to failed. It is the ONLY platform, so the run is
    # all-platforms-failed: loud RuntimeError, sync_runs 'failed', no delist.
    with pytest.raises(RuntimeError, match="all platforms failed"):
        await sync_boards(pool, platforms=["outlier"], fetch=fake_fetch([]))
    still_live = await pool.fetchval(
        "SELECT count(*) FROM competitor_listing WHERE delisted_at IS NULL")
    assert still_live == 1
    last = await pool.fetchrow(
        "SELECT status, error FROM sync_runs WHERE name='competitor_boards'"
        " ORDER BY id DESC LIMIT 1")
    assert last["status"] == "failed" and "zero-listing anomaly" in last["error"]

    # a JD change invalidates the v1 class row so classify_new re-reads it
    await pool.execute(
        "INSERT INTO competitor_listing_class (listing_id, taxonomy_version,"
        " market_type, confidence, model, classified_at)"
        " SELECT id, 'v1', 'search-rating', 0.9, 'm', now()"
        " FROM competitor_listing LIMIT 1")
    await sync_boards(pool, platforms=["outlier"],
                      fetch=fake_fetch([L(pay_min=30.0, desc="rewritten JD")]))
    assert await pool.fetchval(
        "SELECT count(*) FROM competitor_listing_class") == 0

    ok_runs = await pool.fetchval(
        "SELECT count(*) FROM sync_runs WHERE name='competitor_boards' AND status='ok'")
    assert ok_runs >= 2


@requires_pg
async def test_dry_run_writes_nothing(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)
    before = await pool.fetchval("SELECT count(*) FROM sync_runs")
    s = await sync_boards(pool, platforms=["outlier"], dry_run=True,
                          fetch=fake_fetch([L(ext="dry")]))
    assert s["inserted"] == 1  # reported, not applied
    assert await pool.fetchval("SELECT count(*) FROM sync_runs") == before
    assert await pool.fetchval(
        "SELECT count(*) FROM competitor_listing WHERE external_id='dry'") == 0


def test_text_field_guard_rejects_dict_location():
    """Defense in depth: a bad adapter (e.g. a future non-outlier shape
    surprise) must fail with a clear, named error on the apply path — not
    a bare AttributeError three calls into normalize.country_from_location.
    No DB needed; this is a pure guard on the Listing object."""
    l = L(ext="bad")
    l.location = {"name": "Remote - France"}  # the exact shape seen live
    with pytest.raises(TypeError, match=r"platform='outlier'.*location.*dict"):
        _assert_text_fields(l)


def test_text_field_guard_allows_none_and_str():
    l = L(ext="ok")
    l.department = None  # None is a valid TEXT-column value
    _assert_text_fields(l)  # must not raise


@requires_pg
async def test_duplicate_external_id_in_one_fetch_is_deduped_not_crashed(pool):
    """Production incident 22 Sep 2026: mercor/imerit adapters emitted an
    empty external_id for every listing, so two-or-more in the same fetch
    collided on the (platform, external_id) UNIQUE constraint and rolled
    back the whole transaction. The adapters are now fixed at the source
    (see test_fetcher_fulljd.py), but the writer must ALSO guard here —
    dedupe by (platform, external_id), keep the first, count the rest —
    so any future adapter quirk degrades to a counted drop, not a crash."""
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)
    first = L(ext="dup1", title="First")
    second = L(ext="dup1", title="Second")  # same key, different content
    s = await sync_boards(pool, platforms=["outlier"],
                          fetch=fake_fetch([first, second]))
    assert s["inserted"] == 1
    assert s["duplicate_keys_dropped"] == 1
    n = await pool.fetchval(
        "SELECT count(*) FROM competitor_listing WHERE external_id='dup1'")
    assert n == 1
    title = await pool.fetchval(
        "SELECT title FROM competitor_listing WHERE external_id='dup1'")
    assert title == "First"  # first wins, deterministically

    meta = json.loads(await pool.fetchval(
        "SELECT metadata FROM sync_runs WHERE name='competitor_boards'"
        " ORDER BY id DESC LIMIT 1"))
    assert meta["duplicate_keys_dropped"] == 1


@requires_pg
async def test_missing_external_id_dropped_and_counted(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)
    bad = L(ext="")   # empty external_id, e.g. an adapter id-field bug
    good = L(ext="ok1")
    s = await sync_boards(pool, platforms=["outlier"],
                          fetch=fake_fetch([bad, good]))
    assert s["inserted"] == 1
    assert s["missing_external_id"] == {"outlier": 1}
    n = await pool.fetchval("SELECT count(*) FROM competitor_listing")
    assert n == 1

    meta = json.loads(await pool.fetchval(
        "SELECT metadata FROM sync_runs WHERE name='competitor_boards'"
        " ORDER BY id DESC LIMIT 1"))
    assert meta["missing_external_id"] == {"outlier": 1}


@requires_pg
async def test_all_empty_ids_withholds_delist_authority_not_mass_delist(pool):
    """Mass-delist side door (production incident class, occurred twice this
    cycle): the zero-listing anomaly guard checks ok[key] BEFORE the
    missing-external_id drop/dedup step runs. If a platform's fetch returns
    listings that ALL have an empty external_id, ok[key] is still non-empty
    at that check -> the platform reads as "succeeded" -> every one of its
    surviving-in-DB rows would be delisted by plan_changes, with the run
    itself reporting status 'ok'. A platform must never get delisting
    authority in the same run it silently lost 100% of its ids."""
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)

    # seed one live row for 'outlier'
    s1 = await sync_boards(pool, platforms=["outlier"], fetch=fake_fetch([L(ext="keep-me")]))
    assert s1["inserted"] == 1

    # next fetch: every listing from 'outlier' has an empty external_id
    bad_only = [L(ext="", title="t1"), L(ext="", title="t2")]
    s2 = await sync_boards(pool, platforms=["outlier"], fetch=fake_fetch(bad_only))

    assert s2["missing_external_id"] == {"outlier": 2}
    assert s2["delist_withheld"] == ["outlier"]
    assert s2["delisted"] == 0

    still_live = await pool.fetchval(
        "SELECT count(*) FROM competitor_listing"
        " WHERE external_id='keep-me' AND delisted_at IS NULL")
    assert still_live == 1

    last = await pool.fetchrow(
        "SELECT status, metadata FROM sync_runs WHERE name='competitor_boards'"
        " ORDER BY id DESC LIMIT 1")
    assert last["status"] == "ok"
    meta = json.loads(last["metadata"])
    assert meta["delist_withheld"] == ["outlier"]
