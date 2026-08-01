# API keys — where to get them and how to set them

Every key below is **free** unless marked otherwise. You do not need most of them:
41 of the 61 catalogued data sources work with no key at all. Start with none,
run `uscrape doctor`, and add only what a specific job asks for.

## How keys are loaded

Precedence, first hit wins:

1. the process environment
2. `./.env` in the project directory
3. any file listed in `USCRAPE_ENV_FALLBACKS`

That third rule lets you point at an existing `.env` elsewhere on the machine
rather than copying secrets into a second file. Separate multiple paths with the
platform separator — `;` on Windows, `:` on macOS and Linux.

```bash
cp .env.example .env      # then edit
```

Check what resolved:

```bash
uscrape doctor            # LLM, channels, and which sources are ready
uscrape sources --ready   # only the sources usable right now
```

---

## 1. The one key you actually need

### OpenRouter — powers every research agent

| | |
|---|---|
| Variable | `OPENROUTER_API_KEY` |
| Get it | <https://openrouter.ai/keys> |
| Cost | Pay as you go. Kimi K2.6 runs about **$0.066 per research agent**. |

Sign in with Google or GitHub, click **Create Key**, add credit. Nothing else in
the system requires payment.

---

## 2. Census and statistics keys

### US Census Bureau — **required as of 2026**

| | |
|---|---|
| Variable | `CENSUS_API_KEY` |
| Get it | <https://api.census.gov/data/key_signup.html> |
| Time | Instant. Key arrives by email. |

Fill in organisation name and email, submit, click the activation link. The email
domain must end in `.com`, `.net`, `.org`, `.gov` or `.edu`.

**This is now mandatory for every query.** The widely-cited "500 requests a day
without a key" tier no longer exists — a keyless call redirects to a "Missing Key"
page, which looks like a network error rather than an auth failure.

```bash
uscrape census --var B01003_001E --var B19013_001E --for "state:*"
```

### Bureau of Labor Statistics — US employment and wages

| | |
|---|---|
| Variable | `BLS_API_KEY` |
| Get it | <https://data.bls.gov/registrationEngine/> |
| Limits | 500 queries/day registered, versus 25 unregistered |

Registration expires periodically and needs renewing — a key that worked last
quarter may simply stop.

### FRED — 800,000 economic series

| | |
|---|---|
| Variable | `FRED_API_KEY` |
| Get it | <https://fredaccount.stlouisfed.org/apikeys> |

Create a free account, then **My Account → API Keys → Request API Key**.

### Bureau of Economic Analysis — GDP, trade, regional income

| | |
|---|---|
| Variable | `BEA_API_KEY` |
| Get it | <https://apps.bea.gov/api/signup/> |

A keyless BEA request returns HTTP 200 with an **empty body** rather than an
error, so a missing key looks like "no data".

### UK Companies House — company registry, officers, beneficial ownership

| | |
|---|---|
| Variable | `COMPANIES_HOUSE_API_KEY` |
| Get it | <https://developer.company-information.service.gov.uk/manage-applications> |
| Limits | 600 requests per 5 minutes |

Register, **Create an application**, then **Create new key** → REST API.

The auth shape catches everyone: it is **HTTP Basic with the key as the username
and an empty password**, not a Bearer token. UltimateScrape handles this for you.

### Others, in rough order of usefulness

| Source | Variable | Sign-up |
|---|---|---|
| UN Comtrade (trade flows) | `COMTRADE_API_KEY` | <https://comtradedeveloper.un.org/> |
| Japan e-Stat | `ESTAT_APP_ID` | <https://www.e-stat.go.jp/api/> |
| France INSEE / Sirene | `INSEE_API_KEY` | <https://portail-api.insee.fr/> |
| Germany Destatis | `DESTATIS_API_TOKEN` | <https://genesis.destatis.de/datenbank/online/> |
| Mexico INEGI | `INEGI_API_TOKEN` | <https://www.inegi.org.mx/app/desarrolladores/generatoken/Usuarios/token_Verify> |
| India data.gov.in | `DATA_GOV_IN_API_KEY` | <https://data.gov.in/user/register> |
| South Korea KOSIS | `KOSIS_API_KEY` | <https://kosis.kr/openapi/> (Korean only) |
| New Zealand Stats NZ | `STATS_NZ_API_KEY` | <https://portal.apis.stats.govt.nz/how-to-subscribe> |
| Poland GUS | `GUS_BDL_API_KEY` | <https://api.stat.gov.pl/Home/BdlApi> (optional — raises quota) |
| WTO | `WTO_API_KEY` | <https://apiportal.wto.org/signup> |
| Australia ABN Lookup | `ABN_LOOKUP_GUID` | <https://abr.business.gov.au/Tools/WebServices> |
| ABS Indicator API | `ABS_INDICATOR_API_KEY` | Email a form to api.data@abs.gov.au |

### One key that is not a key

`SEC_USER_AGENT` is not a credential. The SEC blocks requests without a
descriptive User-Agent carrying a contact address, so set it to something like
`UltimateScrape research (you@company.com)`. Anonymous requests get a 403.

### Paid, and usually avoidable

**OpenCorporates** (`OPENCORPORATES_API_KEY`) has the widest cross-jurisdiction
company coverage but starts around £225/month with no general free tier — its
docs still describe an old free tier that no longer exists. Before paying, try
**GLEIF** (free, no key, 3.4M entities with an ownership graph and local registry
IDs), **Norway's Brønnøysund** (free, includes officers), and
**recherche-entreprises.api.gouv.fr** (free, French companies).

---

## 3. GA4 — analytics for the market research team

Three values, all strings. No key file, no `gcloud`, no Google SDK — which is why
this works unchanged on Windows.

```bash
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REFRESH_TOKEN=...
GA4_PROPERTY_ID=123456789
```

To mint a refresh token:

1. In Google Cloud console, pick the project holding the OAuth consent screen.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID →
   Desktop app.**
3. Enable the **Google Analytics Data API** and **Admin API** for that project.
4. Run the consent flow once with scope
   `https://www.googleapis.com/auth/analytics.readonly` and keep the
   `refresh_token` it returns.

Then:

```bash
uscrape ga4 properties          # which properties this credential can see
uscrape ga4 reports             # the report catalogue
uscrape ga4 run geo --days 28   # engagement by country and city
uscrape ga4 run devices -f xlsx # device and OS mix, as a spreadsheet
uscrape ga4 fields -s device    # valid field names, so nobody has to guess
```

**Read this before sharing the token with the team.** A refresh token
authenticates as *one person*. GA4's audit log attributes every query to whoever
minted it, and handing it round means handing round a personal Google credential.
It also breaks the moment that person changes their password, and expires after
roughly six months unused. Two cleaner options, both console actions:

- Keep the credential server-side and give the team a bearer token to a shared
  service — the arrangement the internal MCP server already uses.
- Grant a service account **Viewer** on the property in **Admin → Property Access
  Management**, then share that non-personal key instead.

---

## 4. SharePoint publishing

Uses an Azure AD app registration with **Microsoft Graph** application permissions
(`Sites.Selected` plus `Files.ReadWrite.All`). If your organisation already has one
for SharePoint automation, reuse it — no new IT request is needed:

```bash
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
```

Pull the current values rather than copying them by hand:

```bash
vercel env pull .env.production   # or however your team stores them
grep -E '^AZURE_(CLIENT_ID|TENANT_ID|CLIENT_SECRET)=' /tmp/p
```

Then point `uscrape.toml` at a site and check access before uploading:

```toml
[sharepoint]
enabled = true
site_host = "yourtenant.sharepoint.com"
site_path = "/sites/YourSite"
folder = "Active Projects/Research/UltimateScrape"
```

```bash
uscrape publish latest --dry-run    # verifies auth, site and folder
uscrape publish latest              # asks before uploading
```

**Two constraints worth knowing up front.** The app registration holds
**Sites.Selected**, not `Sites.ReadWrite.All`, so it can only touch sites an admin
has explicitly granted — an ungranted site returns 403 no matter how correct the
path. Writes have been proven against **YourSite**; the YourOtherSite
grant may be read-only, so test with a throwaway file first. Separately, the
client secret **expires 2026-12-05**.

---

## 5. LinkedIn

Read [`LINKEDIN.md`](LINKEDIN.md) first — the self-hosted tiers carry real
account-ban risk. Nothing here is required for the rest of the system.

| Tier | Variables | Notes |
|---|---|---|
| `vendor` | `PROXYCURL_API_KEY` | Paid per record, no account risk, the only tier that scales |
| `mcp` | `LINKEDIN_MCP_URL` | Self-hosted stickerdaniel/linkedin-mcp-server |
| `parser` | `LINKEDIN_PROFILE_DIR` or `LINKEDIN_LI_AT` | Highest risk; not in the default tier list |
| `jina` | none | Public pages only |

Prefer `LINKEDIN_PROFILE_DIR` over a raw cookie — LinkedIn invalidates an `li_at`
moved across an OS or fingerprint boundary, so a cookie copied from your desktop
will not authenticate inside a container.

---

## 6. Optional search providers

The research agents use OpenRouter's built-in web search, so these are only
needed for the client-side search fallback:

| Provider | Variable | Free tier |
|---|---|---|
| Tavily | `TAVILY_API_KEY` | <https://tavily.com> — 1,000 searches/month |
| Brave | `BRAVE_SEARCH_API_KEY` | <https://brave.com/search/api/> — 2,000/month |
| Exa | `EXA_API_KEY` | <https://exa.ai> |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Census returns HTML or a redirect | Missing `CENSUS_API_KEY` — now mandatory |
| BEA returns 200 with an empty body | Missing `BEA_API_KEY` |
| Companies House 401 with a valid key | Sent as Bearer; it must be Basic-auth username |
| SEC returns 403 | `SEC_USER_AGENT` not set to a contactable string |
| GA4 `invalid_grant` | Refresh token expired, revoked, or password changed |
| GA4 403 on a property | The token's account lacks Viewer on that property |
| SharePoint 403 on a valid path | Site not granted under Sites.Selected |
| World Bank 502s under load | Throttling — it signals as 5xx, never 429. Slow down. |
| ISTAT stops responding for a day | You exceeded 5 requests/minute; it IP-blocks for 1–2 days |

Per-source quirks are recorded in the `gotchas` field of each entry in
`ultimatescrape/sources/registry.py`. Read it before building on a source:

```bash
uscrape sources --country Germany --json
```
