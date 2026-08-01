"""Competitor and job-board registry.

The important discovery behind this module: almost every AI-data competitor runs
its corporate hiring on a standard ATS with a public JSON API. That means no
scraping, no browser, no rate limits worth worrying about — one adapter per ATS
covers nine companies. A handful run custom endpoints, and those are the valuable
ones, because they carry **worker pay rates** that the corporate boards do not.

Board tokens are recorded here because most are not guessable from the company
name — Invisible is ``agency``, Sama is ``samainc``, Appen is ``appen`` and not
the ``appen-2`` its own site advertises. Guessing wastes an afternoon per company.

Two verified traps are encoded in the adapters rather than left as folklore:

* **SmartRecruiters answers HTTP 200 for tokens that do not exist**, returning
  ``{"totalFound": 0}``. Gate on the count, never the status code.
* **Workable answers 200 with a real company name and an empty ``jobs`` array**
  for dormant accounts. Check the array length.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Access(str, Enum):
    ATS = "ats"            # public JSON from a standard applicant tracking system
    CUSTOM_JSON = "custom" # the company's own JSON endpoint
    BROWSER = "browser"    # needs a headless browser (bot protection)
    LOGIN = "login"        # not reachable without an account


@dataclass(frozen=True)
class Platform:
    key: str
    company: str
    access: Access
    ats: str = ""
    token: str = ""
    #: True when the feed lists worker gigs rather than corporate roles. This is
    #: the competitively interesting set — rates, geography, and task types.
    worker_gigs: bool = False
    #: True when pay data is present in the feed itself.
    has_pay: bool = False
    linkedin: str = ""
    careers_url: str = ""
    notes: str = ""


PLATFORMS: list[Platform] = [
    # ── custom JSON feeds — highest value, all carry pay ──────────────────────
    Platform(
        key="outlier",
        company="Outlier (Scale AI)",
        access=Access.CUSTOM_JSON,
        worker_gigs=True,
        has_pay=True,
        careers_url="https://app.outlier.ai/en/expert/opportunities",
        linkedin="https://www.linkedin.com/company/outlierai",
        notes=(
            "Scale's worker-facing arm. Its board API also exposes a hiring-volume "
            "field, which no competitor publishes — a direct read on how fast Scale "
            "is absorbing contributors."
        ),
    ),
    Platform(
        key="mercor",
        company="Mercor",
        access=Access.CUSTOM_JSON,
        worker_gigs=True,
        has_pay=True,
        careers_url="https://work.mercor.com/explore",
        linkedin="https://www.linkedin.com/company/mercor",
        notes="Listings embedded as __NEXT_DATA__; per-listing detail API adds rates.",
    ),
    Platform(
        key="micro1",
        company="Micro1",
        access=Access.CUSTOM_JSON,
        worker_gigs=True,
        has_pay=True,
        careers_url="https://micro1.ai/jobs",
        notes="Paginated POST endpoint; structured pay on ~93% of listings.",
    ),
    Platform(
        key="imerit",
        company="iMerit",
        access=Access.CUSTOM_JSON,
        worker_gigs=True,
        has_pay=True,
        careers_url="https://imerit.ai/jobs.json",
        linkedin="https://www.linkedin.com/company/imerit-technology",
        notes=(
            "A single static JSON file — the simplest feed of the lot. Every role is "
            "an independent-contractor gig and nearly all carry an explicit rate, "
            "which makes it the clearest view of geographic rate arbitrage available."
        ),
    ),
    # ── Ashby ─────────────────────────────────────────────────────────────────
    Platform(
        key="handshake",
        company="Handshake",
        access=Access.ATS,
        ats="ashby",
        token="handshake",
        has_pay=True,
        linkedin="https://www.linkedin.com/company/handshake",
        notes="Both a competitor (Handshake AI) and a student job board.",
    ),
    Platform(
        key="telus",
        company="TELUS Digital (formerly Lionbridge AI)",
        access=Access.ATS,
        ats="ashby",
        token="telus-digital",
        has_pay=True,
        linkedin="https://www.linkedin.com/company/telusdigital",
        notes=(
            "Ashby is only the corporate slice. The crowd-work gigs live on a "
            "Cloudflare-protected Avature board and need the browser tier."
        ),
    ),
    Platform(
        key="surge",
        company="Surge AI",
        access=Access.ATS,
        ats="ashby",
        token="surge-ai",
        linkedin="https://www.linkedin.com/company/surgehq",
    ),
    # ── Greenhouse ────────────────────────────────────────────────────────────
    Platform(
        key="scale",
        company="Scale AI",
        access=Access.ATS,
        ats="greenhouse",
        token="scaleai",
        linkedin="https://www.linkedin.com/company/scaleai",
    ),
    Platform(
        key="invisible",
        company="Invisible Technologies",
        access=Access.ATS,
        ats="greenhouse",
        token="agency",
        has_pay=True,
        linkedin="https://www.linkedin.com/company/invisible-technologies",
        notes="Token is 'agency', not anything resembling the company name.",
    ),
    Platform(
        key="prolific",
        company="Prolific",
        access=Access.ATS,
        ats="greenhouse",
        token="prolific",
        linkedin="https://www.linkedin.com/company/prolific-ac",
    ),
    Platform(
        key="turing",
        company="Turing",
        access=Access.ATS,
        ats="greenhouse",
        token="turing",
        linkedin="https://www.linkedin.com/company/turingcom",
        notes=(
            "Corporate roles only. The developer marketplace at work.turing.com is "
            "behind a login and its public /jobs pages are SEO templates, not postings."
        ),
    ),
    Platform(
        key="toloka",
        company="Toloka",
        access=Access.ATS,
        ats="greenhouse",
        token="toloka",
        linkedin="https://www.linkedin.com/company/toloka-ai",
    ),
    Platform(
        key="remotasks",
        company="Remotasks (Scale AI)",
        access=Access.ATS,
        ats="greenhouse",
        token="remotasks",
    ),
    Platform(
        key="sama",
        company="Sama",
        access=Access.ATS,
        ats="greenhouse",
        token="samainc",
        linkedin="https://www.linkedin.com/company/samasource",
    ),
    # ── Lever ─────────────────────────────────────────────────────────────────
    Platform(
        key="appen",
        company="Appen",
        access=Access.ATS,
        ats="lever",
        token="appen",
        has_pay=True,
        linkedin="https://www.linkedin.com/company/appen",
        notes="Structured pay on every listing. A second board exists at 'appen-2'.",
    ),
    # ── other ─────────────────────────────────────────────────────────────────
    Platform(
        key="clickworker",
        company="Clickworker",
        access=Access.ATS,
        ats="join",
        token="clickworker",
        linkedin="https://www.linkedin.com/company/clickworker-com",
    ),
    Platform(
        key="transperfect",
        company="TransPerfect / DataForce",
        access=Access.ATS,
        ats="workday",
        token="transperfect",
        linkedin="https://www.linkedin.com/company/transperfect",
        notes="DataForce community projects sit on a separate Drupal site.",
    ),
    Platform(
        key="telus-gigs",
        company="TELUS AI Community (crowd gigs)",
        access=Access.BROWSER,
        worker_gigs=True,
        has_pay=True,
        careers_url="https://jobs.telusdigital.com/search/cfm5/ai-community/jobs",
        notes=(
            "The most directly comparable competitor gig set, with hourly rates in "
            "the body text. robots.txt permits it but Cloudflare blocks plain HTTP, "
            "so this is the one platform that genuinely needs the browser tier."
        ),
    ),
]

BY_KEY: dict[str, Platform] = {p.key: p for p in PLATFORMS}

#: The cheapest platforms to hit and the ones carrying worker pay data.
PHASE_ONE: tuple[str, ...] = ("outlier", "mercor", "micro1", "imerit", "appen")


def get(key: str) -> Platform:
    if key not in BY_KEY:
        raise KeyError(f"unknown platform {key!r}. Known: {', '.join(sorted(BY_KEY))}")
    return BY_KEY[key]


def with_pay() -> list[Platform]:
    return [p for p in PLATFORMS if p.has_pay]


def worker_platforms() -> list[Platform]:
    return [p for p in PLATFORMS if p.worker_gigs]


def by_access(access: Access | str) -> list[Platform]:
    value = access.value if isinstance(access, Access) else access
    return [p for p in PLATFORMS if p.access.value == value]
