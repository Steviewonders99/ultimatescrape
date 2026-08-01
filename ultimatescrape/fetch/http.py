"""The HTTP fetch tier.

Design notes worth keeping in view, all of them scars from the existing fleet:

* Timeouts use httpx's native cancellation, not ``asyncio.wait_for`` around a
  ``Promise.race`` equivalent. The TS fetchers leaked a live socket and an
  uncancelled timer on every timeout.
* Retries exist. None of the four prior fetchers retried at all — one attempt,
  then ``null``. 429 and 503 get exponential backoff and honour ``Retry-After``.
* Responses are size-capped *while streaming*, so a 400 MB file cannot be pulled
  into memory before anyone checks.
* Content type is checked before parsing. Prior code ran a regex HTML extractor
  over PDFs and JSON.
* Partial results survive a deadline. When the run clock expires mid-batch, what
  completed is returned; the prior pipeline threw away every finished article
  and reported all URLs as failed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import httpx

from ..config import settings
from .extract import Extracted, extract, now_iso
from .politeness import HostGate, RobotsCache, host_of

log = logging.getLogger("uscrape.fetch")

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_HTML_TYPES = ("text/html", "application/xhtml", "application/xml", "text/xml")
_TEXTUAL = _HTML_TYPES + ("text/plain", "application/json", "text/markdown")


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    elapsed_ms: int = 0
    attempts: int = 0
    error: str | None = None
    fetched_at: str = field(default_factory=now_iso)
    doc: Extracted | None = None
    raw: str | None = None

    def as_dict(self, include_raw: bool = False) -> dict:
        d = {
            "url": self.url,
            "ok": self.ok,
            "status": self.status,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
            "error": self.error,
            "fetched_at": self.fetched_at,
            "doc": self.doc.as_dict() if self.doc else None,
        }
        if include_raw:
            d["raw"] = self.raw
        return d


class Fetcher:
    """Polite, retrying, size-capped HTTP fetcher with markdown extraction."""

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        gate: HostGate | None = None,
        respect_robots: bool | None = None,
        proxy: str | None = None,
        keep_raw: bool = False,
    ) -> None:
        self.keep_raw = keep_raw
        self.respect_robots = settings.respect_robots if respect_robots is None else respect_robots
        self._sem = asyncio.Semaphore(concurrency or settings.fetch_concurrency)
        self._gate = gate or HostGate()
        self._robots = RobotsCache()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.fetch_timeout_s, connect=10.0),
            follow_redirects=True,
            proxy=proxy,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                # Deliberately NOT setting Accept-Encoding. httpx advertises
                # exactly the codecs it can decode (brotli and zstd only when
                # those extras are installed). Hardcoding "gzip, deflate, br"
                # makes servers send brotli that httpx then cannot decompress,
                # and the body arrives as binary garbage that still parses as
                # "HTML" — a silent corruption, not an error.
            },
        )

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        result = FetchResult(url=url, ok=False)

        if self.respect_robots:
            allowed, delay = await self._robots.allowed(self._client, url)
            self._gate.set_crawl_delay(host_of(url), delay)
            if not allowed:
                result.error = "blocked by robots.txt"
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                return result

        async with self._sem:
            for attempt in range(1, settings.max_attempts + 1):
                result.attempts = attempt
                try:
                    async with self._gate.slot(url):
                        body, resp = await self._stream(url)
                    result.status = resp.status_code
                    result.final_url = str(resp.url)
                    result.content_type = (resp.headers.get("content-type") or "").split(";")[0]

                    if resp.status_code in _RETRY_STATUS and attempt < settings.max_attempts:
                        raise _Retryable(resp.status_code, resp.headers.get("retry-after"))
                    if resp.status_code >= 400:
                        result.error = f"HTTP {resp.status_code}"
                        break

                    ctype = result.content_type or ""
                    if ctype and not ctype.startswith(_TEXTUAL):
                        result.error = f"non-textual content-type {ctype}"
                        break

                    if self.keep_raw:
                        result.raw = body
                    if ctype.startswith(_HTML_TYPES):
                        result.doc = extract(body, result.final_url or url)
                    else:
                        result.doc = Extracted(
                            url=result.final_url or url,
                            markdown=body[:40_000],
                            text=body[:40_000],
                            word_count=len(body.split()),
                            strategy="raw",
                        )
                    result.ok = True
                    break

                except _Retryable as exc:
                    wait = exc.retry_after or min(2**attempt + random.random(), 30)
                    log.warning("[fetch] %s HTTP %s; retrying in %.1fs", url, exc.status, wait)
                    await asyncio.sleep(wait)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    result.error = f"{type(exc).__name__}: {exc}"
                    if attempt < settings.max_attempts:
                        await asyncio.sleep(min(2**attempt, 20))
                    else:
                        log.warning("[fetch] %s gave up: %s", url, result.error)
                except Exception as exc:  # noqa: BLE001
                    result.error = f"{type(exc).__name__}: {exc}"
                    break

        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _stream(self, url: str) -> tuple[str, httpx.Response]:
        """GET with a hard byte cap enforced during the download."""
        async with self._client.stream("GET", url) as resp:
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > settings.max_response_bytes:
                    log.debug("[fetch] %s exceeded byte cap; truncating", url)
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            encoding = resp.charset_encoding or "utf-8"
            try:
                text = raw.decode(encoding, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return text, resp

    async def fetch_many(
        self,
        urls: Iterable[str],
        *,
        deadline_s: float | None = None,
        on_result=None,
    ) -> list[FetchResult]:
        """Fetch a batch, returning whatever finished when the deadline hits.

        ``on_result`` is awaited per completion, which is how the crawler
        checkpoints incrementally instead of losing everything on a crash.
        """
        todo = list(dict.fromkeys(urls))
        if not todo:
            return []
        tasks = {asyncio.create_task(self.fetch(u)): u for u in todo}
        done_results: list[FetchResult] = []

        pending = set(tasks)
        try:
            while pending:
                timeout = None
                if deadline_s is not None:
                    timeout = max(0.0, deadline_s)
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done and timeout is not None:
                    log.warning(
                        "[fetch] deadline hit with %d URLs outstanding; returning %d completed",
                        len(pending),
                        len(done_results),
                    )
                    break
                for task in done:
                    try:
                        res = task.result()
                    except Exception as exc:  # noqa: BLE001
                        res = FetchResult(url=tasks[task], ok=False, error=str(exc))
                    done_results.append(res)
                    if on_result is not None:
                        await on_result(res)
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        for task in pending:
            done_results.append(
                FetchResult(url=tasks[task], ok=False, error="cancelled: run deadline")
            )
        return done_results

    async def check_urls(self, urls: Sequence[str]) -> dict[str, str]:
        """Liveness pass over URLs an agent claimed exist.

        This is the single highest-value verification step in the whole system:
        the vendor swarm found that LLM-reported URLs are wrong often enough that
        an unvalidated list poisons the output. GET rather than HEAD, because
        WAFs commonly block HEAD.
        """
        async def one(url: str) -> tuple[str, str]:
            try:
                async with self._gate.slot(url):
                    resp = await self._client.get(
                        url, timeout=12.0, headers={"Range": "bytes=0-2048"}
                    )
                if resp.status_code < 300:
                    return url, "ok"
                if resp.status_code < 400:
                    return url, "redirect_ok"
                return url, f"dead_{resp.status_code}"
            except httpx.TimeoutException:
                return url, "dead_timeout"
            except httpx.TransportError:
                return url, "dead_connect"
            except Exception:  # noqa: BLE001
                return url, "invalid"

        pairs = await asyncio.gather(*(one(u) for u in dict.fromkeys(urls)))
        return dict(pairs)


class _Retryable(Exception):
    def __init__(self, status: int, retry_after: str | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        try:
            self.retry_after = float(retry_after) if retry_after else None
        except ValueError:
            self.retry_after = None
