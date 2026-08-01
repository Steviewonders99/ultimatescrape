#!/usr/bin/env python3
"""Out-of-process LinkedIn worker: our browser, their parsers.

The design follows from what `joeyism/linkedin_scraper` v3.x actually is, which
is not what its README describes. Three facts drive every choice here:

1. v3 (Jan 2026) is a full rewrite to **async Playwright + Pydantic**. The v2
   Selenium API — ``actions.login``, ``Person(url, driver=driver)``, the
   ``CHROMEDRIVER`` env var — does not exist in any current release. Its scrapers
   take a Playwright ``Page`` you hand them: ``PersonScraper(page).scrape(url)``.
2. Its ``PersonScraper`` is the best free profile parser available — 1,100+ lines
   walking the ``/details/*`` sub-pages for experience, education, interests,
   accomplishments and the contact-info overlay. That is the part worth having.
3. Its ``BrowserManager`` is plain Chromium with no stealth at all —
   ``navigator.webdriver`` is true, and it launches an ephemeral profile. That is
   the part to throw away.

So: we drive a **Patchright** persistent context ourselves (undetected Chromium,
a per-account profile directory, a sticky proxy) and hand the resulting ``Page``
straight to their scrapers. Patchright is a drop-in Playwright replacement, so
their code cannot tell the difference.

Two upstream bugs are patched at runtime:

* Issue #277 — ``detect_rate_limit()`` reads ``body.text_content()``, which
  includes invisible React RSC payload text. LinkedIn ships the string "something
  went wrong. please try again later." inside that payload on ordinary pages, so
  every scrape raises ``RateLimitError``. Unpatched as of 3.1.2. We replace the
  check with one that reads *visible* text.
* ``CompanyScraper`` never populates ``employees`` in v3 — the field exists on the
  model and nothing writes to it. We scrape ``/people/`` ourselves when asked.

Auth prefers a ``li_at`` cookie, but note the sharp edge documented in upstream
issue #279: LinkedIn now invalidates a session cookie moved across an OS or
fingerprint boundary. A cookie lifted from macOS Chrome will not work inside a
Linux container. Use a per-account persistent profile created on the machine that
will use it.

stdin/stdout contract: argv in, one JSON object on stdout, non-zero exit on error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, default=str, ensure_ascii=False)
    sys.stdout.flush()


#: Phrases that indicate a genuine block. Upstream also matches
#: ``"try again later"``, which is the substring LinkedIn embeds in the
#: serialized React payload of ordinary pages — that one phrase is the whole of
#: issue #277, so it is deliberately absent here.
_RATE_LIMIT_PHRASES = (
    "too many requests",
    "rate limit",
    "slow down",
    "you've reached the weekly limit",
    "you have reached the weekly limit",
)


def _patch_rate_limit_detection() -> list[str]:
    """Neutralise upstream issue #277. Returns the module paths actually patched.

    Two things about the real function make the naive patch wrong, both verified
    against linkedin_scraper 3.1.2:

    1. ``detect_rate_limit(page) -> None`` **raises** ``RateLimitError``; it does
       not return a bool. A replacement that returns True/False silently disables
       detection altogether, including the legitimate checkpoint and CAPTCHA
       checks — worse than the bug it set out to fix.
    2. Callers do ``from .utils import detect_rate_limit``, binding the name into
       their own module at import time. Patching ``utils`` alone therefore
       reaches nobody. The real call sites are ``core.auth`` and
       ``scrapers.base``, and both must be imported before they can be patched.

    The fix keeps the URL-checkpoint and CAPTCHA checks exactly as upstream has
    them, and only changes the page-text check: ``inner_text()`` instead of
    ``text_content()``, so it reads rendered text rather than the serialized
    React payload, and without the over-broad "try again later" phrase.
    """
    try:
        import importlib

        from linkedin_scraper import RateLimitError
        from linkedin_scraper.core import utils
    except ImportError:
        return []

    if not hasattr(utils, "detect_rate_limit"):
        return []

    async def detect_rate_limit(page) -> None:  # type: ignore[no-untyped-def]
        url = getattr(page, "url", "") or ""
        if "linkedin.com/checkpoint" in url or "authwall" in url:
            raise RateLimitError(
                "LinkedIn security checkpoint detected. Verify the account in a "
                "headed browser before continuing.",
                suggested_wait_time=3600,
            )
        try:
            captcha = await page.locator(
                'iframe[title*="captcha" i], iframe[src*="captcha" i]'
            ).count()
            if captcha > 0:
                raise RateLimitError(
                    "CAPTCHA challenge detected. Manual intervention required.",
                    suggested_wait_time=3600,
                )
        except RateLimitError:
            raise
        except Exception:  # noqa: BLE001 - a missing frame is not a block
            pass
        try:
            body = await page.locator("body").inner_text(timeout=2000)
        except Exception:  # noqa: BLE001 - an unreadable body is not a block
            return
        lowered = (body or "").lower()
        if any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES):
            raise RateLimitError(
                "Rate limit message visible on the page.", suggested_wait_time=1800
            )

    patched: list[str] = []
    utils.detect_rate_limit = detect_rate_limit
    patched.append("linkedin_scraper.core.utils")

    # Import each call site so the name exists to overwrite, then rebind it.
    for mod_name in (
        "linkedin_scraper.core.auth",
        "linkedin_scraper.scrapers.base",
        "linkedin_scraper.scrapers.person",
        "linkedin_scraper.scrapers.company",
        "linkedin_scraper.scrapers.job",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        if hasattr(mod, "detect_rate_limit"):
            mod.detect_rate_limit = detect_rate_limit
            patched.append(mod_name)
    return patched


def _playwright():
    """Patchright when available, stock Playwright otherwise.

    Patchright is an undetected fork with an identical API. Without it LinkedIn
    sees ``navigator.webdriver`` and a HeadlessChrome user agent, and a burner
    account gets checkpointed quickly.
    """
    try:
        from patchright.async_api import async_playwright

        return async_playwright, True
    except ImportError:
        from playwright.async_api import async_playwright

        return async_playwright, False


async def _launch(pw, *, headless: bool, profile_dir: str | None, proxy: str | None):
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]
    proxy_cfg: dict[str, Any] | None = None
    if proxy:
        from urllib.parse import urlparse

        parsed = urlparse(proxy)
        proxy_cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy_cfg["username"] = parsed.username
            proxy_cfg["password"] = parsed.password or ""

    if profile_dir:
        # A persistent context carries cache, service workers and HSTS state, all
        # of which LinkedIn now fingerprints. storage_state JSON carries none of
        # it, which is why transplanted sessions get deauthed (issue #279).
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            profile_dir,
            headless=headless,
            args=args,
            proxy=proxy_cfg,
            user_agent=os.getenv("USCRAPE_USER_AGENT", DEFAULT_UA),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        return None, context

    browser = await pw.chromium.launch(headless=headless, args=args, proxy=proxy_cfg)
    context = await browser.new_context(
        user_agent=os.getenv("USCRAPE_USER_AGENT", DEFAULT_UA),
        viewport={"width": 1440, "height": 900},
        locale="en-US",
    )
    return browser, context


async def _authenticate(context, page) -> str:
    li_at = os.getenv("LINKEDIN_LI_AT", "").strip()
    if li_at:
        await context.add_cookies(
            [{"name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/"}]
        )
    try:
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        # A malformed or expired li_at makes LinkedIn bounce between /feed and
        # /login until the browser gives up. The raw ERR_TOO_MANY_REDIRECTS says
        # nothing useful, so name the actual cause.
        if "ERR_TOO_MANY_REDIRECTS" in str(exc):
            raise RuntimeError(
                "LinkedIn redirect loop, which means the li_at cookie is malformed, "
                "expired, or bound to a different browser fingerprint. Prefer "
                "LINKEDIN_PROFILE_DIR with a session created on this machine — see "
                "docs/LINKEDIN.md."
            ) from None
        raise RuntimeError(f"could not reach LinkedIn: {exc}") from None
    url = page.url
    if "/checkpoint" in url or "/challenge" in url:
        raise RuntimeError(
            "LinkedIn issued a checkpoint (2FA/CAPTCHA). This cannot be automated — "
            "run once with USCRAPE_LINKEDIN_HEADLESS=false and a persistent profile "
            "to clear it by hand."
        )
    if "/login" in url or "/authwall" in url:
        if li_at:
            raise RuntimeError(
                "li_at cookie rejected. Per upstream issue #279 LinkedIn invalidates "
                "session cookies moved across OS/fingerprint boundaries — create the "
                "session on the machine that will use it, via a persistent profile."
            )
        raise RuntimeError("not authenticated: set LINKEDIN_LI_AT or use a logged-in profile dir")
    return "cookie" if li_at else "profile"


async def _scrape_employees(page, company_url: str, limit: int) -> list[dict]:
    """v3's CompanyScraper declares an ``employees`` field and never writes to it."""
    people_url = company_url.rstrip("/") + "/people/"
    await page.goto(people_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)

    out: list[dict] = []
    seen: set[str] = set()
    for _ in range(12):
        cards = await page.locator("li.org-people-profile-card__profile-card-spacing").all()
        if not cards:
            cards = await page.locator('[data-view-name="people-card"]').all()
        for card in cards:
            try:
                text = (await card.inner_text(timeout=1500)).strip()
            except Exception:  # noqa: BLE001
                continue
            if not text or text in seen:
                continue
            seen.add(text)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            href = ""
            try:
                href = await card.locator('a[href*="/in/"]').first.get_attribute("href") or ""
            except Exception:  # noqa: BLE001
                pass
            out.append(
                {
                    "name": lines[0] if lines else "",
                    "job_title": lines[1] if len(lines) > 1 else "",
                    "linkedin_url": href.split("?")[0] if href else "",
                }
            )
            if len(out) >= limit:
                return out
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(1800)
        try:
            more = page.locator('button:has-text("Show more results")')
            if await more.count():
                await more.first.click(timeout=2000)
                await page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            pass
    return out


def _model_to_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                continue
    return {
        k: v for k, v in vars(obj).items() if not k.startswith("_") and k not in ("page", "driver")
    }


async def run(args: argparse.Namespace) -> dict:
    try:
        from linkedin_scraper import CompanyScraper, PersonScraper
    except ImportError as exc:
        return {"error": f"linkedin_scraper not installed (need >=3.0): {exc}"}

    patched = _patch_rate_limit_detection()
    async_playwright, undetected = _playwright()

    browser = context = None
    async with async_playwright() as pw:
        try:
            browser, context = await _launch(
                pw,
                headless=os.getenv("USCRAPE_LINKEDIN_HEADLESS", "true").lower() != "false",
                profile_dir=os.getenv("LINKEDIN_PROFILE_DIR") or None,
                proxy=os.getenv("USCRAPE_PROXY_URL") or None,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            auth_mode = await _authenticate(context, page)

            if args.kind == "person":
                payload = _model_to_dict(await PersonScraper(page).scrape(args.url))
            else:
                payload = _model_to_dict(await CompanyScraper(page).scrape(args.url))
                if args.employees:
                    payload["employees"] = await _scrape_employees(
                        page, args.url, args.employee_limit
                    )

            payload["_meta"] = {
                "backend": "joeyism/linkedin_scraper (parsers) + "
                + ("patchright" if undetected else "playwright"),
                "auth": auth_mode,
                "undetected": undetected,
                "rate_limit_patch_applied": patched,
                "kind": args.kind,
                "url": args.url,
            }
            return payload
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--kind", choices=("person", "company"), required=True)
    ap.add_argument("--employees", action="store_true")
    ap.add_argument("--employee-limit", type=int, default=100)
    args = ap.parse_args()

    try:
        payload = asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001
        _emit({"error": f"{type(exc).__name__}: {exc}", "url": args.url, "kind": args.kind})
        return 2
    _emit(payload)
    return 1 if payload.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
