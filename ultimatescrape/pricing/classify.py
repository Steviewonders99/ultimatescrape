"""LLM classification of competitor listings into the market taxonomy.

Only new/changed listings reach the model. Parsing is defensive: an
answer outside the taxonomy is evidence of model failure, recorded as
'other' at low confidence rather than trusted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg

TAXONOMY_VERSION = "v1"
BATCH_SIZE = 10
MAX_TOKENS = 8000
MODEL_LABEL = "pricing-classify"

SYSTEM = (
    "You classify job listings from AI-data-industry job boards into a fixed "
    "taxonomy of work types. Answer with JSON only. Never invent taxonomy keys."
)

CONTRACT = """
Return ONLY this JSON shape, one entry per listing, i matching the input index:
{"results": [{"i": 0, "market_type": "<taxonomy key>", "confidence": 0.0,
  "country_code": "<ISO2 if the listing text names a country the location
  field does not, else empty>", "language": "<required human language if any>",
  "expertise_level": "<entry|expert|empty>"}]}
Use "other" when genuinely unsure. confidence is your honest probability.
"""


def build_prompt(listings: list[dict], taxonomy_lines: str | None = None) -> str:
    tax_lines = taxonomy_lines if taxonomy_lines is not None else _default_taxonomy_lines()
    items = [
        {"i": i, "title": l["title"], "platform": l["platform"],
         "location": l.get("location") or "",
         "excerpt": (l.get("description_excerpt") or "")[:280]}
        for i, l in enumerate(listings)
    ]
    return (
        "Taxonomy (key: what it covers):\n" + tax_lines
        + "\n\nListings:\n" + json.dumps(items, ensure_ascii=False)
        + "\n" + CONTRACT
    )


def taxonomy_lines_from_rows(rows) -> str:
    """The DB's ``market_taxonomy`` table is the source of truth for prompt
    text. ``classify_new`` always builds the prompt from these live rows."""
    return "\n".join(f"{r['key']}: {r['description']}" for r in rows)


def _default_taxonomy_lines() -> str:
    """Fallback only for callers with no database at hand (e.g. the DB-less
    golden test). Never used by ``classify_new`` — that always passes the
    live ``market_taxonomy`` rows explicitly."""
    from ultimatescrape.pricing.ddl import TAXONOMY_SEED
    return "\n".join(f"{k}: {d}" for k, _, d in TAXONOMY_SEED)


def parse_response(data: dict, n: int, valid_keys: set[str]) -> list[dict]:
    by_i: dict[int, dict] = {}
    for entry in (data or {}).get("results", []):
        if isinstance(entry, dict) and isinstance(entry.get("i"), int):
            by_i[entry["i"]] = entry
    out = []
    for i in range(n):
        e = by_i.get(i, {})
        mt = e.get("market_type", "")
        conf = e.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else 0.0
        if mt not in valid_keys:
            mt, conf = "other", min(conf, 0.3)
        out.append({
            "market_type": mt,
            "confidence": max(0.0, min(1.0, conf)),
            "attrs": {
                "country_code": str(e.get("country_code") or "")[:2].upper(),
                "language": str(e.get("language") or "")[:32],
                "expertise_level": str(e.get("expertise_level") or "")[:16],
            },
        })
    return out


async def classify_new(
    pool: asyncpg.Pool, *, limit: int | None = None, llm=None
) -> dict:
    taxonomy_rows = await pool.fetch(
        "SELECT key, label, description FROM market_taxonomy"
    )
    valid = {r["key"] for r in taxonomy_rows}
    tax_lines = taxonomy_lines_from_rows(taxonomy_rows)
    rows = await pool.fetch(
        """
        SELECT l.id, l.title, l.platform, l.location_raw AS location,
               l.description_excerpt
        FROM competitor_listing l
        LEFT JOIN competitor_listing_class c
          ON c.listing_id = l.id AND c.taxonomy_version = $1
        WHERE c.listing_id IS NULL
        ORDER BY l.id
        LIMIT $2
        """,
        TAXONOMY_VERSION, limit or 100_000,
    )
    listings = [dict(r) for r in rows]
    summary = {"classified": 0, "unclassifiable": 0, "batches": 0}
    if not listings:
        return summary

    if llm is None:
        from ultimatescrape.llm.client import KimiClient
        llm_cm = KimiClient()
    else:
        llm_cm = llm

    now = datetime.now(timezone.utc)
    async with llm_cm as client:
        for start in range(0, len(listings), BATCH_SIZE):
            batch = listings[start:start + BATCH_SIZE]
            data, _ = await client.complete_json(
                build_prompt(batch, taxonomy_lines=tax_lines), system=SYSTEM,
                max_tokens=MAX_TOKENS, temperature=0.0, label=MODEL_LABEL,
            )
            results = parse_response(data if isinstance(data, dict) else {},
                                     n=len(batch), valid_keys=valid)
            for l, res in zip(batch, results):
                await pool.execute(
                    """
                    INSERT INTO competitor_listing_class
                      (listing_id, taxonomy_version, market_type, confidence,
                       attrs, model, classified_at)
                    VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)
                    ON CONFLICT (listing_id, taxonomy_version) DO NOTHING
                    """,
                    l["id"], TAXONOMY_VERSION, res["market_type"],
                    res["confidence"], json.dumps(res["attrs"]),
                    MODEL_LABEL, now,
                )
                if res["market_type"] == "other" and res["confidence"] <= 0.3:
                    summary["unclassifiable"] += 1
                else:
                    summary["classified"] += 1
            summary["batches"] += 1
    return summary
