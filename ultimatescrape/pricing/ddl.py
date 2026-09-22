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
      rate NUMERIC,
      rate_unit_code TEXT,
      rate_unit_decoded TEXT,
      rate_usd_hour NUMERIC,
      synced_at TIMESTAMPTZ NOT NULL,
      is_current BOOLEAN NOT NULL DEFAULT TRUE,
      UNIQUE (source_table, source_pk)
    )
    """,
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
     "Math, physics, chemistry, biology experts creating or grading AI training tasks."),
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

#: Filled by Task 10 (needs live project_type_map / dealforce vocabularies).
VIEW_SQL: str = ""


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
    await apply_view(pool)


async def apply_view(pool: asyncpg.Pool) -> None:
    if VIEW_SQL:
        await pool.execute(VIEW_SQL)
