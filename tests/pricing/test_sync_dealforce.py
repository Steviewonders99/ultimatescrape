import pytest

from tests.pricing.conftest import requires_pg
from ultimatescrape.pricing import ddl
from ultimatescrape.pricing.sync_dealforce import sync_dealforce

REC = {"c": "Amazon", "p": "AWS CV", "v": 150000, "w": 0, "s": "Loss",
       "q": "2025-Q1", "sl": "Data Collection",
       "tags": ["Data Collection", "Image"], "assetType": "Image",
       "loc": "Global"}


def fetcher(records):
    async def _f():
        return records
    return _f


@requires_pg
async def test_upsert_and_missing_marking(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)
    s1 = await sync_dealforce(pool, fetch=fetcher([REC]))
    assert s1["upserted"] == 1
    # feed drops the record -> marked missing, not deleted
    s2 = await sync_dealforce(pool, fetch=fetcher([]))
    assert s2["missing_marked"] == 1
    assert await pool.fetchval(
        "SELECT count(*) FROM dealforce_opportunity WHERE missing_since IS NOT NULL") == 1
    # it returns -> missing cleared
    await sync_dealforce(pool, fetch=fetcher([REC]))
    assert await pool.fetchval(
        "SELECT count(*) FROM dealforce_opportunity WHERE missing_since IS NULL") == 1


@requires_pg
async def test_empty_feed_is_failure_not_wipe(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)
    await sync_dealforce(pool, fetch=fetcher([REC]))

    async def broken():
        raise RuntimeError("catalogue 502")

    with pytest.raises(RuntimeError):
        await sync_dealforce(pool, fetch=broken)
    assert await pool.fetchval(
        "SELECT status FROM sync_runs WHERE name='dealforce_catalogue' "
        "ORDER BY id DESC LIMIT 1") == "failed"
