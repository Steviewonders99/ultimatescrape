import pytest

from tests.pricing.conftest import requires_pg
from ultimatescrape.pricing import ddl


@requires_pg
async def test_ddl_applies_twice_and_seeds(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)
    await ddl.apply(pool)  # idempotency is the point
    n = await pool.fetchval("SELECT count(*) FROM market_taxonomy")
    assert n == 16
    cols = await pool.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='competitor_listing'"
    )
    names = {c["column_name"] for c in cols}
    assert {"description_full", "pay_usd_hour_min", "content_hash",
            "first_seen_at", "delisted_at"} <= names


@requires_pg
async def test_unique_constraint_holds(pool):
    await ddl.apply(pool)
    await pool.execute(
        "INSERT INTO competitor_listing (platform, company, external_id, title,"
        " first_seen_at, last_seen_at, content_hash)"
        " VALUES ('x','X','1','t', now(), now(), 'h')"
    )
    with pytest.raises(Exception, match="duplicate key"):
        await pool.execute(
            "INSERT INTO competitor_listing (platform, company, external_id,"
            " title, first_seen_at, last_seen_at, content_hash)"
            " VALUES ('x','X','1','t2', now(), now(), 'h2')"
        )
