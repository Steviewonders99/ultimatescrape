"""Tests for the LinkedIn parser tier.

The tier drives our own Patchright browser and hands the page to
joeyism/linkedin_scraper's parsers. These tests pin the runtime patch for
upstream issue #277, which is the difference between the backend working and
aborting on the first page it loads.

Everything here skips cleanly when the optional extra is not installed:
``uv pip install -e ".[linkedin]"``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

WORKER = (
    Path(__file__).resolve().parents[1]
    / "ultimatescrape"
    / "channels"
    / "_linkedin_parser_worker.py"
)

has_scraper = importlib.util.find_spec("linkedin_scraper") is not None
requires_scraper = pytest.mark.skipif(
    not has_scraper, reason='needs the optional extra: uv pip install -e ".[linkedin]"'
)


def load_worker():
    spec = importlib.util.spec_from_file_location("_lw", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_imports_without_the_optional_dependency():
    # The worker is spawned as a subprocess and must always be importable, so a
    # missing optional dependency surfaces as a clean JSON error rather than a
    # crash the parent cannot parse.
    assert load_worker() is not None


def test_the_over_broad_phrase_is_excluded():
    worker = load_worker()
    phrases = worker._RATE_LIMIT_PHRASES
    # "try again later" is the substring LinkedIn embeds in the React payload of
    # ordinary pages. Upstream matches it, which is the whole of issue #277.
    assert not any("try again later" in p for p in phrases)
    # The genuinely diagnostic phrases must still be there.
    assert "too many requests" in phrases
    assert "rate limit" in phrases


@requires_scraper
def test_patch_rebinds_every_real_call_site():
    worker = load_worker()
    patched = worker._patch_rate_limit_detection()
    # Callers do `from .utils import detect_rate_limit`, binding the name into
    # their own module at import time — patching utils alone reaches nobody.
    assert "linkedin_scraper.core.utils" in patched
    assert "linkedin_scraper.core.auth" in patched
    assert "linkedin_scraper.scrapers.base" in patched


@requires_scraper
def test_patch_preserves_the_raise_contract():
    import inspect

    from linkedin_scraper.core import utils

    load_worker()._patch_rate_limit_detection()
    fn = utils.detect_rate_limit
    assert inspect.iscoroutinefunction(fn)
    # Upstream returns None and RAISES RateLimitError. A replacement that
    # returned a bool would silently disable detection altogether — including
    # the legitimate checkpoint and CAPTCHA checks.
    assert inspect.signature(fn).return_annotation in (None, "None", type(None))


@requires_scraper
def test_scraper_api_is_the_v3_shape_we_target():
    import linkedin_scraper as ls

    # v3 takes a Playwright Page. The v2 API most tutorials describe
    # (actions.login, Person(url, driver=...)) does not exist here, and code
    # written against it will not import.
    assert hasattr(ls, "PersonScraper")
    assert hasattr(ls, "CompanyScraper")
    assert not hasattr(ls, "actions")

    import inspect

    params = inspect.signature(ls.PersonScraper.__init__).parameters
    assert "page" in params
    assert inspect.iscoroutinefunction(ls.PersonScraper.scrape)


@requires_scraper
def test_company_employees_is_declared_but_never_populated_upstream():
    import inspect

    from linkedin_scraper.scrapers.company import CompanyScraper

    # The model declares `employees` and nothing in v3 writes to it — there is no
    # /people/ navigation in the package at all. Our worker scrapes it itself, so
    # if upstream ever starts populating it this test tells us we can stop.
    source = inspect.getsource(CompanyScraper)
    assert "/people" not in source
