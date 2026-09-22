from tests.pricing.conftest import requires_pg
from ultimatescrape.pricing import ddl


async def _seed_ptm_stand_in(pool):
    # minimal project_type_map stand-in with the discovered column names,
    # now including jc_code (the fine tier added in the 2026-09-22 review).
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS project_type_map "
        "(project_id TEXT, jc_code TEXT, canonical_category TEXT)"
    )


@requires_pg
async def test_view_selects_and_shapes(pool):
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await _seed_ptm_stand_in(pool)
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
    await _seed_ptm_stand_in(pool)
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
async def test_view_joins_our_rate_via_jc_code_fine_tier(pool):
    # llm-eval-rlhf has our_jc_codes=['jc_0004'] -- a market_taxonomy row
    # with a non-empty our_jc_codes matches ONLY that exact jc_code; the
    # coarse our_project_types=['AI'] fallback is bypassed for this key.
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await _seed_ptm_stand_in(pool)
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
    # a project on the RIGHT jc_code ($20/hr) -- must be picked up
    await pool.execute(
        "INSERT INTO project_type_map (project_id, jc_code, canonical_category)"
        " VALUES ('p1', 'jc_0004', 'AI')"
    )
    await pool.execute(
        "INSERT INTO our_buy_rate (source_table, source_pk, side, project_id,"
        " rate_usd_hour, synced_at, is_current)"
        " VALUES ('x', '1', 'buy', 'p1', 20, now(), true)"
    )
    # a project on a DIFFERENT jc_code but the SAME coarse 'AI' category
    # ($200/hr) -- must NOT be picked up, proving fine-tier is exclusive
    await pool.execute(
        "INSERT INTO project_type_map (project_id, jc_code, canonical_category)"
        " VALUES ('p2', 'jc_0018', 'AI')"
    )
    await pool.execute(
        "INSERT INTO our_buy_rate (source_table, source_pk, side, project_id,"
        " rate_usd_hour, synced_at, is_current)"
        " VALUES ('x', '2', 'buy', 'p2', 200, now(), true)"
    )
    row = await pool.fetchrow("SELECT * FROM benchmark_rate_v")
    assert row["market_type"] == "llm-eval-rlhf"
    assert row["our_n"] == 1  # only the jc_0004 project, not the jc_0018 one
    assert float(row["our_p50_usd_hour"]) == 20.0
    # (40 - 20) / 20 * 100 = 100.0
    assert float(row["gap_pct"]) == 100.0


@requires_pg
async def test_view_joins_our_rate_via_coarse_fallback(pool):
    # content-moderation has our_jc_codes=[] (no documented finer signal) and
    # our_project_types=['AI'] -- it must fall back to matching ANY project on
    # the coarse 'AI' category, regardless of jc_code.
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    await _seed_ptm_stand_in(pool)
    await ddl.apply(pool)
    lid = await pool.fetchval(
        "INSERT INTO competitor_listing (platform, company, external_id, title,"
        " worker_gig, pay_source, pay_usd_hour_min, country_code,"
        " first_seen_at, last_seen_at, content_hash)"
        " VALUES ('outlier','O','v4','Mod', true,'structured', 40, 'US',"
        " now(), now(),'h4') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO competitor_listing_class (listing_id, taxonomy_version,"
        " market_type, confidence, model, classified_at)"
        " VALUES ($1,'v1','content-moderation',0.9,'m',now())", lid,
    )
    # jc_0018 has no documented finer signal, but is still 'AI' coarsely
    await pool.execute(
        "INSERT INTO project_type_map (project_id, jc_code, canonical_category)"
        " VALUES ('p3', 'jc_0018', 'AI')"
    )
    await pool.execute(
        "INSERT INTO our_buy_rate (source_table, source_pk, side, project_id,"
        " rate_usd_hour, synced_at, is_current)"
        " VALUES ('x', '3', 'buy', 'p3', 20, now(), true)"
    )
    row = await pool.fetchrow("SELECT * FROM benchmark_rate_v")
    assert row["market_type"] == "content-moderation"
    assert row["our_n"] == 1
    assert float(row["our_p50_usd_hour"]) == 20.0
    assert float(row["gap_pct"]) == 100.0


@requires_pg
async def test_apply_without_project_type_map_skips_view(pool, caplog):
    # other pricing tests call ddl.apply() without a project_type_map stand-in
    # (that table only exists on the real warehouse) -- apply() must not error,
    # and the skip must be logged rather than silent.
    await pool.execute(ddl.TEST_SYNC_RUNS_DDL)
    with caplog.at_level("WARNING", logger="uscrape.pricing"):
        await ddl.apply(pool)
    exists = await pool.fetchval(
        "SELECT to_regclass('public.benchmark_rate_v') IS NOT NULL"
    )
    assert exists is False
    assert "benchmark_rate_v skipped" in caplog.text
