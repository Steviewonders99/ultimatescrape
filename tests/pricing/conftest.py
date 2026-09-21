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
        await p.close()
