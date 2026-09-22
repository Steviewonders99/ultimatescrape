"""Idempotent warehouse DDL + taxonomy seed for the pricing benchmark.

Spec: docs/superpowers/specs/2026-09-21-uscrape-competitor-benchmark-design.md §4.
Additive only. Every table carries a UNIQUE constraint.
"""

from __future__ import annotations

import asyncpg

DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS competitor_listing (
      id             BIGSERIAL PRIMARY KEY,
      platform       TEXT NOT NULL,
      company        TEXT NOT NULL,
      external_id    TEXT NOT NULL,
      url            TEXT,
      title          TEXT NOT NULL,
      department     TEXT, employment_type TEXT, remote TEXT,
      worker_gig     BOOLEAN NOT NULL DEFAULT FALSE,
      location_raw   TEXT,
      country_code   TEXT,
      description_full    TEXT,
      description_excerpt TEXT,
      pay_min NUMERIC, pay_max NUMERIC,
      pay_currency TEXT, pay_unit TEXT, pay_raw TEXT,
      pay_source TEXT,
      pay_usd_hour_min NUMERIC,
      pay_usd_hour_max NUMERIC,
      pay_fx_as_of DATE,
      posted_at DATE,
      first_seen_at TIMESTAMPTZ NOT NULL,
      last_seen_at  TIMESTAMPTZ NOT NULL,
      delisted_at   TIMESTAMPTZ,
      content_hash  TEXT NOT NULL,
      UNIQUE (platform, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_complist_platform ON competitor_listing (platform)",
    "CREATE INDEX IF NOT EXISTS idx_complist_country ON competitor_listing (country_code)",
    """
    CREATE TABLE IF NOT EXISTS competitor_listing_history (
      id BIGSERIAL PRIMARY KEY,
      listing_id BIGINT NOT NULL REFERENCES competitor_listing(id),
      observed_at TIMESTAMPTZ NOT NULL,
      change_type TEXT NOT NULL,
      old_pay JSONB, new_pay JSONB,
      content_hash TEXT,
      UNIQUE (listing_id, observed_at, change_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitor_listing_class (
      listing_id BIGINT NOT NULL REFERENCES competitor_listing(id),
      taxonomy_version TEXT NOT NULL,
      market_type TEXT NOT NULL,
      confidence NUMERIC NOT NULL,
      attrs JSONB,
      model TEXT NOT NULL,
      classified_at TIMESTAMPTZ NOT NULL,
      UNIQUE (listing_id, taxonomy_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_taxonomy (
      key TEXT PRIMARY KEY,
      label TEXT NOT NULL,
      description TEXT NOT NULL,
      our_project_types TEXT[] NOT NULL DEFAULT '{}',
      dealforce_service_lines TEXT[] NOT NULL DEFAULT '{}',
      dealforce_tags TEXT[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS our_buy_rate (
      id BIGSERIAL PRIMARY KEY,
      source_table TEXT NOT NULL,
      source_pk TEXT NOT NULL,
      side TEXT NOT NULL DEFAULT 'buy',
      project_id TEXT,
      locale TEXT,
      country_code TEXT,
      currency TEXT,
      rate NUMERIC,
      rate_unit_code TEXT,
      rate_unit_decoded TEXT,
      rate_usd_hour NUMERIC,
      rate_fx_as_of DATE,
      synced_at TIMESTAMPTZ NOT NULL,
      is_current BOOLEAN NOT NULL DEFAULT TRUE,
      UNIQUE (source_table, source_pk)
    )
    """,
    # CREATE TABLE IF NOT EXISTS above does not retroactively add columns to
    # an our_buy_rate that already existed (Task 8 shipped without currency/
    # rate_fx_as_of) -- additive backport per R2/R3.
    "ALTER TABLE our_buy_rate ADD COLUMN IF NOT EXISTS currency TEXT",
    "ALTER TABLE our_buy_rate ADD COLUMN IF NOT EXISTS rate_fx_as_of DATE",
    """
    CREATE TABLE IF NOT EXISTS dealforce_opportunity (
      id BIGSERIAL PRIMARY KEY,
      client TEXT NOT NULL,
      project TEXT NOT NULL,
      quarter TEXT NOT NULL,
      value_usd NUMERIC,
      weighted_usd NUMERIC,
      status TEXT,
      service_line TEXT,
      tags TEXT[],
      asset_type TEXT,
      loc TEXT,
      synced_at TIMESTAMPTZ NOT NULL,
      missing_since TIMESTAMPTZ,
      UNIQUE (client, project, quarter)
    )
    """,
]

#: (key, label, description) — mapping arrays are filled in Task 10 once the
#: live project_type_map and dealforce service-line vocabularies are read.
TAXONOMY_SEED: list[tuple[str, str, str]] = [
    ("llm-eval-rlhf", "LLM evaluation / RLHF",
     "Rating, ranking or rewriting AI model responses; preference data; RLHF."),
    ("llm-domain-expert-stem", "Domain expert - STEM",
     ("Math, physics, chemistry, biology and other natural or life sciences — "
      "purely social-science roles are NOT stem.")),
    ("llm-domain-expert-professional", "Domain expert - professional",
     "Law, finance, medicine, accounting or other credentialed-professional AI work."),
    ("coding-eval", "Coding / SWE tasks",
     ("Writing, reviewing or evaluating code for AI training; SWE-bench-style tasks. "
      "Production/serving infrastructure engineering (MLOps, deployment) is not this key.")),
    ("search-rating", "Search & ads rating",
     "Search-engine result rating, ads quality rating, relevance evaluation."),
    ("data-annotation", "Data annotation",
     "Image/video/text labeling, bounding boxes, segmentation, categorisation."),
    ("audio-transcription", "Transcription",
     "Audio/video transcription, captioning, subtitling."),
    ("audio-recording", "Voice & speech collection",
     "Recording speech, voice acting for datasets, pronunciation reading."),
    ("translation-localization", "Translation & localisation",
     "Translation, MT post-editing, localisation QA."),
    ("linguistics-annotation", "Linguistics",
     "Linguistic annotation, phonetics, lexicography, grammar systems."),
    ("data-collection-field", "Field data collection",
     "Collecting images, video, documents, receipts or real-world data."),
    ("content-moderation", "Content moderation",
     ("Policy rating, trust & safety review, harmful-content triage on "
      "human-submitted content.")),
    ("writing-editing", "Writing & editing",
     "Creative writing, prompt writing, copy editing for AI training."),
    ("ai-red-teaming", "AI red-teaming",
     ("Adversarial testing, jailbreak discovery, or teaching a model to safely "
      "handle sensitive or dangerous topics.")),
    ("corporate-role", "Corporate role (competitor hiring)",
     ("The competitor's own staff hiring - engineering, sales, ops running the "
      "platform itself. Tracked for intel; excluded from pay benchmarks. A "
      "contractor placed onto a third-party client's team (even via the "
      "competitor's marketplace) is not this key.")),
    ("other", "Other / unclassifiable",
     "Does not fit any defined market type, or too vague to place."),
]

#: Minimal clone for ephemeral test databases only. The real warehouse
#: already has sync_runs; never run this against it.
TEST_SYNC_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS sync_runs (
  id BIGSERIAL PRIMARY KEY, name TEXT, source TEXT,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, status TEXT,
  rows_inserted INT, rows_updated INT, rows_deleted INT,
  error TEXT, metadata JSONB
)
"""

#: market_type -> (our project types, dealforce service lines, dealforce tags)
#: Vocabularies read live on 2026-09-22 (Task 10 Step 1) — every value below
#: appears verbatim in its source vocabulary; none are invented.
#:
#: Sources:
#:   our_project_types <- warehouse project_type_map.canonical_category
#:     (4 distinct: 'AI', 'Annotation', 'Data Collection',
#:      'Translation & Transcription' — only 68 rows / 7 jc_codes total, so
#:      several market_type keys legitimately share the same coarse bucket)
#:   dealforce_service_lines <- catalogue `sl` (14 distinct)
#:   dealforce_tags <- catalogue `tags` (12 distinct)
#:
#: Consciously left with an empty ours list: corporate-role, other (no
#: internal-project analog; corporate-role is filtered out of the view by
#: definition). Consciously left with empty dealforce lists: llm-domain-
#: expert-stem/professional, coding-eval, search-rating, content-moderation,
#: writing-editing, ai-red-teaming, audio-transcription, translation-
#: localization, linguistics-annotation, corporate-role, other — the
#: catalogue's sl/tags vocabulary has no term distinguishing these (no
#: "coding", "search", "moderation", "writing", "red-team", "translation",
#: "linguistics" token in either list); forcing a broad proxy (e.g. GenAI/LLM
#: for every AI-flavored key) would misattribute the same dollars to
#: multiple market types. Consciously unused source values: sl combo/unclear
#: strings ('DC/TX/TL', 'Data Collection & Annotation', 'Data Collection/
#: Annotation', 'Data Collection/OTS', 'Data collection + OTS + transcription',
#: 'Data collection or OTS', 'OTS', 'Staff Augmentation', 'Staffing') and
#: tags 'Multimodal' (too generic across every asset type) and 'Staffing'
#: (not data-type specific). Image/Video/Audio tag assignment is grounded in
#: measured co-occurrence with the functional tags: Image/Video co-occur with
#: 'Data Collection' 72%/84% of the time (vs 37%/29% with 'Annotation') so
#: they went to data-collection-field; 'Text' co-occurs with 'Annotation' 69%
#: of the time (vs 37% with 'Data Collection') so it went to data-annotation;
#: 'Audio' is reserved for the narrower audio-recording key rather than the
#: broad data-collection-field bucket.
TAXONOMY_MAPPINGS: dict[str, tuple[list[str], list[str], list[str]]] = {
    "llm-eval-rlhf": (["AI"], ["GenAI / LLM"], ["LLM/GenAI", "RL/RLHF"]),
    "llm-domain-expert-stem": (["AI"], [], []),
    "llm-domain-expert-professional": (["AI"], [], []),
    "coding-eval": (["AI"], [], []),
    "search-rating": (["AI"], [], []),
    "data-annotation": (
        ["Annotation"], ["Annotation", "Data Annotation"], ["Annotation", "Text"],
    ),
    "audio-transcription": (["Translation & Transcription"], [], []),
    "audio-recording": (["Data Collection"], [], ["Audio"]),
    "translation-localization": (["Translation & Transcription"], [], []),
    "linguistics-annotation": (["Annotation"], [], []),
    "data-collection-field": (
        ["Data Collection"],
        ["Data Collection", "Data collection"],
        ["Data Collection", "Image", "Video", "3D/LiDAR", "Physical AI"],
    ),
    "content-moderation": (["AI"], [], []),
    "writing-editing": (["AI"], [], []),
    "ai-red-teaming": (["AI"], [], []),
    "corporate-role": ([], [], []),
    "other": ([], [], []),
}

#: Filled by Task 10 (needs live project_type_map / dealforce vocabularies).
#: <type column> = project_type_map.canonical_category (coarse category text)
#: <project id column> = project_type_map.project_id (uuid; cast ::text to
#: join our_buy_rate.project_id, which is stored as TEXT).
VIEW_SQL: str = """
CREATE OR REPLACE VIEW benchmark_rate_v AS
WITH classified AS (
  SELECT l.id, l.platform, l.pay_source, l.pay_usd_hour_min, l.pay_usd_hour_max,
         COALESCE(NULLIF(l.country_code, ''), 'GLOBAL') AS country_code,
         c.market_type
  FROM competitor_listing l
  JOIN competitor_listing_class c
    ON c.listing_id = l.id AND c.taxonomy_version = 'v1'
  WHERE l.delisted_at IS NULL AND c.market_type <> 'corporate-role'
),
cells AS (
  SELECT market_type, country_code,
         COUNT(*)::int AS n_listings,
         COUNT(*) FILTER (WHERE pay_source='structured'
                          AND pay_usd_hour_min IS NOT NULL)::int AS n_structured,
         COUNT(*) FILTER (WHERE pay_source='parsed'
                          AND pay_usd_hour_min IS NOT NULL)::int AS n_parsed,
         percentile_cont(0.25) WITHIN GROUP (ORDER BY
           (pay_usd_hour_min + COALESCE(pay_usd_hour_max, pay_usd_hour_min)) / 2.0)
           FILTER (WHERE pay_source='structured' AND pay_usd_hour_min IS NOT NULL)
           AS p25_usd_hour,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY
           (pay_usd_hour_min + COALESCE(pay_usd_hour_max, pay_usd_hour_min)) / 2.0)
           FILTER (WHERE pay_source='structured' AND pay_usd_hour_min IS NOT NULL)
           AS p50_usd_hour,
         percentile_cont(0.75) WITHIN GROUP (ORDER BY
           (pay_usd_hour_min + COALESCE(pay_usd_hour_max, pay_usd_hour_min)) / 2.0)
           FILTER (WHERE pay_source='structured' AND pay_usd_hour_min IS NOT NULL)
           AS p75_usd_hour,
         MIN(pay_usd_hour_min) FILTER (WHERE pay_source='structured') AS min_usd_hour,
         MAX(COALESCE(pay_usd_hour_max, pay_usd_hour_min))
           FILTER (WHERE pay_source='structured') AS max_usd_hour,
         ARRAY_AGG(DISTINCT platform) AS platforms
  FROM classified
  GROUP BY 1, 2
),
ours AS (
  SELECT mt.key AS market_type,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY r.rate_usd_hour)
           AS our_p50_usd_hour,
         COUNT(*)::int AS our_n
  FROM market_taxonomy mt
  JOIN project_type_map ptm ON ptm.canonical_category = ANY (mt.our_project_types)
  JOIN our_buy_rate r
    ON r.project_id = ptm.project_id::text
   AND r.is_current AND r.side = 'buy' AND r.rate_usd_hour IS NOT NULL
  GROUP BY 1
),
df AS (
  SELECT mt.key AS market_type,
         SUM(d.value_usd) FILTER (WHERE d.status = 'Won') AS dealforce_won_usd,
         SUM(d.weighted_usd) FILTER (WHERE d.status NOT IN ('Won', 'Loss'))
           AS dealforce_pipeline_usd,
         COUNT(*)::int AS dealforce_n
  FROM market_taxonomy mt
  JOIN dealforce_opportunity d
    ON (d.service_line = ANY (mt.dealforce_service_lines)
        OR d.tags && mt.dealforce_tags)
  WHERE d.missing_since IS NULL
  GROUP BY 1
)
SELECT c.*,
       o.our_p50_usd_hour, o.our_n,
       CASE WHEN o.our_p50_usd_hour > 0 AND c.p50_usd_hour IS NOT NULL
            THEN ROUND((100.0 * (c.p50_usd_hour - o.our_p50_usd_hour)
                       / o.our_p50_usd_hour)::numeric, 1)
       END AS gap_pct,
       df.dealforce_won_usd, df.dealforce_pipeline_usd, df.dealforce_n
FROM cells c
LEFT JOIN ours o USING (market_type)
LEFT JOIN df USING (market_type)
"""


async def apply(pool: asyncpg.Pool) -> None:
    for stmt in DDL:
        await pool.execute(stmt)
    for key, label, description in TAXONOMY_SEED:
        await pool.execute(
            """
            INSERT INTO market_taxonomy (key, label, description)
            VALUES ($1, $2, $3)
            ON CONFLICT (key) DO UPDATE
              SET label = EXCLUDED.label, description = EXCLUDED.description
            """,
            key, label, description,
        )
    for key, (ours, sls, tags) in TAXONOMY_MAPPINGS.items():
        await pool.execute(
            "UPDATE market_taxonomy SET our_project_types=$2,"
            " dealforce_service_lines=$3, dealforce_tags=$4 WHERE key=$1",
            key, ours, sls, tags,
        )
    await apply_view(pool)


async def apply_view(pool: asyncpg.Pool) -> None:
    if not VIEW_SQL:
        return
    # project_type_map lives in the real warehouse but not in every ephemeral
    # test DB; skip view creation rather than error when it is absent so
    # ddl.apply() stays safe to call from tests that don't need the view.
    has_ptm = await pool.fetchval(
        "SELECT to_regclass('public.project_type_map') IS NOT NULL"
    )
    if not has_ptm:
        return
    await pool.execute(VIEW_SQL)
