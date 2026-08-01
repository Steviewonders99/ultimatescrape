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


RECIPES = {
    "company": company_research,
    "market": market_research,
    "vendor": vendor_sourcing,
}
