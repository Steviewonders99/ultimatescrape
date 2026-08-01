"""Project configuration from ``uscrape.toml``.

Separate from ``config.py`` on purpose. ``config.py`` holds secrets and machine
tuning that belong in the environment; this holds the choices a repo owner makes
once and commits — which artifacts to produce, where to publish them, whether the
knowledge graph is on.

Precedence is environment > uscrape.toml > built-in default, so CI and one-off
overrides never require editing a committed file.

Lookup walks up from the current directory to find ``uscrape.toml``, so the CLI
behaves the same from any subdirectory of the project.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_NAME = "uscrape.toml"

VALID_FORMATS = ("markdown", "json", "jsonl", "csv", "xlsx", "html", "mermaid")


def find_config(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        path = candidate / CONFIG_NAME
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=1)
def _raw() -> dict:
    override = os.getenv("USCRAPE_CONFIG")
    path = Path(override).expanduser() if override else find_config()
    if not path or not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        data["_path"] = str(path)
        return data
    except (tomllib.TOMLDecodeError, OSError) as exc:
        # A broken config must be loud. Silently falling back to defaults would
        # produce a run that quietly ignores everything the user configured.
        raise ValueError(f"could not read {path}: {exc}") from exc


def _section(name: str) -> dict:
    node: Any = _raw()
    for part in name.split("."):
        node = (node or {}).get(part) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _get(section: str, key: str, default: Any, env: str | None = None) -> Any:
    if env and (raw := os.getenv(env, "").strip()):
        if isinstance(default, bool):
            return raw.lower() not in ("false", "0", "no", "off")
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(raw)
            except ValueError:
                return default
        if isinstance(default, float):
            try:
                return float(raw)
            except ValueError:
                return default
        if isinstance(default, (list, tuple)):
            return [x.strip() for x in raw.split(",") if x.strip()]
        return raw
    value = _section(section).get(key)
    return default if value is None else value


@dataclass(frozen=True)
class OutputConfig:
    formats: list[str] = field(
        default_factory=lambda: _normalize_formats(
            _get("output", "formats", ["markdown", "json"], "USCRAPE_OUTPUT_FORMATS")
        )
    )
    directory: str = field(
        default_factory=lambda: _get("output", "directory", "runs", "USCRAPE_OUTPUT_DIR")
    )
    filename_template: str = field(
        default_factory=lambda: _get("output", "filename_template", "{run_id}")
    )
    write_index: bool = field(default_factory=lambda: _get("output", "write_index", True))

    # markdown
    include_refuted: bool = field(
        default_factory=lambda: _get("output.markdown", "include_refuted", True)
    )
    include_unverified: bool = field(
        default_factory=lambda: _get("output.markdown", "include_unverified", True)
    )
    include_gaps: bool = field(
        default_factory=lambda: _get("output.markdown", "include_gaps", True)
    )
    include_method: bool = field(
        default_factory=lambda: _get("output.markdown", "include_method", True)
    )
    max_rows: int = field(
        default_factory=lambda: _get("output.markdown", "max_rows_per_section", 60)
    )

    # csv
    delimiter: str = field(default_factory=lambda: _get("output.csv", "delimiter", ","))
    include_provenance: bool = field(
        default_factory=lambda: _get("output.csv", "include_provenance", True)
    )

    # xlsx
    freeze_header: bool = field(default_factory=lambda: _get("output.xlsx", "freeze_header", True))
    autofilter: bool = field(default_factory=lambda: _get("output.xlsx", "autofilter", True))
    split_by_verdict: bool = field(
        default_factory=lambda: _get("output.xlsx", "split_by_verdict", True)
    )

    # html
    standalone: bool = field(default_factory=lambda: _get("output.html", "standalone", True))
    theme: str = field(default_factory=lambda: _get("output.html", "theme", "auto"))


@dataclass(frozen=True)
class GraphConfig:
    enabled: bool = field(default_factory=lambda: _get("graph", "enabled", True, "USCRAPE_GRAPH"))
    path: str = field(
        default_factory=lambda: _get("graph", "path", "knowledge/graph.db", "USCRAPE_GRAPH_PATH")
    )
    auto_ingest: bool = field(default_factory=lambda: _get("graph", "auto_ingest", True))
    llm_extraction: bool = field(default_factory=lambda: _get("graph", "llm_extraction", True))
    merge_threshold: float = field(
        default_factory=lambda: float(_get("graph", "merge_threshold", 0.88))
    )


@dataclass(frozen=True)
class SharePointConfig:
    enabled: bool = field(
        default_factory=lambda: _get("sharepoint", "enabled", False, "USCRAPE_SHAREPOINT")
    )
    site_host: str = field(
        default_factory=lambda: _get("sharepoint", "site_host", "", "SHAREPOINT_SITE_HOST")
    )
    site_path: str = field(
        default_factory=lambda: _get("sharepoint", "site_path", "", "SHAREPOINT_SITE_PATH")
    )
    folder: str = field(
        default_factory=lambda: _get("sharepoint", "folder", "", "SHAREPOINT_FOLDER")
    )
    upload_formats: list[str] = field(
        default_factory=lambda: _normalize_formats(
            _get("sharepoint", "upload_formats", ["markdown"], "SHAREPOINT_UPLOAD_FORMATS")
        )
    )
    conflict: str = field(default_factory=lambda: _get("sharepoint", "conflict", "rename"))


@dataclass(frozen=True)
class GA4Config:
    enabled: bool = field(default_factory=lambda: _get("ga4", "enabled", True))
    default_property: str = field(
        default_factory=lambda: str(
            _get("ga4", "default_property", "", "GA4_PROPERTY_ID") or ""
        )
    )
    default_lookback_days: int = field(
        default_factory=lambda: int(_get("ga4", "default_lookback_days", 28))
    )


@dataclass(frozen=True)
class SwarmDefaults:
    verifier_votes: int = field(default_factory=lambda: int(_get("swarm", "verifier_votes", 3)))
    validate_urls: bool = field(default_factory=lambda: _get("swarm", "validate_urls", True))
    synthesize: bool = field(default_factory=lambda: _get("swarm", "synthesize", True))


@dataclass(frozen=True)
class JobBoardConfig:
    watch: list[str] = field(
        default_factory=lambda: list(_get("jobboards", "watch", [], "USCRAPE_JOBBOARDS"))
    )
    include_worker_platforms: bool = field(
        default_factory=lambda: _get("jobboards", "include_worker_platforms", True)
    )


def _normalize_formats(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [v.strip() for v in values.split(",")]
    out: list[str] = []
    for value in values or []:
        name = str(value).strip().lower()
        if name in VALID_FORMATS and name not in out:
            out.append(name)
        elif name and name not in VALID_FORMATS:
            raise ValueError(
                f"unknown output format {name!r} in {CONFIG_NAME}; "
                f"valid formats: {', '.join(VALID_FORMATS)}"
            )
    return out or ["markdown"]


@dataclass(frozen=True)
class ProjectConfig:
    output: OutputConfig = field(default_factory=OutputConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    sharepoint: SharePointConfig = field(default_factory=SharePointConfig)
    ga4: GA4Config = field(default_factory=GA4Config)
    swarm: SwarmDefaults = field(default_factory=SwarmDefaults)
    jobboards: JobBoardConfig = field(default_factory=JobBoardConfig)

    @property
    def source_path(self) -> str | None:
        return _raw().get("_path")


def load() -> ProjectConfig:
    return ProjectConfig()


def reload() -> ProjectConfig:
    """Drop the cached file so tests and long-lived processes see edits."""
    _raw.cache_clear()
    return ProjectConfig()


project = load()
