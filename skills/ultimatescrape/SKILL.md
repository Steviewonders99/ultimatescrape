---
name: ultimatescrape
description: Use when the user wants deep research across companies, markets, industries, countries, suppliers, or people — anything needing many sources gathered, cross-checked, and synthesised. Also use for scraping web pages to clean markdown, LinkedIn company/profile lookups, competitor job-board and pay-rate monitoring (Scale, Appen, Handshake, Outlier, iMerit, Mercor, Toloka, Surge, TELUS), GA4 analytics reporting (traffic, device/OS mix, country and city engagement), querying official census and company-registry APIs, or publishing finished documents to SharePoint. Triggers include "research this company", "size this market", "find suppliers in", "who competes with", "what do competitors pay", "scrape this page", "look up on LinkedIn", "census data for", "GA4", "analytics", "which countries engage most", "device breakdown", "upload to SharePoint", "what do we know about X".
---

# UltimateScrape

Swarm research over the web, LinkedIn, and 61 official statistics and
company-registry APIs. Lives at `~/UltimateScrape`. Run everything through its
venv: `~/UltimateScrape/.venv/bin/uscrape`.

## Before anything expensive

```bash
cd ~/UltimateScrape && .venv/bin/uscrape doctor
```

Shows the LLM balance, which channel backends are live, and which of the 61 data
sources are usable. Run it first when a job will take more than a few agents —
discovering a missing key forty minutes into a run is the failure this prevents.

## Choosing an approach

**One page, or a handful of known URLs** → `uscrape fetch`. No LLM cost.

**A specific statistic from an official source** → `uscrape census`, or the
`StatsClient` in Python. Free, exact, and citable. Always prefer this over asking
a model for a number — the whole system is built on the premise that models supply
judgement and code supplies arithmetic.

**A research question spanning many entities** → a swarm. This is the expensive
path; size it deliberately (see Cost below).

## Commands

```bash
# Fetch pages as clean markdown
.venv/bin/uscrape fetch <url> [<url>...] [--browser] [-o out.json]

# LinkedIn through the tier chain (read docs/LINKEDIN.md first)
.venv/bin/uscrape linkedin <url>... [--employees]

# Swarms
.venv/bin/uscrape company "Acme" "Globex"                    # 8 dimensions each
.venv/bin/uscrape market "United States" "Japan" -t "topic"  # 7 dimensions each
.venv/bin/uscrape vendor -c Brazil -c Vietnam -p "<supplier profile>" -x "<exclude>"

# Official data
.venv/bin/uscrape sources [--ready] [--country X] [--protocol pxweb] [--json]
.venv/bin/uscrape census --var B01003_001E --for "state:*" --dataset 2023/acs/acs5

# Finish an interrupted run, paying only for the gaps
.venv/bin/uscrape resume latest
```

Add `--no-verify` to skip adversarial verification (roughly halves cost, and
findings come back marked `unverified` rather than falsely confident).

## Cost — size the run before starting it

Measured on a live run: **~$0.066 per grounded agent**. Agents = targets ×
dimensions. Verification adds one call per lens per finding, which is usually the
larger number.

| Job | Agents | Rough cost |
|---|---|---|
| 3 companies, no verify | 24 | ~$1.60 |
| 3 companies, 3-lens verify | 24 + verifiers | ~$4–6 |
| 20 countries × 7 dimensions | 140 | ~$9 |

A hard ceiling (`USCRAPE_MAX_RUN_COST_USD`, default $25) aborts the run rather
than overrunning. For anything projected above ~$10, say the estimate to the user
before starting.

Grounded agents take 1–3 minutes each, so a large run is genuinely long. Start it,
report the run id, and let it checkpoint — do not sit and poll.

## Custom swarms

When the built-in recipes don't fit, write a spec. This is the normal case for
anything domain-specific.

```python
import asyncio
from ultimatescrape import Swarm
from ultimatescrape.swarm.spec import SwarmSpec, Target, Dimension
from ultimatescrape.swarm.prompts import RESEARCH_SYSTEM
from ultimatescrape.store.report import render

spec = SwarmSpec(
    topic="<the question>",
    targets=[Target.of(x) for x in ("A", "B")],       # the entities
    dimensions=[                                       # the angles
        Dimension("key", "Ask one narrow question about {label}.", max_tokens=6000),
    ],
    system_prompt=RESEARCH_SYSTEM,
    output_contract='{"findings":[{"name":"","summary":"","url":"","confidence":""}]}',
    dedupe_fields=("url", "name"),
    url_fields=("url",),
    verifier_votes=3,
)

async def main():
    async with Swarm() as swarm:
        result = await swarm.run(spec)
    print(render(result))

asyncio.run(main())
```

Rules that matter when writing dimensions:

- **One narrow question per dimension.** The quality comes from decomposition. A
  dimension asking three things returns hedged prose for all three.
- **Name the sources you want** in the prompt. `RESEARCH_SYSTEM` already lists
  authoritative ones and forbids inventing URLs.
- **Ask for gaps.** The contract footer requires a `gaps` field; an unstated gap
  reads downstream as a finding of zero.
- **Use `activates_when`** to skip dimensions that don't apply to a target. A
  skipped agent costs nothing.

## Reading the output

Runs land in `~/UltimateScrape/runs/<run_id>/`:

- `report.md` — the readable artifact
- `findings.json` — machine-readable, merged and verified
- `units/*.json` — one file per agent, the resume unit
- `manifest.json` — spec, stats, ledger

Every finding carries provenance fields worth surfacing to the user:

- `_verdict` — `supported` / `refuted` / `unverified`
- `_verdict_votes` — e.g. `2/3`
- `_corroborations` — how many independent agents found it
- `_url_status` — result of the deterministic liveness check
- `_problems` — what the verifiers objected to

**Report refuted and unverified findings rather than silently dropping them.** A
refuted finding is evidence about the topic *and* about the swarm. Dead URLs
override model agreement — a finding whose URL does not resolve is marked refuted
no matter how many verifiers liked it.

## Official data from Python

```python
import asyncio
from ultimatescrape.sources.clients import StatsClient

async def main():
    async with StatsClient() as c:
        print(await c.world_bank("SP.POP.TOTL", "USA;BRA", date="2022:2023"))
        print(await c.us_census(["B01003_001E"], dataset="2023/acs/acs5", for_geo="state:*"))
        print(await c.eurostat("demo_pjan", geo="DE", sex="T", age="TOTAL"))
        print(await c.gleif_lei("Stripe"))          # free global entity resolution
        print(await c.companies_house("Acme"))      # needs COMPANIES_HOUSE_API_KEY

asyncio.run(main())
```

Check `ultimatescrape/sources/registry.py` before using any source — each entry's
`gotchas` field records the specific trap. Several are severe: ISTAT blocks your IP
for 1–2 days above 5 req/min, OECD allows 60 downloads/hour and blocks VPNs, and
World Bank signals throttling as a 502 HTML page rather than a 429.

## Don't

- Don't ask a model for a statistic that a catalogued API can answer exactly.
- Don't run a large swarm without `doctor` first.
- Don't enable LinkedIn tiers beyond the default without reading
  `docs/LINKEDIN.md` — those carry account-ban risk that is the user's to accept.
- Don't present findings without their verdict. Unverified is not the same as true.

---

## Competitor and job-board intelligence

Most competitors run hiring on a standard ATS with a public JSON API, so this is
cheap, fast, and needs no keys. The competitively valuable part is **worker pay
rates**, which only the gig feeds publish.

```bash
.venv/bin/uscrape platforms                    # 18 competitors and how each is reached
.venv/bin/uscrape jobs --pay-only              # only listings with a published rate
.venv/bin/uscrape jobs --gigs                  # worker gigs, not corporate roles
.venv/bin/uscrape jobs -p imerit -p appen -f xlsx
```

Rate data quality varies by platform and is recorded per listing in `pay_source`:
`structured` came from a dedicated field, `parsed` was extracted from prose. When
reporting rates to the user, say which. Rates without a `pay_unit` were refused
deliberately — a bare number is more often a version or a headcount than a salary.

iMerit publishes rates in **local currency, not USD** (`pay_currency` is `local`),
which is what makes it the clearest view of geographic rate arbitrage. Do not
compare those figures to USD rates without saying so.

## GA4 analytics

Fourteen named reports so nobody has to know API field names. No Google SDK, so
it works unchanged on Windows.

```bash
.venv/bin/uscrape ga4 reports                  # the catalogue, with the question each answers
.venv/bin/uscrape ga4 properties               # which properties this credential can see
.venv/bin/uscrape ga4 run geo --days 28        # engagement by country and city
.venv/bin/uscrape ga4 run devices -f xlsx      # device and OS mix
.venv/bin/uscrape ga4 run device-by-country    # does device preference differ by market?
.venv/bin/uscrape ga4 fields -s engagement     # valid field names
```

Report `geo` and `country` sort by **engaged sessions**, not raw users — raw user
counts rank by population and bot traffic. If the user asks "where do we get the
most engagement", that is the right report and the right sort.

Two things to surface rather than hide: if a result comes back `sampled`, say so,
because totals are then estimates. And `(not set)` rows are real GA4 values
meaning "unknown", not errors.

## Knowledge graph

Accumulates across runs, so check it before commissioning new research — the
answer may already be there and free.

```bash
.venv/bin/uscrape graph stats
.venv/bin/uscrape graph search "Scale"
.venv/bin/uscrape graph show "Scale AI" --depth 2 --provenance
.venv/bin/uscrape graph export -f mermaid       # paste-able diagram
.venv/bin/uscrape graph ingest latest --llm     # add relations stated in prose
```

Swarm runs auto-ingest. Edge `weight` is a corroboration count — how many
independent times the relation was observed. `--provenance` answers "says who?"
with the run, agent and URL behind each claim.

## Output formats

Set once in `uscrape.toml` under `[output] formats`, and every command honours it.
Override per invocation with `-f/--format`. Available: `markdown`, `json`,
`jsonl`, `csv`, `xlsx`, `html`, `mermaid`.

CSV is written with a UTF-8 BOM so Excel on Windows shows accents correctly. If
the user is going to open results in Excel, prefer `xlsx` — it splits by verdict,
freezes headers, and adds autofilters.

## Publishing to SharePoint

```bash
.venv/bin/uscrape publish latest --dry-run     # verify auth, site and folder first
.venv/bin/uscrape publish latest               # prompts before uploading
```

**Always dry-run first**, and never pass `--yes` on the user's behalf. Uploading
is outward-facing and visible to colleagues; that confirmation is theirs to give.
The Azure app holds `Sites.Selected`, so only explicitly granted sites work — a
403 means the site was never granted, not that the path is wrong.

## Configuration

`uscrape.toml` at the repo root is the user's control panel: output formats,
watched competitors, graph on/off, SharePoint destination, GA4 default property,
swarm defaults. Environment variables override it. If the user wants different
default behaviour, edit that file rather than passing flags every time.

## API keys

`docs/API_KEYS.md` covers every key with its signup URL and what breaks without
it. The two worth knowing by heart: **OpenRouter** is the only key needed to use
the system at all, and **`CENSUS_API_KEY` is now mandatory for every US Census
query** — the old keyless tier is gone, and a keyless call redirects to an HTML
page that looks like a network error.
