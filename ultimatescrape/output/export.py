"""Turning results into artifacts on disk.

Every producer in the system (swarm runs, job-board sweeps, GA4 reports, graph
queries) converts to a :class:`Dataset` and goes through one export path, so the
configured formats apply uniformly and filenames follow one convention.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..projconfig import OutputConfig, project
from .formats import Artifact, Dataset, render, slugify, write_index

log = logging.getLogger("uscrape.output")


def dataset_from_swarm(result: Any) -> Dataset:
    """Convert a SwarmResult into the common row model.

    Findings are grouped by verdict so formats that support sections (markdown
    headings, xlsx sheets) surface verified and refuted separately, while flat
    formats still get every row.
    """
    cfg = project.output
    findings = list(result.findings)

    sections: dict[str, list[dict]] = {}
    verified = [f for f in findings if f.get("_verdict") == "supported"]
    refuted = [f for f in findings if f.get("_verdict") == "refuted"]
    unverified = [f for f in findings if f.get("_verdict") not in ("supported", "refuted")]

    if verified:
        sections["Verified findings"] = verified
    if unverified and cfg.include_unverified:
        sections["Unverified findings"] = unverified
    if refuted and cfg.include_refuted:
        sections["Refuted findings"] = refuted
    if not sections:
        sections["Findings"] = findings

    stats = dict(result.stats)
    ledger = dict(result.ledger)
    summary = {
        "targets": stats.get("targets"),
        "dimensions": stats.get("dimensions"),
        "agents_run": stats.get("units_total"),
        "agents_failed": stats.get("units_failed"),
        "findings_unique": stats.get("findings_unique"),
        "verified": stats.get("findings_verified"),
        "refuted": stats.get("findings_refuted"),
        "dead_urls": stats.get("urls_dead"),
        "elapsed_s": stats.get("elapsed_s"),
        "cost_usd": f"${ledger.get('cost_usd', 0):.4f}",
    }

    rows = findings
    if cfg.include_gaps:
        gaps = [
            {
                "name": f"[gap] {(u.get('_meta') or {}).get('target', '?')}",
                "summary": u.get("gaps"),
                "_agent": f"{(u.get('_meta') or {}).get('target')}::"
                f"{(u.get('_meta') or {}).get('dimension')}",
            }
            for u in result.units
            if u.get("gaps")
        ]
        if gaps:
            sections["Coverage gaps"] = gaps

    return Dataset(
        title=result.spec.get("topic", result.run_id),
        rows=rows,
        sections=sections,
        summary={k: v for k, v in summary.items() if v is not None},
        narrative=result.synthesis or "",
        meta={
            "run_id": result.run_id,
            "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "kind": "swarm",
        },
    )


def dataset_from_rows(
    title: str,
    rows: Sequence[dict],
    *,
    summary: dict | None = None,
    narrative: str = "",
    columns: Sequence[str] | None = None,
    kind: str = "rows",
    meta: dict | None = None,
) -> Dataset:
    return Dataset(
        title=title,
        rows=list(rows),
        summary=dict(summary or {}),
        narrative=narrative,
        columns=list(columns) if columns else None,
        meta={
            "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "kind": kind,
            **(meta or {}),
        },
    )


def export(
    data: Dataset,
    *,
    directory: Path | str | None = None,
    formats: Sequence[str] | None = None,
    basename: str | None = None,
    cfg: OutputConfig | None = None,
) -> list[Path]:
    """Render and write. Returns the paths actually created."""
    cfg = cfg or project.output
    target = Path(directory) if directory else Path(cfg.directory)
    stem = basename or _basename(data, cfg)

    artifacts: list[Artifact] = render(data, formats, cfg=cfg, basename=stem)
    paths = [a.write(target) for a in artifacts]

    if cfg.write_index and artifacts:
        paths.append(write_index(artifacts, data, target))
    if errs := data.meta.get("render_errors"):
        for err in errs:
            log.warning("[output] format skipped — %s", err)
    return paths


def _basename(data: Dataset, cfg: OutputConfig) -> str:
    fields = {
        "run_id": data.meta.get("run_id", ""),
        "topic": slugify(data.title),
        "date": datetime.now(UTC).strftime("%Y%m%d"),
        "kind": data.meta.get("kind", "output"),
    }
    try:
        name = cfg.filename_template.format(**fields)
    except KeyError as exc:
        log.warning(
            "[output] filename_template references unknown field %s; falling back to topic", exc
        )
        name = fields["topic"]
    return slugify(name) or fields["topic"]
