import pytest

from tests.pricing.conftest import requires_pg
from ultimatescrape.jobboards.fetchers import Listing
from ultimatescrape.pricing import ddl
from ultimatescrape.pricing.sync_boards import sync_boards


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
