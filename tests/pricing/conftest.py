import os

import pytest

TEST_DSN = os.getenv("TEST_DATABASE_URL", "")

requires_pg = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL not set (docker run postgres:16)"
)


@pytest.fixture
async def pool():
    import asyncpg

    p = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2)
    try:
        yield p
    finally:
        # Clean up all pricing-related tables between tests
        async with p.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS competitor_listing_class CASCADE")
            await conn.execute("DROP TABLE IF EXISTS competitor_listing_history CASCADE")
            await conn.execute("DROP TABLE IF EXISTS competitor_listing CASCADE")
            await conn.execute("DROP TABLE IF EXISTS market_taxonomy CASCADE")
            await conn.execute("DROP TABLE IF EXISTS our_buy_rate CASCADE")
            await conn.execute("DROP TABLE IF EXISTS dealforce_opportunity CASCADE")
            await conn.execute("DROP TABLE IF EXISTS sync_runs CASCADE")
        await p.close()
