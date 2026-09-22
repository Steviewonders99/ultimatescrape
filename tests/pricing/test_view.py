from tests.pricing.conftest import requires_pg
from ultimatescrape.pricing import ddl


@requires_pg
async def test_view_selects_and_shapes(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    # minimal project_type_map stand-in with the discovered column names:
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS project_type_map "
        "(project_id TEXT, canonical_category TEXT)"
    )
    await ddl.apply(pool)
    rows = await pool.fetch("SELECT * FROM benchmark_rate_v")
    assert rows == []  # empty DB -> empty view, but it SELECTs
    # seed one classified structured listing and re-select
    lid = await pool.fetchval(
        "INSERT INTO competitor_listing (platform, company, external_id, title,"
        " worker_gig, pay_source, pay_usd_hour_min, country_code,"
        " first_seen_at, last_seen_at, content_hash)"
        " VALUES ('outlier','O','v1','Rater', true,'structured', 30, 'US',"
        " now(), now(),'h') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO competitor_listing_class (listing_id, taxonomy_version,"
        " market_type, confidence, model, classified_at)"
        " VALUES ($1,'v1','llm-eval-rlhf',0.9,'m',now())", lid,
    )
    row = await pool.fetchrow("SELECT * FROM benchmark_rate_v")
    assert row["market_type"] == "llm-eval-rlhf"
    assert row["country_code"] == "US"
    assert float(row["p50_usd_hour"]) == 30.0
    assert row["n_structured"] == 1


@requires_pg
async def test_view_excludes_corporate_role(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS project_type_map "
        "(project_id TEXT, canonical_category TEXT)"
    )
    await ddl.apply(pool)
    lid = await pool.fetchval(
        "INSERT INTO competitor_listing (platform, company, external_id, title,"
        " worker_gig, pay_source, pay_usd_hour_min, country_code,"
        " first_seen_at, last_seen_at, content_hash)"
        " VALUES ('outlier','O','v2','Eng', false,'structured', 60, 'US',"
        " now(), now(),'h2') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO competitor_listing_class (listing_id, taxonomy_version,"
        " market_type, confidence, model, classified_at)"
        " VALUES ($1,'v1','corporate-role',0.9,'m',now())", lid,
    )
    rows = await pool.fetch("SELECT * FROM benchmark_rate_v")
    assert rows == []


@requires_pg
async def test_view_joins_our_rate_and_computes_gap(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS project_type_map "
        "(project_id TEXT, canonical_category TEXT)"
    )
    await ddl.apply(pool)
    # seed a market-side listing at $40/hr for llm-eval-rlhf
    lid = await pool.fetchval(
        "INSERT INTO competitor_listing (platform, company, external_id, title,"
        " worker_gig, pay_source, pay_usd_hour_min, country_code,"
        " first_seen_at, last_seen_at, content_hash)"
        " VALUES ('outlier','O','v3','Rater2', true,'structured', 40, 'US',"
        " now(), now(),'h3') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO competitor_listing_class (listing_id, taxonomy_version,"
        " market_type, confidence, model, classified_at)"
        " VALUES ($1,'v1','llm-eval-rlhf',0.9,'m',now())", lid,
    )
    # seed our own rate at $20/hr, mapped via canonical_category='AI'
    await pool.execute(
        "INSERT INTO project_type_map (project_id, canonical_category)"
        " VALUES ('p1', 'AI')"
    )
    await pool.execute(
        "INSERT INTO our_buy_rate (source_table, source_pk, side, project_id,"
        " rate_usd_hour, synced_at, is_current)"
        " VALUES ('x', '1', 'buy', 'p1', 20, now(), true)"
    )
    row = await pool.fetchrow("SELECT * FROM benchmark_rate_v")
    assert row["market_type"] == "llm-eval-rlhf"
    assert float(row["our_p50_usd_hour"]) == 20.0
    assert row["our_n"] == 1
    # (40 - 20) / 20 * 100 = 100.0
    assert float(row["gap_pct"]) == 100.0


@requires_pg
async def test_apply_without_project_type_map_skips_view(pool):
    # other pricing tests call ddl.apply() without a project_type_map stand-in
    # (that table only exists on the real warehouse) -- apply() must not error.
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await ddl.apply(pool)
    exists = await pool.fetchval(
        "SELECT to_regclass('public.benchmark_rate_v') IS NOT NULL"
    )
    assert exists is False
