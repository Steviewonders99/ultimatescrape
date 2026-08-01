"""Run directory: one file per work unit, so a crash costs one unit, not a run.

The vendor swarm's ``ONLY_FAILED=1`` resume is the pattern here, generalised. Each
unit's result lands on disk the moment it completes; resuming re-reads the
directory and only re-dispatches units that are missing or errored. That turns a
three-hour 200-agent job from all-or-nothing into something you can interrupt.

Layout::

    runs/<run_id>/
      manifest.json      config, spec, status, ledger snapshot
      units/<key>.json   one completed work unit
      pages/<sha1>.json  fetched page records
      report.md          rendered report
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import settings

_SAFE = re.compile(r"[^a-z0-9._-]+")


def safe_key(value: str, *, max_len: int = 80) -> str:
    """Filesystem-safe, stable, collision-resistant key.

    A hash suffix is always appended: slugging alone collides (``United States``
    and ``united-states`` become the same file) and silently overwrites results.
    """
    slug = _SAFE.sub("-", value.strip().lower()).strip("-") or "unit"
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:max_len]}-{digest}"


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunStore:
    run_id: str
    root: Path

    @classmethod
    def create(cls, name: str, *, root: Path | None = None) -> RunStore:
        base = root or settings.runs_dir
        run_id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{safe_key(name, max_len=40)}"
        store = cls(run_id=run_id, root=base / run_id)
        (store.root / "units").mkdir(parents=True, exist_ok=True)
        (store.root / "pages").mkdir(parents=True, exist_ok=True)
        return store

    @classmethod
    def open(cls, run_id: str, *, root: Path | None = None) -> RunStore:
        base = root or settings.runs_dir
        path = base / run_id
        if not path.is_dir():
            raise FileNotFoundError(f"no such run: {path}")
        return cls(run_id=run_id, root=path)

    @classmethod
    def latest(cls, *, root: Path | None = None) -> RunStore | None:
        base = root or settings.runs_dir
        if not base.is_dir():
            return None
        # A directory only counts as a run if it holds a manifest. Sibling export
        # folders (jobboards, ga4, graph) sort after the timestamped run ids and
        # would otherwise be picked as "latest", producing an empty ingest that
        # looks like a run with no findings.
        runs = sorted(
            (p for p in base.iterdir() if p.is_dir() and (p / "manifest.json").is_file()),
            key=lambda p: p.name,
        )
        return cls(run_id=runs[-1].name, root=runs[-1]) if runs else None

    # ── manifest ──────────────────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def write_manifest(self, **fields: Any) -> None:
        current = self.read_manifest()
        current.update(fields)
        current.setdefault("run_id", self.run_id)
        current.setdefault("created_at", utcnow())
        current["updated_at"] = utcnow()
        self._atomic_write(self.manifest_path, current)

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text())
        except json.JSONDecodeError:
            return {}

    # ── units ─────────────────────────────────────────────────────────────────

    def unit_path(self, key: str) -> Path:
        return self.root / "units" / f"{safe_key(key)}.json"

    def has_unit(self, key: str) -> bool:
        """True only for a *successful* unit — an errored file is work to redo."""
        path = self.unit_path(key)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return False
        return not data.get("error")

    def save_unit(self, key: str, payload: dict) -> Path:
        payload = {**payload, "_key": key, "_saved_at": utcnow()}
        path = self.unit_path(key)
        self._atomic_write(path, payload)
        return path

    def load_unit(self, key: str) -> dict | None:
        path = self.unit_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def load_units(self) -> list[dict]:
        out: list[dict] = []
        for path in sorted((self.root / "units").glob("*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
        return out

    def pending(self, keys: Iterable[str]) -> list[str]:
        return [k for k in keys if not self.has_unit(k)]

    # ── pages ─────────────────────────────────────────────────────────────────

    def save_page(self, url: str, payload: dict) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        path = self.root / "pages" / f"{digest}.json"
        self._atomic_write(path, {**payload, "url": url, "_saved_at": utcnow()})
        return path

    def load_pages(self) -> list[dict]:
        out: list[dict] = []
        for path in sorted((self.root / "pages").glob("*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
        return out

    # ── report ────────────────────────────────────────────────────────────────

    def write_report(self, markdown: str, name: str = "report.md") -> Path:
        path = self.root / name
        path.write_text(markdown, encoding="utf-8")
        return path

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        """Write via a temp file + rename so a kill mid-write cannot leave a
        half-JSON file that fails to parse on resume."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{time.time_ns()}.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        tmp.replace(path)
