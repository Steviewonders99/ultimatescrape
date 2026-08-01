# UltimateScrape — design

*2026-08-01*

## Problem

Five swarm-research systems already existed across three sibling repositories.
They work, and each has produced real deliverables. But every capability lives in
exactly one of them:

| Capability | Only exists in |
|---|---|
| Retry with backoff | `census_language_swarm_kimi.py` |
| Model fallback chain | `census_language_swarm_kimi.py` |
| Consensus / majority voting | `census_language_swarm_kimi.py` |
| Key pooling for rate limits | `nim_key_pool.py` |
| Credit pre-flight | `openrouter_search.py` |
| TTL cache with failure-gated writes | `cultural_research.py` |
| Resumable job queue | `supervisor.py` |
| Checkpoint/resume via `ONLY_FAILED` | `swarm_research.py` |
| URL liveness validation | `swarm_research.py` |
| Structured JSON output contracts | `channel_recommender.py` |

Nothing anywhere accumulates cost in dollars, enforces a token budget, or
checkpoints partial fan-out results — so a crash mid-run loses everything except
in one Postgres-backed pipeline. And the four separate web-fetch implementations
found across those repos share a common shape: no retries at all, no per-host
limits, no robots handling, no markdown output, and a promised headless-browser
tier that was never built.

## Goal

One standalone system that consolidates what works, adds what is missing, and is
drivable by Claude Code programmatically — targeted at company, market, and
supplier research, with LinkedIn and official statistics as first-class sources.

## Non-goals

- Not a general-purpose crawler competing with Crawl4AI. We *use* Crawl4AI.
- Not a LinkedIn data product. LinkedIn is one channel with honest tiering and an
  explicit risk statement, not the centre of gravity.
- Not a replacement for the existing pipelines. Those keep running; this is where
  new research work goes.

## Decisions

### 1. Targets × dimensions, not one big prompt

A run's work matrix is the product of entities and research angles. Forty targets
by eight dimensions is 320 agents, each asking one narrow question about one
subject.

This is the single largest quality lever. A prompt asking about forty countries
returns evenly-hedged prose; 320 small prompts return specifics. It also makes
every unit independently cacheable, retryable, skippable via `activates_when`, and
cheap enough that failure is uninteresting.

*Rejected:* a single large prompt per target. Cheaper in calls, materially worse in
output, and a single failure loses the whole target.

### 2. Models judge; Python computes

LLMs produce mappings, judgements, and prose. Every number, every deduplication,
and every URL check is computed in code and asserted.

Taken from `census_language_swarm_kimi.py`, which was the most reliable predecessor
precisely because it never let a model do arithmetic — three mapper agents voted on
a column→language mapping, Python cross-derived a fourth vote from the labels, and
then Python did all the maths and asserted the totals.

### 3. Deterministic URL validation before adversarial verification

Every URL an agent claims is liveness-checked with a real GET (not HEAD — WAFs
block HEAD). It is free, deterministic, and catches the failure mode that does the
most damage: confidently cited sources that do not exist. The vendor swarm found
hallucinated URLs frequent enough to poison a whole bench.

A dead URL then **overrides** any amount of verifier agreement. Model consensus is
weaker evidence than a 404.

### 4. Verification by distinct lenses, not repeated skeptics

Each verifier gets a different lens — factual accuracy, recency, specificity.
Three identical skeptics agree with each other; three different ones catch
different failure modes. Majority decides, and the default is skepticism.

### 5. Checkpoint per work unit, resume by re-dispatching gaps

Each unit is written atomically to `runs/<id>/units/<key>.json` the moment it
completes. Resume re-reads the directory and re-dispatches only missing or errored
units. Generalises `ONLY_FAILED=1` from the vendor swarm.

`safe_key()` always appends a hash: slugging alone collides ("United States" and
"united states" become one file) and silently overwrites results.

The manifest stores the **whole** spec, prompts included, so any run can be
rebuilt — not just ones built from a named recipe. The first cut stored dimension
*names* only, which meant resume worked for built-in recipes and failed for custom
specs, i.e. it failed for exactly the long, expensive, one-off runs that most need
it.

`activates_when` is a callable and cannot be serialised, so the manifest records
the *outcome* instead: the list of unit keys the plan actually produced. A resumed
run is restricted to those keys and therefore reproduces the original matrix
exactly, without needing the predicate.

### 6. A shared ledger with a hard ceiling

Every agent writes usage to one `Ledger`, which raises `BudgetExceeded` the moment
the ceiling is crossed. Existing swarms print a total at the end, which cannot stop
a runaway. Overrun is capped at one call.

### 7. Sources as data, protocols as code

Sixty statistics agencies share about five protocols. The registry describes
agencies as data (base URL, auth, coverage, **gotchas**) and the client implements
PxWeb 1.x, PxWeb 2.0, SDMX, OData, and the Census matrix shape. Sixty bespoke
clients would rot on first contact with a renumbered table.

The `gotchas` field is load-bearing, not documentation garnish — these APIs are
uniformly strange and rediscovering each quirk costs an afternoon.

### 8. Channels with tiered backends and health checks

From Agent-Reach, which is worth mining for this pattern and little else. A channel
declares which URLs it handles and an ordered backend list; the dispatcher routes
and the channel walks tiers until one succeeds. Access becomes configuration, and
`doctor` reports liveness before a run rather than during it.

### 9. LinkedIn: our browser, their parsers

Agent-Reach does not scrape LinkedIn itself — it routes to
`stickerdaniel/linkedin-mcp-server`, which is therefore the better direct
dependency and the recommended free tier.

`joeyism/linkedin_scraper` v3 is async Playwright with an excellent ~1,100-line
profile parser and a browser layer with no stealth whatsoever. So we drive our own
Patchright persistent context and hand the `Page` to their scrapers. Three upstream
defects are patched at runtime, chief among them issue #277, which makes
`detect_rate_limit()` fire on every page and is unfixed as of 3.1.2.

*Rejected:* using their `BrowserManager`. Plain Chromium with `navigator.webdriver`
true, against a burner account on a proxy, gets checkpointed immediately.

### 10. HTTP first, browser on evidence

A headless browser is ~100× the cost of a GET in time and memory, so escalation is
triggered by an empty body, a JS shell, or a 403 — not used by default. When no
browser tier is installed, the HTTP result is returned as-is: a genuinely short
page and a hard 404 are legitimate answers, and reporting them as "browser not
installed" hides what happened.

## Bugs found and fixed during the build

Recorded because each was a silent-corruption class, not a crash:

- **JSON-LD could never fire.** `extract()` decomposed `<script>` tags before
  harvesting JSON-LD, disabling the highest-quality extraction path entirely.
  Caught by a test, not by inspection.
- **Hardcoded `Accept-Encoding: gzip, deflate, br`** made servers send brotli that
  httpx could not decode. Bodies arrived as binary garbage that still parsed as
  "HTML". httpx advertises exactly what it can decode; overriding it is always
  wrong.
- **World Bank throttling misdiagnosed twice.** Bursts return 502 HTML pages and
  read timeouts rather than 429, so valid URLs fail in ways that look like
  malformed requests. I wrongly concluded first that the `date=2022:2023` colon
  must stay unencoded, then that the `USA;BRA` separator was blocked — and
  implemented a per-country fan-out on the second wrong theory. Spaced re-testing
  showed both forms are fine and pacing was the whole issue. The fan-out was
  reverted; it would have made the throttling worse.

The third is the useful lesson: an API that signals rate limiting as a 5xx invites
exactly this mistake, and the fix is to re-test with spacing before believing any
theory about URL syntax.

### 11. Competitor boards: ATS first, browser last

Almost every AI-data competitor runs corporate hiring on a standard ATS with a
public JSON API. One adapter per ATS covers nine companies with no scraping. The
custom endpoints are the valuable ones because they publish **worker pay rates**;
corporate boards mostly do not.

Board tokens are recorded as data because most are not derivable from the company
name (Invisible is `agency`, Sama is `samainc`). Two verified traps are encoded
rather than left as folklore: SmartRecruiters returns HTTP 200 for tokens that do
not exist, and Workable returns 200 with a real company name and an empty array
for dormant accounts. Both must be gated on the result count, never the status.

Pay parsing distinguishes `structured` from `parsed` and refuses ambiguous bare
numbers outright. In a competitive pricing comparison a wrong rate is worse than a
blank one, because it is indistinguishable from a real one.

### 12. Knowledge graph: deterministic first, LLM second

Entities and relations derivable from finding *fields* are extracted with no model
call and no chance of hallucination. Only relations stated in prose go to an LLM,
and that pass never overwrites the deterministic one — so a model failure costs
richness, never correctness.

Observations are append-only and every edge carries the run, agent and URL behind
it. A graph storing only the current best answer cannot say why it believes
something and cannot be corrected when a source turns out to be wrong.
Re-observing an edge increments its weight, making weight a corroboration count.

Entity keys include the type, because a company called Handshake and a product
called Handshake are different things and merging them corrupts every count.

### 13. GA4 without the Google SDK

`google-analytics-data` adds ~40 MB of protobuf dependencies and a native-build
failure mode. Both halves of Google's auth are plain HTTP, so the client is httpx
against `oauth2.googleapis.com/token` and the REST Data API. The result runs
unchanged on a Windows laptop with no `gcloud` and no key file, which was the
actual requirement.

Reports are named recipes answering questions rather than parameter sets, so the
team never has to know that "engaged sessions by city" is
`dimensions=[country,city] metrics=[engagedSessions]`.

### 14. One row model for every output

Findings, job listings, GA4 rows and graph edges all become the same `Dataset`.
Without that, a research team receives a differently-shaped spreadsheet from every
command and rebuilds them all by hand. CSV carries a UTF-8 BOM because the
audience opens CSVs in Excel on Windows, where its absence renders accents as
mojibake.

Format selection belongs to the repo owner in `uscrape.toml`, not to each command.
A failed format is reported and skipped rather than aborting the others — losing
the xlsx export should not cost you the markdown report.

### 15. Publishing is never a side effect

SharePoint upload is disabled by default and always an explicit command with a
confirmation prompt. Finishing a research run does not publish anything. The
existing Azure app registration holds `Sites.Selected`, so `preflight()` checks
auth, site and folder before any upload — a batch that fails halfway leaves a
partially published folder, which is worse than not starting.

## Structure

```
ultimatescrape/
  config.py        env-driven settings, model registry, pricing, .env fallbacks
  llm/             Kimi client (fallback chain, empty-content recovery), ledger, JSON recovery
  fetch/           polite HTTP (retries, robots, per-host gates), extraction, Crawl4AI tier
  channels/        base/registry, web (http→browser), linkedin (4 tiers + worker)
  swarm/           spec, prompts, merge, orchestrator, recipes
  sources/         base, registry (61 sources), clients (5 protocols)
  store/           run directories with atomic checkpoints, markdown reports
  cli.py           typer CLI
```

## Verification

23 tests, no network required, covering extraction, merge semantics, budget
enforcement, spec expansion, and resume. Live-verified end to end: a 2×2 swarm
produced 22 raw findings → 11 deduped, 11/11 URLs validated, synthesis with sourced
numbers, for $0.26. World Bank, Eurostat, GLEIF, and US Census clients all return
real data.

## Open items

- Bright Data and Apify vendor tiers are stubbed; they need a job-poll loop.
- Runs created before prompt serialisation cannot be resumed and say so explicitly
  rather than half-rebuilding.
- No persistent cache across runs. The 7-day TTL pattern from `cultural_research.py`
  with its failure-gated writes is the model to copy when this becomes a cost issue.
