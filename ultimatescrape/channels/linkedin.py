"""LinkedIn channel — four tiers, best-first, each independently skippable.

Read ``docs/LINKEDIN.md`` before enabling tiers 2 or 3. LinkedIn's User Agreement
prohibits automated access; the self-hosted tiers carry a real risk of the
account being restricted. This module makes that a deliberate, configured choice
rather than a default.

Tier order (``USCRAPE_LINKEDIN_TIERS``, default ``vendor,mcp,jina``):

  1. ``vendor`` — Proxycurl / Bright Data / Apify. Costs money per record,
                  carries no account risk, and is the only tier that truly scales.
  2. ``mcp``    — a locally running stickerdaniel/linkedin-mcp-server. Free,
                  moderate risk. Currently the strongest self-hosted option: it
                  drives Patchright against a persistent per-account profile and
                  is the only backend here whose ``get_company_employees`` and
                  ``search_people`` actually work.
  3. ``parser`` — our own Patchright browser handed to joeyism/linkedin_scraper's
                  parsers, in a subprocess. Its ``PersonScraper`` is the best free
                  profile parser available; its browser layer has no stealth at
                  all, so we supply our own. Free, highest fragility — it reads
                  the live DOM, so a front-end change degrades fields to null
                  silently rather than failing loudly. Not in the default tiers.
  4. ``jina``   — r.jina.ai reading whatever is publicly visible. No auth, no
                  risk, very little data. The honest floor.

Operational note that outranks the choice of library: use one dedicated static
ISP address per account and set the proxy up *before* the session is created.
Moving a logged-in session onto a new IP is itself a checkpoint trigger, so a
rotating residential pool is worse than no proxy at all here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from ..config import api_key_for, settings
from .base import BackendStatus, Channel, ChannelResult, Health

log = logging.getLogger("uscrape.linkedin")

_LINKEDIN_HOST = re.compile(r"(^|\.)linkedin\.com$", re.IGNORECASE)
_COMPANY_RE = re.compile(r"/company/([^/?#]+)", re.IGNORECASE)
_PROFILE_RE = re.compile(r"/in/([^/?#]+)", re.IGNORECASE)

WORKER = Path(__file__).with_name("_linkedin_parser_worker.py")

#: Vendors with a working adapter. Bright Data and Apify are dataset/actor-shaped
#: and need a submit-then-poll loop that is not written yet.
_IMPLEMENTED_VENDORS = frozenset({"proxycurl"})


def classify(url: str) -> str:
    if _COMPANY_RE.search(url):
        return "company"
    if _PROFILE_RE.search(url):
        return "person"
    if "/jobs/" in url:
        return "job"
    return "other"


class LinkedInChannel(Channel):
    name = "linkedin"
    backends = ("vendor", "mcp", "parser", "jina")

    def __init__(self, tiers: list[str] | None = None) -> None:
        self.tiers = tiers or settings.linkedin_tiers
        self._client = httpx.AsyncClient(timeout=60.0)

    def can_handle(self, url: str) -> bool:
        from urllib.parse import urlparse

        return bool(_LINKEDIN_HOST.search((urlparse(url).netloc or "").lower()))

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── health ────────────────────────────────────────────────────────────────

    async def health(self) -> list[BackendStatus]:
        out: list[BackendStatus] = []

        vendor = self._vendor_name()
        if not vendor:
            out.append(
                BackendStatus(
                    "vendor",
                    Health.UNCONFIGURED,
                    "set PROXYCURL_API_KEY / BRIGHTDATA_API_TOKEN / APIFY_API_TOKEN",
                )
            )
        elif vendor in _IMPLEMENTED_VENDORS:
            out.append(
                BackendStatus("vendor", Health.OK, f"using {vendor}", cost_hint_usd=0.01)
            )
        else:
            # A configured key with no implementation behind it would otherwise
            # report green while every fetch silently fell through to the next
            # tier — the exact "silent zeros read as truth" failure doctor exists
            # to prevent.
            out.append(
                BackendStatus(
                    "vendor",
                    Health.DEGRADED,
                    f"{vendor} key is set but its adapter is not implemented "
                    f"(needs a job-poll loop); requests fall through to the next tier",
                )
            )

        mcp_url = api_key_for("LINKEDIN_MCP_URL")
        if not mcp_url:
            out.append(
                BackendStatus("mcp", Health.UNCONFIGURED, "set LINKEDIN_MCP_URL", risk=True)
            )
        else:
            try:
                r = await self._client.get(mcp_url.replace("/mcp", "/health"), timeout=5.0)
                reachable = r.status_code < 500
            except Exception:  # noqa: BLE001
                reachable = False
            out.append(
                BackendStatus(
                    "mcp",
                    Health.OK if reachable else Health.UNAVAILABLE,
                    mcp_url if reachable else f"no server responding at {mcp_url}",
                    cost_hint_usd=0.0,
                    risk=True,
                )
            )

        out.append(await self._parser_health())

        out.append(
            BackendStatus(
                "jina",
                Health.OK,
                f"{api_key_for('JINA_READER_BASE') or 'https://r.jina.ai'} (public pages only)",
                cost_hint_usd=0.0,
            )
        )
        return out

    async def _parser_health(self) -> BackendStatus:
        import importlib.util

        def installed(mod: str) -> bool:
            try:
                return importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                return False

        if not installed("linkedin_scraper"):
            return BackendStatus(
                "parser",
                Health.UNCONFIGURED,
                'uv pip install "linkedin-scraper>=3.1" patchright && patchright install chromium',
                risk=True,
            )

        notes: list[str] = []
        if not installed("patchright"):
            # Stock Playwright leaves navigator.webdriver true, which LinkedIn
            # reads. Usable, but a burner account will get checkpointed fast.
            notes.append("patchright missing — falling back to detectable Playwright")
        if not (api_key_for("LINKEDIN_LI_AT") or api_key_for("LINKEDIN_PROFILE_DIR")):
            return BackendStatus(
                "parser",
                Health.UNCONFIGURED,
                "set LINKEDIN_PROFILE_DIR (preferred, survives fingerprint checks) "
                "or LINKEDIN_LI_AT",
                risk=True,
            )
        return BackendStatus(
            "parser",
            Health.DEGRADED if notes else Health.OK,
            "; ".join(notes) or "ready (own Patchright browser + upstream parsers)",
            cost_hint_usd=0.0,
            risk=True,
        )

    # ── dispatch ──────────────────────────────────────────────────────────────

    async def fetch(self, url: str, **kwargs: Any) -> ChannelResult:
        kind = classify(url)
        attempted: list[str] = []
        last_error = "no tier configured"

        for tier in self.tiers:
            handler = getattr(self, f"_via_{tier}", None)
            if handler is None:
                log.warning("unknown LinkedIn tier %r in USCRAPE_LINKEDIN_TIERS", tier)
                continue
            attempted.append(tier)
            try:
                result = await handler(url, kind, **kwargs)
                if result is not None:
                    result.attempted = attempted
                    return result
            except Exception as exc:  # noqa: BLE001
                last_error = f"{tier}: {type(exc).__name__}: {exc}"
                log.warning("[linkedin] tier %s failed for %s — %s", tier, url, exc)

        return ChannelResult(
            url=url, ok=False, backend="none", attempted=attempted, error=last_error
        )

    # ── tier 1: paid vendors ──────────────────────────────────────────────────

    @staticmethod
    def _vendor_name() -> str | None:
        for env, label in (
            ("PROXYCURL_API_KEY", "proxycurl"),
            ("BRIGHTDATA_API_TOKEN", "brightdata"),
            ("APIFY_API_TOKEN", "apify"),
        ):
            if api_key_for(env):
                return label
        return None

    async def _via_vendor(self, url: str, kind: str, **_: Any) -> ChannelResult | None:
        vendor = self._vendor_name()
        if vendor not in _IMPLEMENTED_VENDORS:
            # Falling through is correct — it degrades to the next tier rather
            # than pretending. `doctor` reports this tier as degraded so the gap
            # is visible before a run rather than inferred from thin results.
            return None
        key = api_key_for("PROXYCURL_API_KEY")
        endpoint = {
            "person": "https://nubela.co/proxycurl/api/v2/linkedin",
            "company": "https://nubela.co/proxycurl/api/linkedin/company",
        }.get(kind)
        if not endpoint:
            return None
        param = "linkedin_profile_url" if kind == "person" else "url"
        r = await self._client.get(
            endpoint,
            params={param: url, "use_cache": "if-present"},
            headers={"Authorization": f"Bearer {key}"},
        )
        r.raise_for_status()
        data = r.json()
        return ChannelResult(
            url=url,
            ok=True,
            backend="vendor:proxycurl",
            data=data,
            markdown=_summarize(data, kind),
        )

    # ── tier 2: self-hosted MCP server ────────────────────────────────────────

    async def _via_mcp(self, url: str, kind: str, **kwargs: Any) -> ChannelResult | None:
        base = api_key_for("LINKEDIN_MCP_URL")
        if not base:
            return None
        tool = {
            "person": "get_person_profile",
            "company": "get_company_profile",
            "job": "get_job_details",
        }.get(kind)
        if not tool:
            return None
        args: dict[str, Any] = {"linkedin_url": url}
        if kind == "company" and kwargs.get("with_employees"):
            args["get_employees"] = True

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        r = await self._client.post(
            base,
            json=payload,
            headers={"Accept": "application/json, text/event-stream"},
        )
        r.raise_for_status()
        body = _parse_mcp_response(r.text)
        if body is None:
            return None
        return ChannelResult(
            url=url, ok=True, backend="mcp:linkedin-mcp-server", data=body,
            markdown=_summarize(body, kind),
        )

    # ── tier 3: our Patchright browser + joeyism parsers, in a subprocess ─────

    async def _via_parser(self, url: str, kind: str, **kwargs: Any) -> ChannelResult | None:
        if kind not in ("person", "company"):
            return None
        status = await self._parser_health()
        if status.health not in (Health.OK, Health.DEGRADED):
            return None

        cmd = [sys.executable, str(WORKER), "--url", url, "--kind", kind]
        if kwargs.get("with_employees"):
            cmd += ["--employees", "--employee-limit", str(kwargs.get("employee_limit", 100))]

        # A subprocess per record because the upstream library has no concurrency
        # control of its own, and a wedged Chromium is reaped with the process
        # instead of leaking into the swarm's event loop.
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("linkedin parser worker timed out after 240s") from None

        raw = stdout.decode().strip()
        if not raw:
            raise RuntimeError(
                f"linkedin parser worker exit {proc.returncode}: {stderr.decode()[:400]}"
            )
        data = json.loads(raw)
        if data.get("error"):
            raise RuntimeError(data["error"])

        # Selector drift degrades fields to null rather than raising, so an
        # empty-looking record is treated as a miss and falls through to the next
        # tier. Silent nulls are the documented failure mode of this backend.
        if not (data.get("name") or data.get("about") or data.get("about_us")):
            raise RuntimeError("parser returned an empty record (likely selector drift)")

        return ChannelResult(
            url=url,
            ok=True,
            backend="parser:linkedin_scraper",
            data=data,
            markdown=_summarize(data, kind),
        )

    # ── tier 4: Jina Reader on public pages ───────────────────────────────────

    async def _via_jina(self, url: str, kind: str, **_: Any) -> ChannelResult | None:
        base = api_key_for("JINA_READER_BASE") or "https://r.jina.ai"
        headers = {"Accept": "text/plain"}
        if token := api_key_for("JINA_API_KEY"):
            headers["Authorization"] = f"Bearer {token}"
        r = await self._client.get(f"{base}/{url}", headers=headers)
        r.raise_for_status()
        body = r.text.strip()
        # An auth wall renders as a short "join LinkedIn" stub; that is a miss,
        # not a hit, and reporting it as success would poison the dataset.
        if len(body) < 400 or "Sign in to view" in body or "Join LinkedIn" in body:
            return None
        return ChannelResult(
            url=url,
            ok=True,
            backend="jina",
            data={"kind": kind, "source": "public_page"},
            markdown=body[:20_000],
        )


def _parse_mcp_response(text: str) -> dict | None:
    """MCP over HTTP answers as JSON or as an SSE stream depending on the server."""
    payload: dict | None = None
    if text.lstrip().startswith("{"):
        payload = json.loads(text)
    else:
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        payload = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
    if not payload:
        return None
    if "error" in payload:
        raise RuntimeError(str(payload["error"])[:300])
    content = (payload.get("result") or {}).get("content") or []
    for block in content:
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                return {"text": block["text"]}
    return payload.get("result")


def _summarize(data: dict, kind: str) -> str:
    """Compact markdown rendering so downstream agents get a uniform shape
    regardless of which tier produced the record."""
    if not isinstance(data, dict):
        return ""
    lines: list[str] = []
    if kind == "company":
        lines.append(f"# {data.get('name') or data.get('company_name') or 'Company'}")
        for key in (
            "tagline",
            "industry",
            "company_size",
            "company_size_on_linkedin",
            "headquarters",
            "founded_year",
            "founded",
            "website",
            "specialties",
            "company_type",
        ):
            if val := data.get(key):
                lines.append(f"- **{key.replace('_', ' ')}**: {val}")
        if about := data.get("about_us") or data.get("description"):
            lines.append(f"\n{about}")
        employees = data.get("employees") or []
        if employees:
            lines.append(f"\n## Employees sampled ({len(employees)})")
            for emp in employees[:50]:
                if isinstance(emp, dict):
                    lines.append(
                        f"- {emp.get('name', '?')} — {emp.get('job_title') or emp.get('title', '')}"
                    )
                else:
                    lines.append(f"- {emp}")
    else:
        lines.append(f"# {data.get('name') or data.get('full_name') or 'Profile'}")
        for key in ("headline", "occupation", "job_title", "company", "location", "city", "country"):
            if val := data.get(key):
                lines.append(f"- **{key.replace('_', ' ')}**: {val}")
        if about := data.get("about") or data.get("summary"):
            lines.append(f"\n{about}")
        for section in ("experiences", "experience", "educations", "education"):
            items = data.get(section) or []
            if items:
                lines.append(f"\n## {section.title()}")
                for item in items[:20]:
                    if isinstance(item, dict):
                        title = item.get("position_title") or item.get("title") or item.get("degree", "")
                        org = item.get("institution_name") or item.get("company") or item.get("school", "")
                        lines.append(f"- {title} — {org}")
                    else:
                        lines.append(f"- {item}")
    return "\n".join(lines)
