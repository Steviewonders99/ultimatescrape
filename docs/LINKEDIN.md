# LinkedIn access

Read this before enabling any tier beyond the defaults.

## The legal position

LinkedIn's User Agreement prohibits automated access. Accounts detected using
automation get restricted or permanently banned, and that is a routine outcome
rather than an edge case. *hiQ v. LinkedIn* established that scraping **public**
data is not a CFAA violation in the US, but it did not make it a contract
violation any less of one — LinkedIn won the breach-of-contract claim. Anything
behind a login is squarely outside what that case protects.

Two practical consequences:

- Use a burner account you can afford to lose, never a real identity or a company
  page you depend on.
- If the data matters commercially, the paid vendor tier is the only route that is
  both ToS-clean and reliable. The self-hosted tiers are for exploration.

## Tier chain

Configured with `USCRAPE_LINKEDIN_TIERS`, default `vendor,mcp,parser,jina`. Each
tier is skipped when unconfigured, so removing a key degrades the chain rather
than failing it. That is why `parser` can sit in the default list safely: with no
`LINKEDIN_PROFILE_DIR` and no `LINKEDIN_LI_AT` it reports `unconfigured` and is
never reached, so installing this repo never touches LinkedIn on its own.

### 1. `vendor` — Proxycurl / Bright Data / Apify

Costs money per record, carries no account risk, and is the only tier that
actually scales. If LinkedIn data is load-bearing for a deliverable, start here and
stop reading.

### 2. `mcp` — stickerdaniel/linkedin-mcp-server

Currently the strongest self-hosted option, and the recommended free tier. It
drives **Patchright** (undetected Chromium) against a persistent per-account
profile, ships first-class proxy support, and can import a real browser session
with `--import-from-browser`. Critically, it is the only self-hosted backend whose
`get_company_employees` and `search_people` actually work.

```bash
# run it, then:
export LINKEDIN_MCP_URL=http://localhost:3000/mcp
```

### 3. `parser` — our browser, their parsers

In the default tier list but inert until you give it a session. To enable:

```bash
uv pip install -e ".[linkedin]"
patchright install chromium
export LINKEDIN_PROFILE_DIR=~/.uscrape/linkedin-profile-acct1
```

`joeyism/linkedin_scraper` is widely recommended and widely misunderstood. **v3.0.0
(Jan 2026) was a complete rewrite to async Playwright + Pydantic.** The API that
most tutorials and LLM training data describe — `actions.login(driver, ...)`,
`Person(url, driver=driver)`, `JobSearch`, the `CHROMEDRIVER` env var — exists only
in `2.11.5` and earlier. Writing against it today produces code that cannot import.

What it is genuinely good at is **parsing**. Its `PersonScraper` is ~1,100 lines
that walk the `/details/experience/`, `/details/education/`, interests,
accomplishments and contact-info sub-pages, and it is the best free profile parser
available. What it is bad at is **browsing**: `BrowserManager` launches plain
Chromium with no stealth at all, so `navigator.webdriver` is `true` and the profile
is ephemeral.

So we take the parsers and supply our own browser. `_linkedin_parser_worker.py`
drives a Patchright persistent context and hands the resulting `Page` straight to
`PersonScraper` / `CompanyScraper`, which cannot tell the difference because
Patchright is a drop-in Playwright replacement.

Three upstream defects are handled in that worker.

**Issue #277 — `detect_rate_limit()` false-positives on every page.** Verified
against 3.1.2: it reads `body.text_content()`, which includes text from
unrendered nodes, and matches the phrase `"try again later"` — which LinkedIn
embeds inside the serialized React payload of ordinary pages. Every scrape
therefore raises `RateLimitError` before it reads anything.

Our patch reads `inner_text()` instead, so only rendered text counts, and drops
that one over-broad phrase while keeping `too many requests`, `rate limit` and
`slow down`. Two details make the naive fix wrong, both of which cost us a
rewrite:

- `detect_rate_limit(page) -> None` **raises**; it does not return a bool. A
  replacement returning True/False silently disables detection entirely,
  including the legitimate checkpoint and CAPTCHA checks.
- Call sites do `from .utils import detect_rate_limit`, binding the name at
  import time, so patching `utils` alone reaches nobody. The real sites are
  `core.auth` and `scrapers.base`, and both must be imported before rebinding.

`tests/test_linkedin_parser.py` pins all of this, and the fix was verified by
reproducing the bug against a local page shaped like LinkedIn's: upstream raises,
patched does not, and a genuinely blocked page still raises.

The other two defects:

- **`Company.employees` is never populated in v3.** The field exists on the model
  and nothing writes to it — there is no `/people/` navigation anywhere in the
  package. We scrape it ourselves.
- **Company overview fields are mostly `None`.** HQ detection matches against a
  hardcoded six-item list of US/UK locales; industry matches eight keywords;
  `founded`, `specialties`, `company_type` and `phone` come only from a legacy
  fallback that runs solely when everything else came back empty.

Because selector drift degrades fields to `null` rather than raising, the channel
treats an empty-looking record as a **miss** and falls through to the next tier.
Silent nulls are this backend's documented failure mode, and quiet data rot is
worse than a loud failure.

### 4. `jina` — public pages via r.jina.ai

No auth, no risk, very little data. The channel checks for auth-wall stubs ("Sign
in to view", "Join LinkedIn") and treats them as misses, because reporting a
login wall as a successful fetch would poison the dataset with empty records.

## Sessions and proxies

This is the part that decides whether anything above works.

**Prefer a persistent profile over a raw cookie.** LinkedIn now invalidates a
`li_at` cookie moved across an OS or fingerprint boundary — a cookie lifted from
macOS Chrome will not authenticate inside a Linux container. This is upstream issue
#279, and the linkedin-mcp-server maintainer confirms it there: `storage_state`
carries only cookies and localStorage, while a real returning browser also has HTTP
cache, service workers and HSTS state, all of which are now fingerprinted. Chromium
also encrypts profile cookies with an OS-specific key.

```bash
export LINKEDIN_PROFILE_DIR=~/.uscrape/linkedin-profile-acct1
export USCRAPE_LINKEDIN_HEADLESS=false   # once, headed, to clear the first checkpoint
```

**Use one dedicated static ISP address per account, and set the proxy up *before*
the session is created.** Moving an already-logged-in session onto a new IP is
itself a checkpoint trigger. A rotating residential pool is therefore actively
worse than no proxy at all here — the opposite of the usual scraping intuition.

**Checkpoints cannot be automated.** 2FA and CAPTCHA challenges require a human at
a headed browser. Every backend here detects them and stops; none solve them.

## What breaks first, in order

1. The `detect_rate_limit()` false positive — aborts on page one, before volume is
   a factor. Patched in our worker.
2. A checkpoint on the burner account — hard stop, needs a human.
3. Session invalidation when a profile crosses a machine or OS boundary — a worker
   fleet silently deauthenticates.
4. Selector drift — fields quietly become `null` rather than raising, so data
   quality rots for weeks before anyone notices. Assert field presence.
5. Pacing — none of these libraries throttle, jitter, or budget per account. That
   is yours to build, and the channel's subprocess-per-record model is where it
   goes.
