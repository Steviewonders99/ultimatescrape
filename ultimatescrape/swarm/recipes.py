"""Ready-made swarm specs.

These exist so that the common jobs are one call rather than a prompt-writing
exercise, and so the dimension sets themselves become reviewable artifacts that
improve over time instead of being retyped slightly differently each run.

Each recipe is a function returning a ``SwarmSpec``; edit or subclass freely.
"""

from __future__ import annotations

from .prompts import RESEARCH_SYSTEM
from .spec import Dimension, SwarmSpec, Target

# ── company intelligence ──────────────────────────────────────────────────────

COMPANY_CONTRACT = """{
  "findings": [
    {
      "name": "<entity, product, person, or claim>",
      "category": "<one of: overview|product|people|hiring|funding|customer|partner|risk|tech|pricing>",
      "summary": "<2-3 sentences, specific and sourced>",
      "evidence": "<the exact figure, quote, or filing detail supporting this>",
      "date": "<YYYY-MM or YYYY-MM-DD of the source>",
      "url": "<the source URL you actually retrieved>",
      "linkedin": "<LinkedIn URL if the entity has one, else null>",
      "confidence": "high|medium|low"
    }
  ]
}"""

COMPANY_DIMENSIONS = [
    Dimension(
        "profile",
        "Research {label} ({topic}). Establish the basics: legal entity name, HQ, "
        "founding year, ownership status (private/public/PE-backed/subsidiary), employee "
        "headcount with its source and date, and revenue if disclosed or credibly estimated. "
        "Prefer official registries (SEC EDGAR, Companies House, GLEIF) over press coverage.",
        why="Grounds every other dimension in verifiable entity facts.",
    ),
    Dimension(
        "products",
        "What does {label} actually sell? Enumerate product lines and services, who each is "
        "for, and how they are packaged. Use their own site, docs, changelog and pricing page "
        "as primary sources. Note the pricing model and any published prices.",
        why="Product surface is the foundation of competitive positioning.",
    ),
    Dimension(
        "people",
        "Map the leadership and org shape of {label}: founders, C-suite, and the heads of "
        "engineering, sales and product. For each give the LinkedIn URL, tenure, and prior "
        "role. Then characterise the org: rough team sizes and where staff are located.",
        why="People and their history predict strategy better than press releases.",
    ),
    Dimension(
        "hiring",
        "Analyse {label}'s current open roles. Which functions and locations are they hiring "
        "into, at what seniority, and what does the job-post language reveal about their "
        "roadmap and tech stack? Cite specific live postings with URLs.",
        why="Hiring is the least-managed public signal a company emits.",
    ),
    Dimension(
        "funding",
        "Trace {label}'s funding and financial history: rounds, dates, amounts, lead investors, "
        "valuations, M&A, and any filings. For public companies use the most recent 10-K/10-Q "
        "or equivalent and cite the specific filing.",
        why="Capital position bounds what a company can credibly do next.",
    ),
    Dimension(
        "customers",
        "Who are {label}'s customers? Find named logos, case studies, testimonials, review-site "
        "profiles (G2, Capterra, Trustpilot) with ratings and volume, and any disclosed customer "
        "counts. Note the concentration and the segments they actually win in.",
        why="Named customers separate stated positioning from real traction.",
    ),
    Dimension(
        "competition",
        "Map {label}'s competitive set: direct competitors, adjacent players, and substitutes. "
        "For each, state the basis of competition and who wins where. Use comparison pages, "
        "analyst coverage, and review-site head-to-heads.",
        why="Position is only meaningful relative to alternatives.",
    ),
    Dimension(
        "risk",
        "Identify risks around {label}: litigation, regulatory exposure, security incidents, "
        "layoffs, executive departures, negative press, employee-review patterns on Glassdoor, "
        "and platform or key-supplier dependencies. Report only sourced items.",
        why="The dimension every optimistic research pass omits.",
    ),
]


def company_research(companies: list[str], *, topic: str | None = None, verify: int = 3) -> SwarmSpec:
    return SwarmSpec(
        topic=topic or f"Company intelligence: {', '.join(companies[:5])}",
        targets=[Target.of(c) for c in companies],
        dimensions=COMPANY_DIMENSIONS,
        system_prompt=RESEARCH_SYSTEM,
        output_contract=COMPANY_CONTRACT,
        dedupe_fields=("url", "name"),
        url_fields=("url", "linkedin"),
        verifier_votes=verify,
        notes="Eight dimensions per company. 20 companies is 160 agents.",
    )


# ── market / industry research ────────────────────────────────────────────────

MARKET_CONTRACT = """{
  "findings": [
    {
      "name": "<the entity, statistic, trend, or player>",
      "category": "<one of: size|growth|segment|player|regulation|channel|pricing|trend|barrier>",
      "summary": "<2-3 sentences, specific>",
      "value": "<the number, with unit and currency, or null>",
      "period": "<the year or range the figure covers>",
      "source_name": "<the publishing organisation>",
      "url": "<source URL you actually retrieved>",
      "confidence": "high|medium|low"
    }
  ]
}"""

MARKET_DIMENSIONS = [
    Dimension(
        "size",
        "Size the {label} market for {topic}. Give total value and unit volume with currency, "
        "year, and the publishing body. Where estimates disagree, report the range and name who "
        "says what — do not average them. Prefer national statistics offices and industry bodies "
        "over vendor-sponsored reports, and say which kind each source is.",
        why="Everything downstream is denominated in this.",
    ),
    Dimension(
        "growth",
        "What is the growth trajectory of {topic} in {label}? Historical CAGR, current growth, "
        "and forecasts with their assumptions. Name the forecaster and the publication date.",
        why="Direction and its credibility, separately.",
    ),
    Dimension(
        "players",
        "Who competes in {topic} in {label}? Leaders with market share where published, notable "
        "challengers, and recent entrants or exits. Include company URLs and LinkedIn pages.",
        why="Concentration determines whether entry is plausible.",
    ),
    Dimension(
        "regulation",
        "What regulation governs {topic} in {label}? Name the statutes and regulators, licensing "
        "or compliance requirements, recent or pending changes, and enforcement actions. Cite the "
        "regulator's own pages.",
        why="Regulation is usually the real barrier to entry, not capital.",
    ),
    Dimension(
        "demand",
        "Characterise demand for {topic} in {label}: buyer segments, their size, purchase "
        "triggers, budget ownership, and sales-cycle length. Ground the segment sizes in census "
        "or national statistics data where possible and cite the table.",
        why="Segment sizes are checkable against official statistics; opinions are not.",
    ),
    Dimension(
        "channels",
        "How is {topic} bought and sold in {label}? Distribution channels, dominant marketplaces "
        "and platforms, typical partner structures, and which channels are growing.",
        why="Route to market decides cost of acquisition.",
    ),
    Dimension(
        "pricing",
        "What does {topic} cost in {label}? Prevailing price points and models, benchmarks by "
        "segment, and the direction prices are moving. Cite live pricing pages and published "
        "rate cards.",
        why="Published prices are verifiable in a way market sizing is not.",
    ),
]


def market_research(
    markets: list[str], topic: str, *, verify: int = 3
) -> SwarmSpec:
    return SwarmSpec(
        topic=topic,
        targets=[Target.of(m) for m in markets],
        dimensions=MARKET_DIMENSIONS,
        system_prompt=RESEARCH_SYSTEM,
        output_contract=MARKET_CONTRACT,
        dedupe_fields=("url", "name"),
        url_fields=("url",),
        verifier_votes=verify,
        notes="Seven dimensions per market. 30 countries is 210 agents.",
    )


# ── vendor / supplier sourcing ────────────────────────────────────────────────

VENDOR_CONTRACT = """{
  "findings": [
    {
      "name": "<company legal or trading name>",
      "website": "<homepage URL>",
      "linkedin": "<LinkedIn company URL, or null>",
      "hq_city": "<city, country>",
      "approx_size": "<one of: 1-10|11-50|51-200|201-500|500+>",
      "founded_year": "<YYYY or null>",
      "services": ["<what they actually deliver>"],
      "evidence": "<the specific page or claim showing they do this work>",
      "estimated_rate_usd": "<day or hourly rate range if discoverable, else null>",
      "fit_score": "<1-5, where 5 is a pure-play specialist match>",
      "fit_reasoning": "<why this score>",
      "contact": "<public contact route, or null>",
      "source_url": "<where you found them>",
      "confidence": "high|medium|low"
    }
  ]
}"""


def vendor_sourcing(
    countries: list[str],
    profile: str,
    *,
    exclude: list[str] | None = None,
    verify: int = 3,
) -> SwarmSpec:
    """Find small local specialist suppliers matching a profile, per country.

    The exclusion list is load-bearing rather than cosmetic. Without it every
    agent returns the same handful of globally-marketed megavendors, because
    those dominate search results — which produces a list that looks complete
    and contains nothing you could not have named yourself.
    """
    excluded = ", ".join(exclude or []) or "none specified"
    dimensions = [
        Dimension(
            "specialists",
            "Find small, local, specialist suppliers in {label} matching this profile:\n"
            f"{profile}\n\n"
            f"HARD EXCLUDE these firms and their subsidiaries: {excluded}. They price at global "
            "rates and crowd out the local suppliers this search exists to find.\n\n"
            "Issue 10-15 distinct searches. Vary the angle deliberately:\n"
            "  - plain descriptive queries in English\n"
            "  - the same queries in the country's primary language\n"
            "  - site:linkedin.com/company queries with the country and the specialism\n"
            "  - startup directories, accelerator portfolios, university spin-out lists\n"
            "  - trade association member directories and industry-body registers\n"
            "  - local business registries\n"
            "Target 6-12 verified suppliers. Four real ones beat twelve padded ones — a "
            "fabricated supplier costs more to discover than a missing one does.",
            why="Primary discovery.",
            max_tokens=16000,
        ),
        Dimension(
            "adjacent",
            "Find suppliers in {label} who are ADJACENT to this profile rather than a direct "
            f"match — they have the capability but market it differently:\n{profile}\n\n"
            "Look at neighbouring industries, agencies with a relevant practice line, and firms "
            "whose case studies show the capability even though their positioning does not. "
            f"Still exclude: {excluded}.",
            why="Adjacent suppliers are cheaper and less contested than pure-plays.",
            max_tokens=12000,
        ),
    ]
    return SwarmSpec(
        topic=f"Supplier sourcing: {profile[:80]}",
        targets=[Target.of(c) for c in countries],
        dimensions=dimensions,
        system_prompt=RESEARCH_SYSTEM,
        output_contract=VENDOR_CONTRACT,
        findings_key="findings",
        dedupe_fields=("website", "name", "linkedin"),
        url_fields=("website", "linkedin", "source_url"),
        verifier_votes=verify,
        notes="URL liveness validation matters most here — dead vendor URLs poison a bench.",
    )


# ── contributor-funnel / SEO / AEO competitive benchmark ────────────────────

BENCHMARK_CONTRACT = """{
  "findings": [
    {
      "competitor": "<company being assessed>",
      "dimension": "<landing|onboarding|marketplace|social|seo|aeo|conversion_ops>",
      "observation": "<one specific, falsifiable observation>",
      "observable_value": "<count, rank, follower level, stage, timing, or null>",
      "benchmark_implication": "<what OneForma should compare or measure>",
      "evidence_type": "<first_party_page|official_social|search_result|third_party|inference>",
      "source_name": "<publisher or platform>",
      "url": "<source URL actually retrieved>",
      "captured_at": "<YYYY-MM-DD>",
      "confidence": "high|medium|low",
      "caveat": "<what cannot be known from public evidence>"
    }
  ],
  "gaps": "<public evidence that was unavailable, blocked, or private>"
}"""

BENCHMARK_SYNTHESIS = """Produce an executive competitor benchmark for OneForma.
Keep three evidence classes visibly separate: (1) directly observed public facts,
(2) reasonable inferences from those facts, and (3) unavailable private metrics.
Never invent a competitor conversion rate. Compare observable funnel friction,
step counts, gating, pay visibility, time-to-value claims, search visibility,
social reach, and AEO extractability. End with a measurement plan mapping each
public proxy to the exact first-party OneForma funnel rate needed for a fair
internal benchmark."""


def competitor_benchmark(
    profiles: list | None = None,
    *,
    evidence_by_target: dict[str, dict[str, str]] | None = None,
    verify: int = 3,
) -> SwarmSpec:
    """Deep benchmark for contributor acquisition and onboarding.

    ``profiles`` accepts the central ``CompetitorProfile`` objects from
    :mod:`ultimatescrape.benchmark`. Importing lazily avoids making the generic
    swarm recipe module depend on the optional Crawl4AI tier.
    """
    if profiles is None:
        from ..benchmark import resolve_profiles

        profiles = resolve_profiles()
    crawl = evidence_by_target or {}
    targets: list[Target] = []
    for profile in profiles:
        observed = crawl.get(profile.key, {})
        targets.append(
            Target(
                key=profile.key,
                label=profile.label,
                context={
                    "domain": profile.domain,
                    "landing_evidence": observed.get("landing", "No Crawl4AI capture supplied."),
                    "funnel_evidence": observed.get("funnel", "No Crawl4AI capture supplied."),
                    "onboarding_evidence": observed.get(
                        "onboarding", "No Crawl4AI capture supplied."
                    ),
                    "crawl_evidence": observed.get("all", "No Crawl4AI capture supplied."),
                    "social_profiles": ", ".join(
                        f"{platform}: {url}" for platform, url in profile.social_profiles.items()
                    )
                    or "No official social profiles catalogued.",
                },
            )
        )

    dimensions = [
        Dimension(
            "landing",
            "Audit {label}'s public contributor landing page on {domain}. Use the Crawl4AI "
            "capture below as primary evidence, then retrieve the live page to verify material "
            "claims. Map audience, hero promise, proof, pay transparency, CTA hierarchy, FAQ, "
            "objection handling, mobile/visual friction, and message match from ad/search intent. "
            "Count observed CTAs and forms; do not estimate conversion.\n\nCRAWL:\n{landing_evidence}",
            why="Landing-page message and friction are observable conversion inputs.",
            max_tokens=7000,
        ),
        Dimension(
            "onboarding",
            "Reconstruct the current public onboarding funnel for {label}, from first visit to "
            "first eligible paid work. Identify every evidenced stage, required field, identity "
            "or phone check, assessment, agreement, payment setup, human review, and wait state. "
            "Report the minimum observable steps and every unknown; never create an account or "
            "submit personal data.\n\nCRAWL:\n{onboarding_evidence}",
            why="Stage/gate inventory is the closest defensible public proxy for onboarding CVR.",
            max_tokens=8000,
        ),
        Dimension(
            "marketplace",
            "Assess {label}'s public work marketplace and time-to-value. Check whether jobs can be "
            "browsed before registration, whether pay/rate/unit/location/capacity are visible, how "
            "matching works, what qualifications are disclosed, and the stated or implied delay "
            "between signup, qualification, matching, first task, QA, and payment. Distinguish "
            "company claims from verified mechanics.\n\nCRAWL:\n{funnel_evidence}",
            why="Pay visibility and time-to-first-work are major activation levers.",
            max_tokens=7000,
        ),
        Dimension(
            "social",
            "Benchmark {label}'s current official social reach. Check the catalogued official "
            "profiles below and official-site links. For LinkedIn, YouTube, Facebook, Instagram, "
            "TikTok and X, record the exact visible follower/subscriber count, capture date, last "
            "post date, posting cadence over the latest 30 days, and median visible engagement "
            "over the latest 10 non-pinned posts where public. Use null when a platform blocks or "
            "hides a metric; never infer one follower count from another.\n\nPROFILES:\n{social_profiles}",
            why="Reach without cadence and engagement is a vanity metric, so all three travel together.",
            max_tokens=7500,
        ),
        Dimension(
            "seo",
            "Measure {label}'s live organic-search visibility for this fixed English query panel: "
            "AI training jobs, data annotation jobs, remote AI jobs, AI data collection company, "
            "human data platform, and train AI for money. Record search engine, locale, date, "
            "observed position/range, ranking URL, title, snippet angle, and SERP features. Also "
            "audit title/H1 alignment, indexability, content depth, internal links and freshness on "
            "the ranking page. A missing result is a gap, not rank zero.",
            why="A fixed panel makes future share-of-search runs comparable.",
            max_tokens=8500,
        ),
        Dimension(
            "aeo",
            "Audit {label}'s AEO/AI-search readiness and visibility. Check robots.txt access for "
            "GPTBot, ChatGPT-User, PerplexityBot, ClaudeBot, Google-Extended and Bingbot. Review "
            "definition blocks, self-contained answers, FAQs, comparison tables, author/date "
            "signals, original statistics, third-party mentions, and entity consistency. Record "
            "actual observed citations only when the named AI/search surface exposes them; do not "
            "call ordinary Google rank an AI citation.\n\nCRAWL:\n{crawl_evidence}",
            why="Traditional rank and AI citation are related but not interchangeable.",
            max_tokens=8000,
        ),
        Dimension(
            "conversion_ops",
            "Translate the public evidence for {label} into an operations benchmark for OneForma. "
            "List the observable proxy, the exact OneForma first-party metric needed for a fair "
            "comparison, numerator, denominator, clock start/stop, segmentation, and confounders. "
            "Cover landing-to-signup, signup completion, profile completion, qualification start "
            "and pass, application completion, approval, first task, first approved unit, first "
            "payment, 30-day activation and 90-day retention. Explicitly state that {label}'s "
            "actual conversion rates are private unless a sourced disclosure exists.",
            why="Turns competitive observation into an internal measurement contract.",
            max_tokens=7000,
        ),
    ]
    return SwarmSpec(
        topic="Contributor onboarding, landing page, social, SEO and AEO benchmark",
        targets=targets,
        dimensions=dimensions,
        system_prompt=RESEARCH_SYSTEM,
        output_contract=BENCHMARK_CONTRACT,
        findings_key="findings",
        dedupe_fields=("competitor", "dimension", "url"),
        url_fields=("url",),
        verifier_votes=verify,
        synthesis_prompt=BENCHMARK_SYNTHESIS,
        notes=(
            "Seven dimensions per company. Public evidence supports friction proxies, not private "
            "competitor conversion rates. Crawl4AI evidence is injected dimension-by-dimension."
        ),
    )


RECIPES = {
    "company": company_research,
    "market": market_research,
    "vendor": vendor_sourcing,
    "benchmark": competitor_benchmark,
}
