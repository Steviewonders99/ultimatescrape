"""Pure lifecycle diff: DB state + fetched listings -> a ChangeSet.

No IO. This is the logic that must never mass-delist on a failed fetch:
delisting requires the platform to be in succeeded_platforms.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from ultimatescrape.jobboards.fetchers import Listing


@dataclass(frozen=True)
class ExistingRow:
    id: int
    content_hash: str
    delisted: bool
    pay_key: tuple


@dataclass
class ChangeSet:
    inserts: list[Listing] = field(default_factory=list)
    updates: list[tuple[int, Listing]] = field(default_factory=list)
    touches: list[int] = field(default_factory=list)
    relists: list[tuple[int, Listing]] = field(default_factory=list)
    delists: list[int] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)


def pay_key(l: Listing) -> tuple:
    return (l.pay_min, l.pay_max, l.pay_currency, l.pay_unit)


def listing_content_hash(l: Listing) -> str:
    raw = json.dumps(
        [l.title, l.description_full, l.location, l.pay_raw,
         l.pay_min, l.pay_max, l.pay_currency, l.pay_unit],
        sort_keys=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _pay_json(l: Listing) -> dict:
    return {"pay_min": l.pay_min, "pay_max": l.pay_max,
            "pay_currency": l.pay_currency, "pay_unit": l.pay_unit,
            "pay_raw": l.pay_raw}


def plan_changes(
    existing: dict[tuple[str, str], ExistingRow],
    fetched: list[Listing],
    succeeded_platforms: set[str],
    now: datetime,
) -> ChangeSet:
    cs = ChangeSet()
    seen: set[tuple[str, str]] = set()

    for l in fetched:
        key = (l.platform, l.external_id)
        seen.add(key)
        row = existing.get(key)
        if row is None:
            cs.inserts.append(l)
            continue
        if row.delisted:
            cs.relists.append((row.id, l))
            cs.history.append({
                "listing_id": row.id, "observed_at": now,
                "change_type": "relisted", "old_pay": None,
                "new_pay": _pay_json(l),
                "content_hash": listing_content_hash(l),
            })
            continue
        new_hash = listing_content_hash(l)
        if new_hash == row.content_hash:
            cs.touches.append(row.id)
            continue
        cs.updates.append((row.id, l))
        change = "pay_change" if pay_key(l) != row.pay_key else "jd_change"
        cs.history.append({
            "listing_id": row.id, "observed_at": now, "change_type": change,
            "old_pay": None if change == "jd_change" else {"pay_key": list(row.pay_key)},
            "new_pay": _pay_json(l), "content_hash": new_hash,
        })

    for key, row in existing.items():
        platform = key[0]
        if key in seen or row.delisted or platform not in succeeded_platforms:
            continue
        cs.delists.append(row.id)
        cs.history.append({
            "listing_id": row.id, "observed_at": now, "change_type": "delisted",
            "old_pay": {"pay_key": list(row.pay_key)}, "new_pay": None,
            "content_hash": row.content_hash,
        })
    return cs
