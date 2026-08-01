# Getting started — Windows

Written for a Windows machine. Every command below is PowerShell. Total setup time
is about ten minutes, most of it waiting for downloads.

---

## 1. Install

Open **PowerShell** (not Command Prompt) and run:

```powershell
git clone https://github.com/Steviewonders99/ultimatescrape.git
cd ultimatescrape
.\setup.ps1
```

That script installs `uv` if you don't have it, creates the virtual environment,
installs everything, copies `.env.example` to `.env`, and runs a health check.
It's safe to re-run at any point.

If PowerShell refuses to run the script, allow local scripts once:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### If you'd rather do it by hand

```powershell
winget install Python.Python.3.13          # if you don't have Python 3.11+
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv venv --python 3.13
uv pip install -e ".[dev,export]"
Copy-Item .env.example .env
```

### Activating the environment

Everything lives in `.venv`. Either activate it once per terminal:

```powershell
.\.venv\Scripts\Activate.ps1
uscrape doctor
```

…or prefix each command, which always works:

```powershell
.\.venv\Scripts\uscrape.exe doctor
```

The rest of this guide assumes you've activated.

---

## 2. Try it without any API key

Two commands work immediately, with no credentials at all. Start here — they
prove the install is good and they're genuinely useful.

```powershell
uscrape platforms
uscrape jobs -p imerit -p appen -p handshake --pay-only
```

That pulls live competitor listings and their published pay rates, and writes
markdown, JSON and CSV into `runs\exports\jobboards\`.

You should see something like Handshake paying $125/hr for specialist AI-trainer
contract roles against iMerit's $3–20/hr — iMerit's rates are in **local
currency**, which is what makes that comparison interesting rather than alarming.

```powershell
uscrape sources --ready       # 41 official statistics APIs usable with no key
```

---

## 3. Add the one key that matters

Open `.env` in any editor and set:

```
OPENROUTER_API_KEY=sk-or-v1-...
```

Get it from <https://openrouter.ai/keys> — sign in, **Create Key**, add credit.
This is the only key needed to use the research engine. Budget roughly **$0.07
per research agent**; a three-company sweep is about $1.60.

Then:

```powershell
uscrape doctor
```

You should see your balance and a green LLM row.

Everything else is optional. `docs\API_KEYS.md` lists every other key, where to
get it, and what breaks without it. The one worth adding early is
`CENSUS_API_KEY` (free, instant, from <https://api.census.gov/data/key_signup.html>)
because as of 2026 the US Census API rejects **every** unkeyed request.

---

## 4. Your first research run

```powershell
uscrape company "Scale AI" "Appen"
```

That's 2 companies × 8 research dimensions = 16 agents, each asking one narrow
question, then deduplicated, URL-checked, adversarially verified, and synthesised.
Expect **8–15 minutes** and about **$1–2**.

Watch for the run id it prints. If you need to stop it, Ctrl-C is safe — every
agent checkpoints to disk as it finishes:

```powershell
uscrape resume latest
```

That re-dispatches only the agents that never completed. You never pay twice.

Results land in `runs\<run_id>\`:

| File | What it is |
|---|---|
| `*.md` | the readable report |
| `*.csv` | opens cleanly in Excel |
| `findings.json` | machine-readable, with verdicts |
| `units\*.json` | one file per agent — the resume unit |

**Read the verdicts.** Every finding carries `_verdict` (`supported` / `refuted` /
`unverified`), `_corroborations` (how many independent agents found it), and
`_url_status`. Refuted findings are kept deliberately — a refuted finding tells
you something about the topic *and* about the research.

---

## 5. The commands you'll actually use

```powershell
# Competitor intelligence — no key needed
uscrape platforms                          # who we track and how
uscrape jobs --pay-only                    # everything with a published rate
uscrape jobs --gigs                        # worker gigs, not corporate roles

# Research swarms — needs OPENROUTER_API_KEY
uscrape company "Acme" "Globex"            # 8 dimensions per company
uscrape market "Germany" "Japan" -t "AI training data"
uscrape vendor -c Brazil -c Vietnam -p "small video data collection studios"

# Official statistics — mostly no key
uscrape sources --ready
uscrape census --var B01003_001E --for "state:*"

# What we already know (accumulates across every run)
uscrape graph stats
uscrape graph search "Scale"
uscrape graph show "Scale AI" --provenance

# Analytics — needs the GA4 credentials
uscrape ga4 reports
uscrape ga4 run geo --days 28
uscrape ga4 run devices -f xlsx

# Pages
uscrape fetch https://example.com
```

Add `-f xlsx` to any command to get a spreadsheet instead. Add `--no-verify` to a
swarm to roughly halve the cost — findings then come back marked `unverified`
rather than falsely confident.

---

## 6. Configure it your way

`uscrape.toml` in the repo root is your control panel. It's plain text, commented
throughout, and safe to commit. The parts you'll want to change first:

```toml
[output]
formats = ["markdown", "json", "csv"]   # add "xlsx" if you live in Excel

[jobboards]
watch = ["scale", "appen", "handshake", "surge", "mercor", "toloka", "prolific"]

[swarm]
verifier_votes = 3      # 0 disables verification and roughly halves cost
```

Environment variables always beat the file, so you never have to edit it for a
one-off.

---

## 7. Using Claude Code to drive it

This is the fastest way to get value, and it's why the repo ships a skill file.

Install the skill once:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills\ultimatescrape" | Out-Null
Copy-Item "skills\ultimatescrape\SKILL.md" "$env:USERPROFILE\.claude\skills\ultimatescrape\SKILL.md"
```

Now open Claude Code in the repo folder and just describe what you want. The skill
teaches Claude the commands, the cost model, how to size a run, and — importantly —
how to read the verdicts so it doesn't present unverified findings as fact.

### The golden prompt

Paste this into Claude Code the first time. It gets you oriented and produces
something real in one go:

> I've just cloned UltimateScrape at `C:\path\to\ultimatescrape` and I'm on Windows.
> Use the `ultimatescrape` skill.
>
> First, run `doctor` and tell me plainly what's working, what's missing, and
> whether anything needs a key before I can be useful.
>
> Then, without spending anything, pull the competitor job boards for every
> platform that publishes worker pay rates and give me a short written brief on
> what our competitors are paying — call out the highest and lowest rates, and
> flag where the same kind of work is priced very differently by country. Note
> which rates came from a structured field versus parsed from prose, and don't
> present a parsed rate as though it were confirmed.
>
> Finally, estimate what a research swarm across our five biggest competitors
> would cost and how long it would take, and wait for me to approve before
> running it.

### Prompts that work well after that

> Research Scale AI, Appen and Surge across all eight dimensions. Verify with 3
> lenses. Give me the estimate first.

> Size the AI training data market in Germany, Japan and Brazil. I want figures
> with sources and years, and tell me where the estimates disagree rather than
> averaging them.

> Which countries and cities give us the most engaged users in the last 28 days?
> Export it as a spreadsheet.

> What do we already know about Mercor? Check the knowledge graph first before
> commissioning any new research.

> Find small local video data collection vendors in Brazil, Vietnam and Poland.
> Exclude Scale, Appen, Sama and TELUS — I want local specialists, not megavendors.

### Things worth telling Claude

- **"Give me the estimate first"** — swarms cost real money and take real time.
  The skill instructs Claude to estimate anything over ~$10 before starting, but
  saying it makes that reliable.
- **"Check the graph first"** — the knowledge graph accumulates across runs, so
  the answer may already exist for free.
- **"Don't present unverified findings as fact"** — the skill covers this, but
  it's the failure mode most worth guarding against.

---

## 8. Credentials that need setting up separately

### GA4 analytics

Three values in `.env`:

```
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REFRESH_TOKEN=...
GA4_PROPERTY_ID=...
```

Deliberately built with **no Google SDK** — no `gcloud`, no service-account JSON
file, no native builds. That's why it works on Windows without ceremony.
`docs\API_KEYS.md` §3 has the minting steps.

Once set: `uscrape ga4 properties` lists what you can see.

### SharePoint publishing

Three values, for uploading finished documents:

```
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
```

Point `uscrape.toml` at your site, then **always dry-run first**:

```powershell
uscrape publish latest --dry-run
uscrape publish latest
```

Publishing is disabled by default and always asks before uploading — finishing a
research run never publishes anything on its own.

---

## 9. Windows-specific notes

- **Paths in `USCRAPE_ENV_FALLBACKS` use `;`**, not `:` — a colon would split
  `C:\...` at the drive letter.
- **CSV files carry a UTF-8 BOM** so Excel renders accented characters correctly.
  If you open them in something else and see `ï»¿` at the start, that's why.
- **Long paths**: if you clone into a deeply nested folder and hit path-length
  errors, enable long paths:
  `New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force`
- **The browser tier is optional and heavy.** Only install it (`.\setup.ps1
  -WithBrowser`) if you hit a site that needs JavaScript. The HTTP tier handles
  most of the web.
- **Antivirus** occasionally flags Chromium downloads from the browser tier.
  That's expected if you install it.

---

## 10. When something breaks

```powershell
uscrape doctor          # always start here
```

| Symptom | Cause |
|---|---|
| `uscrape` not recognised | Environment not activated — use `.\.venv\Scripts\uscrape.exe` |
| Script blocked by policy | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| `no LLM key configured` | `OPENROUTER_API_KEY` missing from `.env` |
| Census returns HTML | `CENSUS_API_KEY` missing — it's mandatory now |
| A swarm stopped early | Budget ceiling hit. Raise `USCRAPE_MAX_RUN_COST_USD` and `uscrape resume latest` |
| GA4 `invalid_grant` | Refresh token expired or the owner changed their password |
| SharePoint 403 | The site was never granted to the app registration |

Run `uscrape doctor` and paste the output to Claude Code — it can diagnose from
that directly.

---

## Where to read more

| Document | What's in it |
|---|---|
| `README.md` | What the system is and why it's built this way |
| `docs\API_KEYS.md` | Every key, its signup URL, and its failure mode |
| `docs\LINKEDIN.md` | **Read before enabling LinkedIn** — real account-ban risk |
| `uscrape.toml` | Your configuration, commented throughout |
| `skills\ultimatescrape\SKILL.md` | What Claude Code knows about all this |
