"""Integration test for sync_rates: proves the batched executemany() path
upserts identically to what the old per-row execute() path did, and stays
idempotent across a re-run. proxydb.query is monkeypatched to a canned
page sequence (like test_sync_boards.py's fake_fetch) so this runs against
TEST_DATABASE_URL only, no live proxy dependency.
"""

from tests.pricing.conftest import requires_pg
from ultimatescrape.pricing import ddl, sync_rates
from ultimatescrape.pricing.rate_units import RateSource

FAKE_SOURCE = RateSource("fake_rate_table", "id", "project_id", "locale", "buy")

ROWS = [
    {"id": 1, "rate": 42.0, "rate_unit": "1", "currency": "USD",
     "project_id": 100, "locale": "en_US"},           # hourly, USD -> as-is
    {"id": 2, "rate": 100.0, "rate_unit": "1", "currency": "CNY",
     "project_id": 100, "locale": "de_DE"},            # hourly, CNY -> fx-converted
    {"id": 3, "rate": 0.05, "rate_unit": "2", "currency": "USD",
     "project_id": 100, "locale": "fr_FR"},             # per-word -> usd_hour NULL
    {"id": 4, "rate": 12.0, "rate_unit": "1", "currency": None,
     "project_id": 100, "locale": "es_ES"},             # hourly, unknown currency -> NULL
    {"id": 5, "rate": 7.0, "rate_unit": None, "currency": "USD",
     "project_id": 100, "locale": "it_IT"},             # unmapped code -> NULL/NULL
]


def fake_query_pages(pages):
    """pages: list of row-lists. Each call pops the next page in order
    (an exhausted list yields [], ending that table's pagination), mirroring
    proxydb.query's real page semantics without hitting the live proxy."""
    calls = {"n": 0}

    async def _fake(db, sql, **kw):
        i = calls["n"]
        calls["n"] += 1
        return pages[i] if i < len(pages) else []

    return _fake


@requires_pg
async def test_sync_rates_batched_executemany_matches_and_is_idempotent(pool, monkeypatch):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)

    monkeypatch.setattr(sync_rates, "RATE_SOURCES", [FAKE_SOURCE])
    monkeypatch.setattr(sync_rates.proxydb, "query", fake_query_pages([ROWS, []]))

    summary1 = await sync_rates.sync_rates(pool)
    assert summary1["tables"]["fake_rate_table"] == 5

    rows = await pool.fetch(
        "SELECT source_pk, currency, rate_unit_code, rate_usd_hour,"
        " rate_fx_as_of, is_current FROM our_buy_rate"
        " WHERE source_table='fake_rate_table' ORDER BY source_pk")
    by_pk = {r["source_pk"]: r for r in rows}
    assert len(by_pk) == 5
    assert all(r["is_current"] for r in rows)

    assert by_pk["1"]["rate_usd_hour"] == 42.0
    assert by_pk["1"]["rate_fx_as_of"] is None  # USD -> no fx stamp

    assert by_pk["2"]["rate_usd_hour"] is not None  # CNY converted
    assert by_pk["2"]["rate_usd_hour"] != 100.0      # not the raw CNY number
    assert by_pk["2"]["rate_fx_as_of"] is not None   # fx stamp present

    assert by_pk["3"]["rate_usd_hour"] is None  # per-word, not hourly

    assert by_pk["4"]["rate_usd_hour"] is None  # unknown currency, abstain
    assert by_pk["4"]["rate_fx_as_of"] is None

    assert by_pk["5"]["rate_unit_code"] is None  # NULL source code, never "None"
    assert by_pk["5"]["rate_usd_hour"] is None

    # Re-run with the SAME canned pages through the SAME batched
    # executemany() path -- idempotency must hold: no duplicate rows, same
    # count, everything still current.
    monkeypatch.setattr(sync_rates.proxydb, "query", fake_query_pages([ROWS, []]))
    summary2 = await sync_rates.sync_rates(pool)
    assert summary2["tables"]["fake_rate_table"] == 5

    total = await pool.fetchval(
        "SELECT count(*) FROM our_buy_rate WHERE source_table='fake_rate_table'")
    current = await pool.fetchval(
        "SELECT count(*) FROM our_buy_rate"
        " WHERE source_table='fake_rate_table' AND is_current")
    assert total == 5
    assert current == 5


@requires_pg
async def test_sync_rates_batches_across_multiple_pages(pool, monkeypatch):
    """A source with more than one page must still land every row via
    multiple executemany() calls (one per page), not just the first."""
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)

    page1 = [{"id": i, "rate": 1.0, "rate_unit": "1", "currency": "USD",
              "project_id": 1, "locale": "en_US"} for i in range(1, 4)]
    page2 = [{"id": i, "rate": 2.0, "rate_unit": "1", "currency": "USD",
              "project_id": 1, "locale": "en_US"} for i in range(4, 6)]

    monkeypatch.setattr(sync_rates, "RATE_SOURCES", [FAKE_SOURCE])
    monkeypatch.setattr(sync_rates.proxydb, "query", fake_query_pages([page1, page2, []]))

    summary = await sync_rates.sync_rates(pool)
    assert summary["tables"]["fake_rate_table"] == 5

    total = await pool.fetchval(
        "SELECT count(*) FROM our_buy_rate WHERE source_table='fake_rate_table'")
    assert total == 5
