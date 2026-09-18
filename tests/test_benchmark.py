"""Deterministic competitor benchmark extraction — no network calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ultimatescrape.benchmark import extract_page_signals, resolve_profiles
from ultimatescrape.channels.linkedin import LinkedInChannel
from ultimatescrape.fetch.browser import BrowserFetcher
from ultimatescrape.fetch.extract import Extracted
from ultimatescrape.fetch.http import FetchResult
from ultimatescrape.llm.client import KimiClient, LLMError


def test_profile_aliases_resolve_and_dedupe():
    profiles = resolve_profiles(
        ["CrowdGen", "Appen", "iMerit Scholars", "Surge AI"],
        include_oneforma=True,
    )
    assert [profile.key for profile in profiles] == ["oneforma", "appen", "imerit", "surge"]


def test_unknown_profile_fails_with_supported_values():
    with pytest.raises(ValueError, match="supported: appen, imerit, dataforce, surge, mercor"):
        resolve_profiles(["not-a-company"])


def test_page_signal_extraction_counts_public_form_friction():
    raw = """<html><body><main>
      <script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[{"@type":"Organization"},{"@type":["WebPage","FAQPage"]}]}
      </script>
      <h1>Put your expertise to work</h1>
      <a href="/jobs">Explore jobs</a>
      <form action="/register">
        <input type="email" name="email" required>
        <input type="password" name="password" required>
        <select name="country" required><option>Canada</option></select>
        <button type="submit">Create account</button>
      </form>
    </main></body></html>"""
    result = FetchResult(
        url="https://example.com/",
        final_url="https://example.com/",
        ok=True,
        status=200,
        raw=raw,
        doc=Extracted(
            url="https://example.com/",
            markdown="# Put your expertise to work\n\n[Explore jobs](https://example.com/jobs)",
            word_count=8,
            links=["https://example.com/jobs"],
        ),
    )
    signals = extract_page_signals(result)
    assert signals["headings"] == ["Put your expertise to work"]
    assert signals["forms"] == 1
    assert signals["required_fields"] == 3
    assert "Create account" in signals["ctas"]
    assert signals["funnel_links"] == ["https://example.com/jobs"]
    assert signals["schema_types"] == ["Organization", "WebPage", "FAQPage"]


def test_browser_result_keeps_clean_html_only_when_requested():
    res = SimpleNamespace(
        success=True,
        status_code=200,
        url="https://example.com/start",
        redirected_url="https://example.com/final",
        markdown=SimpleNamespace(fit_markdown="# Page", raw_markdown="# Page"),
        metadata={"title": "Page"},
        links={"internal": [], "external": []},
        cleaned_html="<main><h1>Page</h1></main>",
    )
    kept = BrowserFetcher._to_result("https://example.com/start", res, keep_raw=True)
    dropped = BrowserFetcher._to_result("https://example.com/start", res)
    assert kept.url == "https://example.com/start"
    assert kept.final_url == "https://example.com/final"
    assert kept.raw == res.cleaned_html
    assert dropped.raw is None


async def test_browser_batch_maps_completion_order_back_to_request_order():
    def result(url: str, title: str):
        return SimpleNamespace(
            success=True,
            status_code=200,
            url=url,
            redirected_url=None,
            markdown=SimpleNamespace(fit_markdown=f"# {title}", raw_markdown=f"# {title}"),
            metadata={"title": title},
            links={"internal": [], "external": []},
            cleaned_html=f"<main><h1>{title}</h1></main>",
        )

    class FakeCrawler:
        async def arun_many(self, **_kwargs):
            return [
                result("https://example.com/slow", "Slow"),
                result("https://example.com/fast", "Fast"),
            ]

    fetcher = object.__new__(BrowserFetcher)
    fetcher.options = SimpleNamespace(keep_raw=False)
    fetcher._crawler = FakeCrawler()
    fetcher._run_config = lambda: None
    rows = await fetcher.fetch_many(
        ["https://example.com/fast", "https://example.com/slow"],
        per_host_delay=(0.0, 0.0),
    )
    assert [row.doc.title for row in rows] == ["Fast", "Slow"]


async def test_strict_credit_check_stops_bad_auth_before_fanout():
    client = KimiClient(api_key="invalid")

    class Response:
        status_code = 401

    async def fake_get(*_args, **_kwargs):
        return Response()

    client._client.get = fake_get
    try:
        with pytest.raises(LLMError, match="authentication failed"):
            await client.check_credits(strict_auth=True)
    finally:
        await client.aclose()


async def test_linkedin_jina_rejects_404_wrappers_but_keeps_real_public_pages():
    class Response:
        def __init__(self, text: str):
            self.text = text

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, body: str):
            self.body = body

        async def get(self, *_args, **_kwargs):
            return Response(self.body)

    channel = object.__new__(LinkedInChannel)
    channel._client = Client(
        "Warning: Target URL returned error 404: Not Found\n\n## Page not found\n"
        + ("missing " * 100)
    )
    assert await channel._via_jina("https://linkedin.com/company/missing", "company") is None

    channel._client = Client(
        "# Example Company\n\n## About us\n\n" + ("real profile content " * 150) + "Join LinkedIn"
    )
    result = await channel._via_jina("https://linkedin.com/company/example", "company")
    assert result is not None
    assert result.ok is True
