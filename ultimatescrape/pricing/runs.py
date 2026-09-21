"""sync_runs telemetry (R6) for the pricing writers.

Convention (centric-intake): the PUBLIC sync function writes
finish(status='failed') before re-raising; callers add no try/except.
Warehouse schema (introspected 2026-09-21): id, name, source, started_at,
finished_at, status, rows_inserted, rows_updated, rows_deleted, error,
metadata jsonb.
"""

from __future__ import annotations

import json

import asyncpg


async def start(
    pool: asyncpg.Pool, name: str, source: str, metadata: dict | None = None
) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO sync_runs (name, source, started_at, status, metadata)
        VALUES ($1, $2, now(), 'running', $3::jsonb)
        RETURNING id
        """,
        name,
        source,
        json.dumps(metadata or {}),
    )
    return row["id"]


async def finish(
    pool: asyncpg.Pool,
    run_id: int,
    status: str,
    *,
    error: str = "",
    rows_inserted: int = 0,
    rows_updated: int = 0,
    rows_deleted: int = 0,
    metadata: dict | None = None,
) -> None:
    await pool.execute(
        """
        UPDATE sync_runs
        SET finished_at = now(), status = $2, error = NULLIF($3, ''),
            rows_inserted = $4, rows_updated = $5, rows_deleted = $6,
            metadata = COALESCE(metadata, '{}'::jsonb) || $7::jsonb
        WHERE id = $1
        """,
        run_id,
        status,
        (error or "")[:500],
        rows_inserted,
        rows_updated,
        rows_deleted,
        json.dumps(metadata or {}),
    )
