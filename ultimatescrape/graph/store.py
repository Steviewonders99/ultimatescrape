"""Local knowledge graph over everything the system captures.

SQLite, single file, no server. The point is accumulation: the second question
about a company should be cheaper than the first, because the entities, the
relations, and the sources that answered it are already here. A swarm without
this forgets everything the moment a run ends.

Schema, deliberately small:

    entities     one row per real-world thing, with a canonical key
    aliases      the surface forms that resolve to an entity
    relations    typed, directed, weighted edges with provenance
    observations every time something was seen, and where — never overwritten
    sources      pages and APIs consulted, with content hashes

The design rule that matters: **observations are append-only and relations carry
their provenance**. A graph that stores only the current best answer cannot tell
you why it believes something, and cannot be corrected when a source turns out to
be wrong. Every edge here can be traced back to the run, agent, and URL that
produced it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..projconfig import project

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,   -- normalised identity
    name        TEXT NOT NULL,          -- best display name seen
    type        TEXT NOT NULL DEFAULT 'thing',
    attrs       TEXT NOT NULL DEFAULT '{}',
    mentions    INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS aliases (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);

CREATE TABLE IF NOT EXISTS relations (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate  TEXT NOT NULL,
    attrs      TEXT NOT NULL DEFAULT '{}',
    weight     REAL NOT NULL DEFAULT 1.0,
    confidence TEXT NOT NULL DEFAULT 'medium',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE (source_id, target_id, predicate)
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS idx_rel_pred ON relations(predicate);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    relation_id INTEGER REFERENCES relations(id) ON DELETE CASCADE,
    run_id      TEXT,
    agent       TEXT,
    source_url  TEXT,
    snippet     TEXT,
    verdict     TEXT,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);

CREATE TABLE IF NOT EXISTS sources (
    url          TEXT PRIMARY KEY,
    title        TEXT,
    run_id       TEXT,
    content_hash TEXT,
    status       TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS entity_fts USING fts5(
    name, aliases, attrs, content=''
);
"""

_STOPWORDS = {
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "corp", "corp.", "corporation",
    "co", "co.", "company", "gmbh", "sa", "sas", "bv", "nv", "plc", "ag", "ab",
    "oy", "as", "pty", "the", "group", "holdings", "technologies", "technology",
}


def canonical_key(name: str, kind: str = "thing") -> str:
    """Normalised identity for an entity.

    Strips accents, punctuation, and corporate suffixes so "Scale AI, Inc." and
    "scale ai" collapse to one node. Type is part of the key because a *company*
    called Handshake and a *product* called Handshake are different things and
    merging them silently corrupts every downstream count.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t and t not in _STOPWORDS]
    slug = "-".join(tokens) or re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "unknown"
    return f"{kind}:{slug}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Entity:
    key: str
    name: str
    type: str = "thing"
    attrs: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    mentions: int = 0


@dataclass
class Relation:
    source: str
    predicate: str
    target: str
    attrs: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    confidence: str = "medium"


class KnowledgeGraph:
    """Append-only graph store. Safe to open concurrently (WAL mode)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or project.graph.path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def __enter__(self) -> KnowledgeGraph:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with closing(self._conn):
            pass

    # ── writes ────────────────────────────────────────────────────────────────

    def upsert_entity(
        self,
        name: str,
        kind: str = "thing",
        attrs: dict | None = None,
        *,
        aliases: Iterable[str] = (),
    ) -> int:
        """Insert or merge an entity, returning its id.

        Attributes merge rather than replace, and an existing non-empty value is
        never overwritten by a new empty one — a later, thinner sighting must not
        erase what an earlier, richer one established.
        """
        key = canonical_key(name, kind)
        now = _now()
        cur = self._conn.execute("SELECT id, name, attrs, mentions FROM entities WHERE key=?", (key,))
        row = cur.fetchone()

        if row is None:
            cur = self._conn.execute(
                "INSERT INTO entities(key, name, type, attrs, mentions, first_seen, last_seen) "
                "VALUES(?,?,?,?,1,?,?)",
                (key, name.strip(), kind, json.dumps(attrs or {}, default=str), now, now),
            )
            entity_id = int(cur.lastrowid)
        else:
            entity_id = int(row["id"])
            merged = json.loads(row["attrs"] or "{}")
            for field_name, value in (attrs or {}).items():
                if value in (None, "", [], {}):
                    continue
                if merged.get(field_name) in (None, "", [], {}):
                    merged[field_name] = value
            # Prefer the longer display name; acronyms lose to full names.
            best_name = row["name"] if len(row["name"]) >= len(name.strip()) else name.strip()
            self._conn.execute(
                "UPDATE entities SET attrs=?, name=?, mentions=mentions+1, last_seen=? WHERE id=?",
                (json.dumps(merged, default=str), best_name, now, entity_id),
            )

        for alias in {name.strip(), *aliases}:
            if alias:
                self._conn.execute(
                    "INSERT OR IGNORE INTO aliases(entity_id, alias) VALUES(?,?)",
                    (entity_id, alias.strip()),
                )
        self._reindex(entity_id)
        return entity_id

    def add_relation(
        self,
        source: str,
        predicate: str,
        target: str,
        *,
        source_kind: str = "thing",
        target_kind: str = "thing",
        attrs: dict | None = None,
        confidence: str = "medium",
        run_id: str | None = None,
        agent: str | None = None,
        source_url: str | None = None,
        snippet: str | None = None,
    ) -> int:
        """Add or reinforce an edge.

        Re-observing an edge increments its weight rather than duplicating it, so
        weight reads as "how many independent times has this been seen" — the
        graph equivalent of the swarm's corroboration count.
        """
        src = self.upsert_entity(source, source_kind)
        dst = self.upsert_entity(target, target_kind)
        now = _now()
        pred = re.sub(r"\s+", "_", predicate.strip().lower())[:60] or "related_to"

        cur = self._conn.execute(
            "SELECT id, weight, attrs FROM relations WHERE source_id=? AND target_id=? AND predicate=?",
            (src, dst, pred),
        )
        row = cur.fetchone()
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO relations(source_id,target_id,predicate,attrs,weight,confidence,"
                "first_seen,last_seen) VALUES(?,?,?,?,1.0,?,?,?)",
                (src, dst, pred, json.dumps(attrs or {}, default=str), confidence, now, now),
            )
            relation_id = int(cur.lastrowid)
        else:
            relation_id = int(row["id"])
            merged = {**json.loads(row["attrs"] or "{}"), **{k: v for k, v in (attrs or {}).items() if v}}
            self._conn.execute(
                "UPDATE relations SET weight=weight+1, attrs=?, confidence=?, last_seen=? WHERE id=?",
                (json.dumps(merged, default=str), confidence, now, relation_id),
            )

        self._conn.execute(
            "INSERT INTO observations(relation_id, entity_id, run_id, agent, source_url, snippet, "
            "verdict, captured_at) VALUES(?,?,?,?,?,?,?,?)",
            (relation_id, src, run_id, agent, source_url, (snippet or "")[:800], confidence, now),
        )
        return relation_id

    def record_source(
        self,
        url: str,
        *,
        title: str | None = None,
        run_id: str | None = None,
        content_hash: str | None = None,
        status: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO sources(url,title,run_id,content_hash,status,fetched_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET title=COALESCE(excluded.title,title), "
            "run_id=excluded.run_id, content_hash=excluded.content_hash, "
            "status=excluded.status, fetched_at=excluded.fetched_at",
            (url, title, run_id, content_hash, status, _now()),
        )

    def _reindex(self, entity_id: int) -> None:
        row = self._conn.execute(
            "SELECT e.name, e.attrs, COALESCE(GROUP_CONCAT(a.alias,' '),'') AS al "
            "FROM entities e LEFT JOIN aliases a ON a.entity_id=e.id WHERE e.id=? GROUP BY e.id",
            (entity_id,),
        ).fetchone()
        if row:
            self._conn.execute(
                "INSERT INTO entity_fts(rowid, name, aliases, attrs) VALUES(?,?,?,?)",
                (entity_id, row["name"], row["al"], row["attrs"]),
            )

    # ── reads ─────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 25) -> list[dict]:
        """Full-text entity search, falling back to LIKE when FTS syntax fails."""
        try:
            rows = self._conn.execute(
                "SELECT e.* FROM entity_fts f JOIN entities e ON e.id=f.rowid "
                "WHERE entity_fts MATCH ? ORDER BY e.mentions DESC LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            rows = self._conn.execute(
                "SELECT DISTINCT e.* FROM entities e LEFT JOIN aliases a ON a.entity_id=e.id "
                "WHERE e.name LIKE ? OR a.alias LIKE ? ORDER BY e.mentions DESC LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [self._entity_row(r) for r in rows]

    def get(self, name: str, kind: str = "thing") -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM entities WHERE key=?", (canonical_key(name, kind),)
        ).fetchone()
        return self._entity_row(row) if row else None

    def neighbors(
        self, name: str, kind: str = "thing", *, depth: int = 1, limit: int = 200
    ) -> list[dict]:
        """Edges within ``depth`` hops. Breadth-first, deduped by edge."""
        start = self._conn.execute(
            "SELECT id FROM entities WHERE key=?", (canonical_key(name, kind),)
        ).fetchone()
        if not start:
            return []

        frontier = {int(start["id"])}
        seen_nodes = set(frontier)
        edges: dict[int, dict] = {}
        for _ in range(max(1, depth)):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = self._conn.execute(
                f"""SELECT r.id, r.predicate, r.weight, r.confidence, r.attrs,
                           s.name AS source, s.type AS source_type, s.id AS sid,
                           t.name AS target, t.type AS target_type, t.id AS tid
                    FROM relations r
                    JOIN entities s ON s.id=r.source_id
                    JOIN entities t ON t.id=r.target_id
                    WHERE r.source_id IN ({placeholders}) OR r.target_id IN ({placeholders})
                    ORDER BY r.weight DESC LIMIT ?""",
                (*frontier, *frontier, limit),
            ).fetchall()
            next_frontier: set[int] = set()
            for row in rows:
                edges[int(row["id"])] = {
                    "source": row["source"],
                    "source_type": row["source_type"],
                    "relation": row["predicate"],
                    "target": row["target"],
                    "target_type": row["target_type"],
                    "weight": row["weight"],
                    "confidence": row["confidence"],
                    **json.loads(row["attrs"] or "{}"),
                }
                for node in (int(row["sid"]), int(row["tid"])):
                    if node not in seen_nodes:
                        seen_nodes.add(node)
                        next_frontier.add(node)
            frontier = next_frontier
        return list(edges.values())[:limit]

    def provenance(self, name: str, kind: str = "thing", limit: int = 50) -> list[dict]:
        """Where the claims about an entity came from. The answer to "says who?"."""
        row = self._conn.execute(
            "SELECT id FROM entities WHERE key=?", (canonical_key(name, kind),)
        ).fetchone()
        if not row:
            return []
        rows = self._conn.execute(
            "SELECT run_id, agent, source_url, snippet, verdict, captured_at FROM observations "
            "WHERE entity_id=? ORDER BY captured_at DESC LIMIT ?",
            (int(row["id"]), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def top_entities(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM entities"
        params: list[Any] = []
        if kind:
            sql += " WHERE type=?"
            params.append(kind)
        sql += " ORDER BY mentions DESC, last_seen DESC LIMIT ?"
        params.append(limit)
        return [self._entity_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def edges(self, limit: int = 1000, predicate: str | None = None) -> list[dict]:
        sql = """SELECT r.predicate, r.weight, r.confidence, s.name AS source,
                        s.type AS source_type, t.name AS target, t.type AS target_type
                 FROM relations r JOIN entities s ON s.id=r.source_id
                 JOIN entities t ON t.id=r.target_id"""
        params: list[Any] = []
        if predicate:
            sql += " WHERE r.predicate=?"
            params.append(predicate)
        sql += " ORDER BY r.weight DESC LIMIT ?"
        params.append(limit)
        return [
            {
                "source": r["source"],
                "relation": r["predicate"],
                "target": r["target"],
                "weight": r["weight"],
                "confidence": r["confidence"],
                "source_type": r["source_type"],
                "target_type": r["target_type"],
            }
            for r in self._conn.execute(sql, params).fetchall()
        ]

    def stats(self) -> dict:
        def scalar(sql: str) -> int:
            return int(self._conn.execute(sql).fetchone()[0] or 0)

        by_type = {
            r["type"]: r["n"]
            for r in self._conn.execute(
                "SELECT type, COUNT(*) AS n FROM entities GROUP BY type ORDER BY n DESC"
            ).fetchall()
        }
        by_pred = {
            r["predicate"]: r["n"]
            for r in self._conn.execute(
                "SELECT predicate, COUNT(*) AS n FROM relations GROUP BY predicate "
                "ORDER BY n DESC LIMIT 15"
            ).fetchall()
        }
        return {
            "path": str(self.path),
            "entities": scalar("SELECT COUNT(*) FROM entities"),
            "relations": scalar("SELECT COUNT(*) FROM relations"),
            "observations": scalar("SELECT COUNT(*) FROM observations"),
            "sources": scalar("SELECT COUNT(*) FROM sources"),
            "runs": scalar("SELECT COUNT(DISTINCT run_id) FROM observations WHERE run_id IS NOT NULL"),
            "entities_by_type": by_type,
            "top_predicates": by_pred,
        }

    @staticmethod
    def _entity_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["attrs"] = json.loads(data.get("attrs") or "{}")
        return data
