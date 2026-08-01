"""Populating the graph from what the system captures.

Two extraction passes, in this order for a reason:

1. **Deterministic.** Findings already carry structure — a name, a URL, a target,
   a dimension, a LinkedIn page, a country. Those become entities and edges with
   no model call and no chance of hallucination. This pass alone produces a
   useful graph.
2. **LLM (optional).** Only for the relations that are stated in prose and cannot
   be derived from fields — "acquired", "partners with", "competes with",
   "founded by". One cheap call per batch of findings.

Pass 1 is never skipped and pass 2 never overwrites it. That ordering means a
model failure degrades the graph's richness, never its correctness.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from ..projconfig import project
from .store import KnowledgeGraph

log = logging.getLogger("uscrape.graph")

#: Finding fields that name another entity, and the edge to draw.
_FIELD_EDGES: dict[str, tuple[str, str]] = {
    "company": ("belongs_to", "company"),
    "employer": ("employed_by", "company"),
    "country": ("located_in", "place"),
    "hq_city": ("headquartered_in", "place"),
    "location": ("located_in", "place"),
    "industry": ("operates_in", "industry"),
    "category": ("categorised_as", "category"),
    "sector": ("operates_in", "industry"),
    "platform": ("listed_on", "platform"),
    "source_name": ("reported_by", "source"),
}

_EXTRACT_SYSTEM = """You extract a knowledge graph from research findings.

Return only relations that the text ACTUALLY STATES. Do not infer, do not use
background knowledge, and do not connect two entities merely because they appear
in the same finding. A wrong edge is far more costly than a missing one, because
it becomes indistinguishable from a verified fact once stored.

Use concise, lowercase, snake_case predicates from this set where they fit:
  competes_with, partners_with, acquired, owns, subsidiary_of, founded_by,
  invested_in, customer_of, supplier_of, uses, integrates_with, hiring_for,
  operates_in, headquartered_in, employs, offers_product

Return STRICT JSON only:
{"entities":[{"name":"","type":"company|person|product|place|industry|platform|technology|concept"}],
 "relations":[{"source":"","predicate":"","target":"","evidence":"<the phrase that states it>"}]}"""


def ingest_swarm_result(
    result: Any,
    graph: KnowledgeGraph | None = None,
    *,
    llm: Any | None = None,
) -> dict:
    """Ingest a completed SwarmResult. Returns counts of what was written."""
    owns_graph = graph is None
    graph = graph or KnowledgeGraph()
    run_id = result.run_id
    topic = result.spec.get("topic", run_id)
    counts = {"entities": 0, "relations": 0, "sources": 0, "llm_relations": 0}

    try:
        topic_key = graph.upsert_entity(topic, "topic", {"run_id": run_id})
        counts["entities"] += 1

        for target in result.spec.get("targets", []):
            label = target.get("label") or target.get("key")
            if not label:
                continue
            graph.add_relation(
                topic, "researched", label,
                source_kind="topic", target_kind="subject",
                run_id=run_id, confidence="high",
            )
            counts["relations"] += 1

        for finding in result.findings:
            counts_delta = _ingest_finding(graph, finding, run_id=run_id, topic=topic)
            counts["entities"] += counts_delta[0]
            counts["relations"] += counts_delta[1]

        for page in _pages_of(result):
            graph.record_source(**page, run_id=run_id)
            counts["sources"] += 1

        del topic_key
    finally:
        if owns_graph and llm is None:
            graph.close()

    log.info(
        "[graph] run %s → %d entities, %d relations, %d sources",
        run_id, counts["entities"], counts["relations"], counts["sources"],
    )
    return counts


def _ingest_finding(graph: KnowledgeGraph, finding: dict, *, run_id: str, topic: str) -> tuple[int, int]:
    name = (finding.get("name") or finding.get("title") or "").strip()
    if not name:
        return 0, 0

    kind = _infer_kind(finding)
    url = finding.get("url") or finding.get("website") or ""
    attrs = {
        k: v
        for k, v in finding.items()
        if not k.startswith("_")
        and k not in ("name", "title")
        and v not in (None, "", [], {})
        and isinstance(v, (str, int, float, bool))
    }
    aliases = [a for a in (finding.get("aka"), finding.get("legal_name")) if a]
    graph.upsert_entity(name, kind, attrs, aliases=aliases)
    entities, relations = 1, 0

    verdict = finding.get("_verdict")
    confidence = {"supported": "high", "refuted": "low"}.get(verdict, finding.get("confidence", "medium"))
    agent = finding.get("_agent")

    # A refuted finding still belongs in the graph — with its verdict attached,
    # so "we checked this and it did not hold" is recorded rather than forgotten
    # and rediscovered next quarter.
    graph.add_relation(
        topic, "found", name,
        source_kind="topic", target_kind=kind,
        attrs={"verdict": verdict, "url": url} if verdict else {"url": url},
        confidence=confidence, run_id=run_id, agent=agent, source_url=url,
        snippet=finding.get("summary") or finding.get("evidence"),
    )
    relations += 1

    for field_name, (predicate, target_kind) in _FIELD_EDGES.items():
        value = finding.get(field_name)
        if isinstance(value, str) and value.strip() and len(value) < 120:
            graph.add_relation(
                name, predicate, value.strip(),
                source_kind=kind, target_kind=target_kind,
                confidence=confidence, run_id=run_id, agent=agent, source_url=url,
            )
            entities += 1
            relations += 1

    if url:
        domain = (urlparse(url).netloc or "").lower().removeprefix("www.")
        if domain:
            graph.add_relation(
                name, "documented_at", domain,
                source_kind=kind, target_kind="domain",
                attrs={"url": url, "status": finding.get("_url_status")},
                confidence=confidence, run_id=run_id, agent=agent, source_url=url,
            )
            entities += 1
            relations += 1
    return entities, relations


def _infer_kind(finding: dict) -> str:
    category = str(finding.get("category") or "").lower()
    if any(w in category for w in ("people", "person", "founder", "exec")):
        return "person"
    if any(w in category for w in ("product", "tool", "platform")):
        return "product"
    if finding.get("linkedin") or finding.get("website") or "vendor" in category:
        return "company"
    if any(k in finding for k in ("approx_size", "founded_year", "hq_city")):
        return "company"
    if finding.get("value") is not None and finding.get("period"):
        return "statistic"
    return "thing"


def _pages_of(result: Any) -> list[dict]:
    pages: list[dict] = []
    seen: set[str] = set()
    for finding in result.findings:
        for key in ("url", "website", "source_url", "linkedin"):
            url = finding.get(key)
            if isinstance(url, str) and url.startswith("http") and url not in seen:
                seen.add(url)
                pages.append(
                    {
                        "url": url,
                        "title": finding.get("name") or finding.get("title"),
                        "status": (finding.get("_url_checks") or {}).get(key)
                        or finding.get("_url_status"),
                    }
                )
    return pages


def ingest_pages(
    graph: KnowledgeGraph, pages: Sequence[Any], *, run_id: str | None = None
) -> int:
    """Record fetched pages as sources, with content hashes for change detection."""
    import hashlib

    count = 0
    for page in pages:
        url = getattr(page, "url", None) or (page.get("url") if isinstance(page, dict) else None)
        if not url:
            continue
        doc = getattr(page, "doc", None)
        text = getattr(doc, "markdown", "") if doc else (page.get("markdown", "") if isinstance(page, dict) else "")
        graph.record_source(
            url,
            title=getattr(doc, "title", None) if doc else None,
            run_id=run_id,
            content_hash=hashlib.sha256((text or "").encode()).hexdigest()[:16] if text else None,
            status="ok" if text else "empty",
        )
        count += 1
    return count


async def extract_relations_llm(
    graph: KnowledgeGraph,
    findings: Sequence[dict],
    llm: Any,
    *,
    run_id: str | None = None,
    batch_size: int = 20,
) -> int:
    """Pull stated relations out of finding prose. Optional, additive, best-effort.

    Failures are logged and swallowed: the deterministic pass has already built a
    correct graph, and losing the prose relations must not fail a run.
    """
    from ..llm.json_util import coerce_dict

    written = 0
    for start in range(0, len(findings), batch_size):
        batch = findings[start : start + batch_size]
        payload = json.dumps(
            [
                {
                    k: v
                    for k, v in f.items()
                    if k in ("name", "summary", "evidence", "category", "fit_reasoning", "url")
                    and v
                }
                for f in batch
            ],
            ensure_ascii=False,
            default=str,
        )[:24_000]
        try:
            data, _ = await llm.complete_json(
                f"Extract the knowledge graph stated in these findings:\n\n{payload}",
                system=_EXTRACT_SYSTEM,
                grounded=False,
                max_tokens=4000,
                temperature=0.0,
                label="graph-extract",
            )
            parsed = coerce_dict(data, context="graph extraction")
        except Exception as exc:  # noqa: BLE001
            log.warning("[graph] LLM extraction failed for batch %d: %s", start // batch_size, exc)
            continue

        types = {
            str(e.get("name", "")).strip(): str(e.get("type", "thing")).strip() or "thing"
            for e in parsed.get("entities", [])
            if isinstance(e, dict) and e.get("name")
        }
        for relation in parsed.get("relations", []):
            if not isinstance(relation, dict):
                continue
            src, pred, dst = (
                str(relation.get("source", "")).strip(),
                str(relation.get("predicate", "")).strip(),
                str(relation.get("target", "")).strip(),
            )
            if not (src and pred and dst) or src.lower() == dst.lower():
                continue
            graph.add_relation(
                src, pred, dst,
                source_kind=types.get(src, "thing"),
                target_kind=types.get(dst, "thing"),
                attrs={"extracted": "llm"},
                confidence="medium",
                run_id=run_id,
                agent="graph-extract",
                snippet=str(relation.get("evidence", ""))[:400],
            )
            written += 1

    log.info("[graph] LLM extraction added %d relations", written)
    return written


def maybe_ingest(result: Any, llm: Any | None = None) -> dict | None:
    """Honour the project config. Called automatically at the end of a run."""
    if not (project.graph.enabled and project.graph.auto_ingest):
        return None
    try:
        with KnowledgeGraph() as graph:
            return ingest_swarm_result(result, graph)
    except Exception as exc:  # noqa: BLE001
        # The graph is an accumulator, not a deliverable. Never fail a completed
        # research run because the sidecar store had a problem.
        log.warning("[graph] auto-ingest skipped: %s", exc)
        return None
