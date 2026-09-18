"""Deterministic competitor-funnel crawl feeding the research swarm.

The browser is evidence collection, not analysis. Crawl4AI renders each public
surface and this module computes the observable facts (headings, CTA labels,
forms, fields and funnel links) without asking an LLM. The compact evidence
blocks can then be injected into the relevant swarm dimensions, so research
agents start from pages we actually retrieved rather than remembered copy.

Actual competitor conversion rates are private. This module deliberately never
manufactures them: its outputs are public signals and friction proxies that an
operator can compare with first-party OneForma funnel rates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from .fetch.browser import BrowserFetcher, BrowserOptions
from .fetch.http import Fetcher, FetchResult
from .output import dataset_from_rows, export
from .output.formats import Dataset, slugify


@dataclass(frozen=True)
class BenchmarkSurface:
    key: str
    kind: str
    url: str


@dataclass(frozen=True)
class CompetitorProfile:
    key: str
    label: str
    domain: str
    surfaces: tuple[BenchmarkSurface, ...]
    social_profiles: dict[str, str] = field(default_factory=dict)


@dataclass
class BenchmarkCrawl:
    dataset: Dataset
    rows: list[dict[str, Any]]
    evidence_by_target: dict[str, dict[str, str]]
    paths: list[Path]


ONEFORMA = CompetitorProfile(
    key="oneforma",
    label="OneForma",
    domain="oneforma.com",
    surfaces=(
        BenchmarkSurface("home", "landing", "https://www.oneforma.com/"),
        BenchmarkSurface(
            "how-it-works", "funnel", "https://www.oneforma.com/how-oneforma-works/"
        ),
        BenchmarkSurface(
            "account-creation",
            "onboarding",
            "https://www.oneforma.com/help-center/general-inquiries/how-to-open-a-oneforma-account/",
        ),
        BenchmarkSurface(
            "project-application",
            "onboarding",
            "https://www.oneforma.com/help-center/general-inquiries/how-to-apply-to-projects-in-your-dashboard/",
        ),
    ),
    social_profiles={
        "linkedin": "https://www.linkedin.com/company/oneformaglobal/",
        "facebook": "https://www.facebook.com/OneForma/",
        "instagram": "https://www.instagram.com/oneforma/",
        "youtube": "https://www.youtube.com/@OneForma",
    },
)


COMPETITORS: dict[str, CompetitorProfile] = {
    "appen": CompetitorProfile(
        key="appen",
        label="Appen / CrowdGen",
        domain="crowdgen.com",
        surfaces=(
            BenchmarkSurface("home", "landing", "https://crowdgen.com/"),
            BenchmarkSurface(
                "sign-up",
                "onboarding",
                "https://app.crowdgen.com/apply/signup?attribution=crowdgencom_homepage",
            ),
            BenchmarkSurface("pro", "funnel", "https://crowdgen.com/pro-coding/"),
        ),
        social_profiles={
            "linkedin": "https://www.linkedin.com/company/appen/",
            "facebook": "https://www.facebook.com/appenglobal/",
            "youtube": "https://www.youtube.com/@AppenGlobal",
            "x": "https://x.com/AppenGlobal",
        },
    ),
    "imerit": CompetitorProfile(
        key="imerit",
        label="iMerit Scholars",
        domain="join-scholars.imerit.net",
        surfaces=(
            BenchmarkSurface("home", "landing", "https://join-scholars.imerit.net/"),
            BenchmarkSurface(
                "roadmap",
                "onboarding",
                "https://scholarhelpdesk.imerit.net/kb/welcome-to-imerit-scholars-your-onboarding-roadmap1",
            ),
            BenchmarkSurface(
                "project", "funnel", "https://join-scholars.imerit.net/single-job/"
            ),
        ),
        social_profiles={
            "linkedin": "https://www.linkedin.com/company/imerit/",
            "facebook": "https://www.facebook.com/iMeritTechnology/",
            "youtube": "https://www.youtube.com/@iMeritTechnology",
            "x": "https://x.com/iMeritDigital",
        },
    ),
    "dataforce": CompetitorProfile(
        key="dataforce",
        label="DataForce by TransPerfect",
        domain="dataforcecommunity.transperfect.com",
        surfaces=(
            BenchmarkSurface(
                "home", "landing", "https://dataforcecommunity.transperfect.com/"
            ),
            BenchmarkSurface(
                "projects",
                "funnel",
                "https://dataforcecommunity.transperfect.com/projects",
            ),
            BenchmarkSurface(
                "faq", "onboarding", "https://dataforcecommunity.transperfect.com/FAQ"
            ),
        ),
        social_profiles={
            "linkedin": "https://www.linkedin.com/company/dataforceai/",
            "facebook": "https://www.facebook.com/DataForceCommunity/",
            "instagram": "https://www.instagram.com/dataforcecommunity/",
        },
    ),
    "surge": CompetitorProfile(
        key="surge",
        label="Surge AI",
        domain="surgehq.ai",
        surfaces=(
            BenchmarkSurface("workforce", "landing", "https://www.surgehq.ai/workforce"),
            BenchmarkSurface("about", "funnel", "https://www.surgehq.ai/about"),
            BenchmarkSurface(
                "consultant-network", "onboarding", "https://www.surgehq.ai/consultant-network"
            ),
        ),
        social_profiles={
            "linkedin": "https://www.linkedin.com/company/surge-ai/",
            "x": "https://x.com/HelloSurgeAI",
        },
    ),
    "mercor": CompetitorProfile(
        key="mercor",
        label="Mercor",
        domain="mercor.com",
        surfaces=(
            BenchmarkSurface("experts", "landing", "https://www.mercor.com/experts/"),
            BenchmarkSurface("opportunities", "funnel", "https://work.mercor.com/explore"),
            BenchmarkSurface("talent-docs", "onboarding", "https://talent.docs.mercor.com/"),
        ),
        social_profiles={
            "linkedin": "https://www.linkedin.com/company/mercor-ai/",
            "x": "https://x.com/mercor_ai",
            "youtube": "https://www.youtube.com/@mercor-ai",
        },
    ),
}

DEFAULT_COMPETITORS = ("appen", "imerit", "dataforce", "surge", "mercor")

_ALIASES = {
    "crowdgen": "appen",
    "appen/crowdgen": "appen",
    "imerit scholars": "imerit",
    "dataforce by transperfect": "dataforce",
    "surge ai": "surge",
}

_CTA_HINT = re.compile(
    r"\b(apply|join|sign[ -]?up|register|start|explore|browse|find|create|book|contact|"
    r"learn|get paid|get matched|see (?:jobs|projects|roles|how))\b",
    re.IGNORECASE,
)
_FUNNEL_PATH = re.compile(
    r"/(apply|application|signup|sign-up|register|join|jobs?|careers?|projects?|experts?|"
    r"login|account|profile|assessment|interview|payments?|faq|help|how-it-works)(?:/|\?|$)",
    re.IGNORECASE,
)


def resolve_profiles(
    values: list[str] | tuple[str, ...] | None = None,
    *,
    include_oneforma: bool = True,
) -> list[CompetitorProfile]:
    keys = list(values or DEFAULT_COMPETITORS)
    profiles = [ONEFORMA] if include_oneforma else []
    seen = {p.key for p in profiles}
    for value in keys:
        key = slugify(_ALIASES.get(value.strip().lower(), value)).replace("-", "")
        # Canonical keys contain no punctuation, so the compact comparison is
        # helpful for inputs such as "Surge AI" and "iMerit Scholars".
        candidates = {
            slugify(k).replace("-", ""): k for k in COMPETITORS
        }
        canonical = candidates.get(key)
        if canonical is None:
            supported = ", ".join(DEFAULT_COMPETITORS)
            raise ValueError(f"unknown competitor {value!r}; supported: {supported}")
        if canonical not in seen:
            profiles.append(COMPETITORS[canonical])
            seen.add(canonical)
    return profiles


def _node_text(node) -> str:
    try:
        return " ".join(node.text(separator=" ", strip=True).split())
    except TypeError:
        return " ".join(node.text().split())


def _unique(values: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = " ".join(value.split()).strip()
        norm = clean.casefold()
        if not clean or norm in seen:
            continue
        seen.add(norm)
        out.append(clean[:180])
        if len(out) >= limit:
            break
    return out


def _schema_types(value: Any) -> list[str]:
    """Return every schema.org ``@type`` found in a JSON-LD value."""
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            kind = item.get("@type")
            if isinstance(kind, str):
                found.append(kind)
            elif isinstance(kind, list):
                found.extend(str(part) for part in kind if part)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return _unique(found, 30)


def extract_page_signals(result: FetchResult) -> dict[str, Any]:
    """Extract public funnel/CRO signals from one fetched page."""
    markdown = result.doc.markdown if result.doc else ""
    raw = result.raw or ""
    headings: list[str] = []
    ctas: list[str] = []
    fields: list[str] = []
    required_fields = 0
    forms = 0
    schema_types: list[str] = []
    links: list[str] = list(result.doc.links if result.doc else [])

    if raw:
        tree = HTMLParser(raw)
        for node in tree.css('script[type="application/ld+json"]'):
            try:
                schema_types.extend(_schema_types(json.loads(node.text(strip=True))))
            except (json.JSONDecodeError, TypeError):
                continue
        headings = [_node_text(n) for n in tree.css("h1, h2, h3")]
        for node in tree.css("a, button, input[type=submit], input[type=button]"):
            text = _node_text(node) or (node.attributes.get("value") or "")
            if _CTA_HINT.search(text):
                ctas.append(text)
        form_nodes = tree.css("form")
        forms = len(form_nodes)
        for node in tree.css("input, select, textarea"):
            attrs = node.attributes
            field_type = (attrs.get("type") or node.tag or "field").lower()
            if field_type == "hidden":
                continue
            label = attrs.get("name") or attrs.get("aria-label") or attrs.get("placeholder") or ""
            fields.append(f"{field_type}:{label}" if label else field_type)
            if "required" in attrs or attrs.get("aria-required") == "true":
                required_fields += 1
        for node in tree.css("a[href]"):
            href = node.attributes.get("href")
            if href:
                links.append(urljoin(result.final_url or result.url, href))

    if not headings:
        headings = [
            match.group(2).strip()
            for match in re.finditer(r"^(#{1,3})\s+(.+)$", markdown, re.MULTILINE)
        ]
    if not ctas:
        for label, _url in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", markdown):
            if _CTA_HINT.search(label):
                ctas.append(label)

    funnel_links = [url for url in links if _FUNNEL_PATH.search(urlparse(url).path + "/")]
    return {
        "headings": _unique(headings, 12),
        "ctas": _unique(ctas, 12),
        "forms": forms,
        "fields": _unique(fields, 20),
        "required_fields": required_fields,
        "funnel_links": _unique(funnel_links, 20),
        "schema_types": _unique(schema_types, 30),
        "excerpt": " ".join(markdown.split())[:1200],
    }


def _row(
    profile: CompetitorProfile,
    surface: BenchmarkSurface,
    result: FetchResult,
    *,
    backend: str,
) -> dict[str, Any]:
    signals = extract_page_signals(result)
    doc = result.doc
    return {
        "competitor_key": profile.key,
        "competitor": profile.label,
        "surface": surface.key,
        "surface_kind": surface.kind,
        "requested_url": surface.url,
        "final_url": result.final_url,
        "ok": result.ok,
        "status": result.status,
        "backend": backend,
        "title": doc.title if doc else None,
        "description": doc.description if doc else None,
        "word_count": doc.word_count if doc else 0,
        "headings": signals["headings"],
        "ctas": signals["ctas"],
        "forms": signals["forms"],
        "fields": signals["fields"],
        "required_fields": signals["required_fields"],
        "funnel_links": signals["funnel_links"],
        "schema_types": signals["schema_types"],
        "page_excerpt": signals["excerpt"][:600],
        "fetched_at": result.fetched_at,
        "error": result.error,
        "social_profiles": profile.social_profiles,
    }


def _evidence(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        target = grouped.setdefault(row["competitor_key"], {})
        kind = row["surface_kind"]
        lines = target.setdefault(kind, [])
        lines.append(
            " | ".join(
                (
                    f"URL={row['final_url'] or row['requested_url']}",
                    f"status={row['status']}",
                    f"title={row['title'] or ''}",
                    f"headings={'; '.join(row['headings'][:5])}",
                    f"ctas={'; '.join(row['ctas'][:6])}",
                    f"forms={row['forms']}",
                    f"fields={'; '.join(row['fields'][:8])}",
                    f"schema={'; '.join(row['schema_types'][:8])}",
                    f"excerpt={row['page_excerpt'][:500]}",
                )
            )
        )

    evidence: dict[str, dict[str, str]] = {}
    for target, kinds in grouped.items():
        combined = {kind: "\n".join(parts)[:6000] for kind, parts in kinds.items()}
        combined["all"] = "\n".join(
            f"[{kind}]\n{text}" for kind, text in combined.items() if kind != "all"
        )[:12000]
        evidence[target] = combined
    return evidence


async def crawl_competitor_surfaces(
    profiles: list[CompetitorProfile],
    *,
    directory: Path,
    use_browser: bool = True,
    max_sessions: int = 6,
) -> BenchmarkCrawl:
    """Crawl public benchmark surfaces and export a reusable evidence corpus."""
    directory.mkdir(parents=True, exist_ok=True)
    requested: list[tuple[CompetitorProfile, BenchmarkSurface]] = [
        (profile, surface) for profile in profiles for surface in profile.surfaces
    ]
    urls = [surface.url for _profile, surface in requested]

    # HTTP is the cheap, high-fidelity first tier for server-rendered pages.
    # Crawl4AI is the evidence-triggered fallback for failures and JS shells.
    # This avoids a browser making a static page *worse* (some sites serve a
    # sparse anti-bot shell to Chromium while returning full public HTML).
    async with Fetcher(keep_raw=True) as fetcher:
        results = await fetcher.fetch_many(urls)
    backends = ["http"] * len(results)
    if use_browser:
        thin = [
            index
            for index, result in enumerate(results)
            if not result.ok or not result.doc or result.doc.word_count < 120
        ]
        if thin:
            browser_urls = [urls[index] for index in thin]
            async with BrowserFetcher(
                BrowserOptions(keep_raw=True, page_timeout_ms=60_000)
            ) as fetcher:
                browser_results = await fetcher.fetch_many(
                    browser_urls, max_sessions=max_sessions
                )
            for index, browser_result in zip(thin, browser_results, strict=False):
                http_words = results[index].doc.word_count if results[index].doc else 0
                browser_words = browser_result.doc.word_count if browser_result.doc else 0
                if browser_result.ok and (not results[index].ok or browser_words > http_words):
                    results[index] = browser_result
                    backends[index] = "crawl4ai"

    rows = [
        _row(profile, surface, result, backend=backend)
        for (profile, surface), result, backend in zip(
            requested, results, backends, strict=False
        )
    ]
    dataset = dataset_from_rows(
        "Competitor funnel crawl",
        rows,
        summary={
            "companies": len(profiles),
            "pages_requested": len(requested),
            "pages_ok": sum(1 for row in rows if row["ok"]),
            "pages_failed": sum(1 for row in rows if not row["ok"]),
            "browser_fallback": use_browser,
            "browser_pages_used": sum(1 for backend in backends if backend == "crawl4ai"),
        },
        columns=(
            "competitor",
            "surface",
            "surface_kind",
            "requested_url",
            "final_url",
            "status",
            "backend",
            "title",
            "word_count",
            "headings",
            "ctas",
            "forms",
            "fields",
            "required_fields",
            "funnel_links",
            "schema_types",
            "error",
            "fetched_at",
        ),
        kind="competitor-benchmark-crawl",
        meta={"captured_at": datetime.now(UTC).isoformat()},
    )
    paths = export(
        dataset,
        directory=directory,
        formats=("markdown", "json", "csv", "xlsx"),
        basename="crawl-signals",
    )

    pages_dir = directory / "pages"
    pages_dir.mkdir(exist_ok=True)
    corpus: list[str] = ["# Competitor crawl corpus\n"]
    for (profile, surface), result in zip(requested, results, strict=False):
        markdown = result.doc.markdown if result.doc else ""
        page_path = pages_dir / f"{profile.key}-{surface.kind}-{surface.key}.md"
        page_path.write_text(
            f"# {profile.label}: {surface.key}\n\n"
            f"Source: {result.final_url or surface.url}\n\n"
            f"Status: {result.status or 'failed'}\n\n{markdown[:50000]}",
            encoding="utf-8",
        )
        corpus.append(
            f"\n## {profile.label} — {surface.key}\n\n"
            f"Source: {result.final_url or surface.url}\n\n{markdown[:8000]}\n"
        )
    corpus_path = directory / "crawl-corpus.md"
    corpus_path.write_text("\n".join(corpus), encoding="utf-8")
    paths.append(corpus_path)

    evidence = _evidence(rows)
    evidence_path = directory / "swarm-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    paths.append(evidence_path)
    return BenchmarkCrawl(dataset=dataset, rows=rows, evidence_by_target=evidence, paths=paths)
