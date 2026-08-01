"""Cross-agent merge: dedupe, field back-fill, and majority voting.

Two independent agents finding the same entity is a signal, not a duplicate to
discard — it raises confidence. So the merge keeps a corroboration count and
back-fills missing fields from the later sighting rather than dropping it, which
is exactly what the v6 vendor re-swarm learned to do after its first pass
overwrote good records with thinner ones.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlparse

_NORM = re.compile(r"[^a-z0-9]+")


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return _NORM.sub("", str(value).lower()).strip()


def normalize_url(url: str) -> str:
    """Domain + path, minus scheme, www, query, trailing slash.

    Without this, ``https://example.com/x``, ``http://www.example.com/x/`` and
    ``https://example.com/x?utm_source=…`` are three distinct entities.
    """
    try:
        parsed = urlparse(url if "//" in url else f"//{url}", scheme="https")
    except ValueError:
        return normalize(url)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    return f"{host}{path}"


def dedupe_key(item: dict, fields: Sequence[str]) -> str:
    for field in fields:
        value = item.get(field)
        if value:
            return (
                normalize_url(str(value))
                if field in ("url", "website", "linkedin", "source_url")
                else normalize(value)
            )
    return normalize(str(sorted(item.items()))[:200])


def merge_findings(
    findings: Iterable[dict],
    *,
    dedupe_fields: Sequence[str],
    prefer: Sequence[str] = ("confidence",),
) -> list[dict]:
    """Collapse findings to one record per entity, richest version winning."""
    order = {"high": 3, "medium": 2, "low": 1}

    def rank(item: dict) -> tuple[int, int]:
        conf = order.get(str(item.get(prefer[0], "")).lower(), 0) if prefer else 0
        filled = sum(1 for v in item.values() if v not in (None, "", [], {}))
        return conf, filled

    merged: dict[str, dict] = {}
    for item in findings:
        if not isinstance(item, dict):
            continue
        key = dedupe_key(item, dedupe_fields)
        if not key:
            continue
        if key not in merged:
            record = dict(item)
            record["_corroborations"] = 1
            record["_sources"] = [item.get("_agent")] if item.get("_agent") else []
            merged[key] = record
            continue

        existing = merged[key]
        existing["_corroborations"] = existing.get("_corroborations", 1) + 1
        if item.get("_agent") and item["_agent"] not in existing.get("_sources", []):
            existing.setdefault("_sources", []).append(item["_agent"])

        winner, loser = (
            (item, existing) if rank(item) > rank(existing) else (existing, item)
        )
        for field, value in loser.items():
            if field.startswith("_"):
                continue
            if value not in (None, "", [], {}) and winner.get(field) in (None, "", [], {}):
                winner[field] = value
        if winner is item:
            winner["_corroborations"] = existing["_corroborations"]
            winner["_sources"] = existing.get("_sources", [])
            merged[key] = winner

    return sorted(
        merged.values(),
        key=lambda r: (-r.get("_corroborations", 1), -rank(r)[0]),
    )


def majority_vote(votes: Sequence[Any], *, min_agreement: int = 2) -> tuple[Any, int]:
    """Most common value among votes, with its count.

    Used where several agents independently answer the same closed question. The
    census swarm's refinement is worth remembering: when a deterministic answer
    can be computed in code, add it as an extra voter, because it breaks ties in
    favour of arithmetic rather than in favour of whichever model spoke twice.
    """
    import json

    if not votes:
        return None, 0
    encoded = [json.dumps(v, sort_keys=True, default=str) for v in votes if v is not None]
    if not encoded:
        return None, 0
    counts = Counter(encoded)
    best, count = counts.most_common(1)[0]
    if count < min_agreement:
        return json.loads(best), count
    return json.loads(best), count
