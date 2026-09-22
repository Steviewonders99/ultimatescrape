import json
import re

from ultimatescrape.jobboards.fetchers import JobBoardClient, Listing, _full_text
from ultimatescrape.jobboards.registry import Access, Platform

LONG_HTML = "<p>" + "The quick brown fox. " * 60 + "</p>"  # ~1300 chars


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _Http:
    def __init__(self, payload):
        self._p = payload

    async def get(self, url):
        return _Resp(self._p)


class _PostHttp:
    """outlier's feed is a POST endpoint, not GET."""

    def __init__(self, payload):
        self._p = payload

    async def post(self, url, **kwargs):
        return _Resp(self._p)


class _HtmlResp:
    """mercor embeds JSON inside an HTML page's __NEXT_DATA__ script tag —
    the adapter regexes resp.text, not resp.json()."""

    status_code = 200

    def __init__(self, html):
        self.text = html


class _HtmlHttp:
    def __init__(self, html):
        self._html = html

    async def get(self, url):
        return _HtmlResp(self._html)


class _PagedPostHttp:
    """micro1's real pagination lives in the URL query string (page=/
    limit=), not the JSON body — this fake must respond differently per
    page (indexed by the ?page= it's asked for) to prove the adapter
    actually reads the URL, and records every call for inspection."""

    def __init__(self, pages):
        self._pages = pages  # list of response payloads, 0-indexed by page-1
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs.get("json")))
        m = re.search(r"page=(\d+)", url)
        idx = (int(m.group(1)) if m else 1) - 1
        payload = self._pages[idx] if idx < len(self._pages) else {"data": []}
        return _Resp(payload)


def test_full_text_does_not_truncate():
    out = _full_text(LONG_HTML)
    assert len(out) > 1000
    assert "<p>" not in out


async def test_mercor_pay_fields_from_real_payload_shape():
    """Observed live 22 Sep 2026: hourlyRate/payRate/rate (the fields the
    old code read) do not exist on any of 374 live job objects. The real
    fields are rateMin/rateMax (374/374 present) plus payRateFrequency
    ("hourly" 332, "per-task" 30, "one-time" 9, "yearly" 3 live). No
    currency field anywhere — USD assumed (Mercor is US-headquartered)."""
    next_data = {"listings": [
        {"listingId": "list_AAA111", "title": "Political Expert",
         "location": "Remote", "description": "d1",
         "rateMin": 150, "rateMax": 250, "payRateFrequency": "hourly",
         "createdAt": "2026-09-21T23:55:06"},
        {"listingId": "list_BBB222", "title": "Per-task rater",
         "location": "Remote", "description": "d2",
         "rateMin": 5, "rateMax": 5, "payRateFrequency": "per-task",
         "createdAt": "2026-09-20T10:00:00"},
    ]}
    html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(next_data) + '</script></body></html>')
    client = JobBoardClient()
    client._http = _HtmlHttp(html)
    p = Platform(key="mercor", company="Mercor", access=Access.CUSTOM_JSON)
    rows = await client._mercor(p)
    assert len(rows) == 2
    hourly, per_task = rows
    assert hourly.pay_min == 150.0 and hourly.pay_max == 250.0
    assert hourly.pay_unit == "hour" and hourly.pay_currency == "USD"
    assert hourly.pay_source == "structured"
    assert hourly.posted_at == "2026-09-21"  # createdAt, newly wired up
    # per-task is stored but never converted to USD/hour by normalize —
    # the unit mapping must preserve that distinction, not force "hour".
    assert per_task.pay_unit == "task"
    assert per_task.pay_source == "structured"


async def test_mercor_missing_id_field_uses_listing_id():
    """Observed live 22 Sep 2026: mercor's __NEXT_DATA__ job objects carry
    NO "id" field at all (375/375 live listings) — job.get("id", "") always
    fell through to "" for every listing, so every mercor row collided on
    (platform="mercor", external_id="") the moment more than one was
    fetched in the same sync — production duplicate-key crash. The stable
    field is "listingId"."""
    next_data = {"props": {"pageProps": {"listings": [
        {"listingId": "list_AAABoMZlDn5LJvVj3XBA3YGz", "title": "Rater A",
         "location": "Remote", "description": "d1"},
        {"listingId": "list_AAABoMYTZMoA9n-S3ttLwqcq", "title": "Rater B",
         "location": "Remote", "description": "d2"},
    ]}}}
    html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(next_data) + '</script></body></html>')
    client = JobBoardClient()
    client._http = _HtmlHttp(html)
    p = Platform(key="mercor", company="Mercor", access=Access.CUSTOM_JSON)
    rows = await client._mercor(p)
    assert len(rows) == 2
    assert rows[0].external_id == "list_AAABoMZlDn5LJvVj3XBA3YGz"
    assert rows[1].external_id == "list_AAABoMYTZMoA9n-S3ttLwqcq"
    assert rows[0].url == "https://work.mercor.com/jobs/list_AAABoMZlDn5LJvVj3XBA3YGz"
    assert all(r.external_id for r in rows)
    assert len({r.external_id for r in rows}) == 2  # no collision


async def test_mercor_falls_back_when_listing_id_genuinely_absent():
    next_data = {"listings": [
        {"title": "No id at all", "location": "Remote",
         "description": "d"},
    ]}
    html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(next_data) + '</script></body></html>')
    client = JobBoardClient()
    client._http = _HtmlHttp(html)
    p = Platform(key="mercor", company="Mercor", access=Access.CUSTOM_JSON)
    rows = await client._mercor(p)
    assert len(rows) == 1
    assert rows[0].external_id  # never empty
    assert rows[0].url == ""  # no listingId, so no URL can be built


async def test_imerit_missing_id_field_uses_job_id():
    """Observed live 22 Sep 2026: iMerit's jobs.json has no "id" field at
    all (36/36 live listings) — the stable field is "job_id" (e.g.
    "GLO-057")."""
    payload = {"jobs": [
        {"job_id": "GLO-057", "title": "Korean Language Expert",
         "location": "Global"},
        {"job_id": "GLO-058", "title": "French Language Expert",
         "location": "Global"},
    ]}
    client = JobBoardClient()
    client._http = _Http(payload)
    p = Platform(key="imerit", company="iMerit", access=Access.CUSTOM_JSON)
    rows = await client._imerit(p)
    assert len(rows) == 2
    assert rows[0].external_id == "GLO-057"
    assert rows[1].external_id == "GLO-058"


async def test_imerit_employment_type_and_posted_at_from_real_shape():
    """Observed live 22 Sep 2026: "job_type" does not exist on any of the
    36 live job objects — the real field is "type" (e.g. "Independent
    Contractor (Scholar)"). No "posted_date" field exists either; the
    only date-shaped field is "expiry_date", which means something
    different (closes, not opens) and was empty on every listing sampled
    — not a substitute, so posted_at stays honestly empty."""
    payload = {"jobs": [
        {"job_id": "GLO-057", "title": "Korean Language Expert",
         "location": "Global", "type": "Independent Contractor (Scholar)",
         "expiry_date": ""},
    ]}
    client = JobBoardClient()
    client._http = _Http(payload)
    p = Platform(key="imerit", company="iMerit", access=Access.CUSTOM_JSON)
    rows = await client._imerit(p)
    assert rows[0].employment_type == "Independent Contractor (Scholar)"
    assert rows[0].posted_at == ""


async def test_micro1_new_contract_paginates_via_query_string():
    """Observed live 22 Sep 2026: the old body-only {"page":..,"limit":..}
    request 400s with "Invalid action! Available actions: get_all_jobs,
    ...". The fix needs {"action": "get_all_jobs"} in the JSON body AND
    page/limit as URL QUERY PARAMS — a body page/limit is silently
    ignored (confirmed live: 7 differently-"paged" body-only calls
    collected only 10 unique job_ids out of 369). The response shape also
    changed entirely: job objects are now job_id/job_name/apply_url/
    date_posted/ideal_hourly_rate (a {"min":..,"max":..} dict) — none of
    the old title/hourly_rate/id/location field names exist."""
    page1 = {"status": True, "total": 2, "data": [{
        "job_id": "0936cd2a-a0a9-4105-802a-a4ec81cbf466",
        "job_name": "In-House Counsel",
        "apply_url": "https://jobs.micro1.ai/post/0936cd2a-...",
        "date_posted": "2026-09-22 14:55:42",
        "ideal_hourly_rate": {"min": 90, "max": 130},
        "ideal_monthly_salary_min": None, "ideal_monthly_salary_max": None,
        "ideal_yearly_compensation": None, "location_type": None,
    }]}
    page2 = {"status": True, "total": 2, "data": []}
    client = JobBoardClient()
    fake = _PagedPostHttp([page1, page2])
    client._http = fake
    p = Platform(key="micro1", company="Micro1", access=Access.CUSTOM_JSON)
    rows = await client._micro1(p)

    assert len(rows) == 1
    r = rows[0]
    assert r.title == "In-House Counsel"
    assert r.external_id == "0936cd2a-a0a9-4105-802a-a4ec81cbf466"
    assert r.pay_min == 90.0 and r.pay_max == 130.0
    assert r.pay_unit == "hour" and r.pay_currency == "USD"
    assert r.pay_source == "structured"
    assert r.posted_at == "2026-09-22"
    assert r.url.startswith("https://jobs.micro1.ai")

    # the contract fix itself: action in the body, page/limit in the URL
    first_url, first_body = fake.calls[0]
    assert first_body == {"action": "get_all_jobs"}
    assert "page=1" in first_url and "limit=" in first_url


async def test_micro1_missing_hourly_rate_is_honest_no_pay():
    page1 = {"status": True, "data": [{
        "job_id": "abc123", "job_name": "No Rate Listed",
        "apply_url": "https://jobs.micro1.ai/post/abc123",
        "date_posted": "2026-09-22 00:00:00",
        "ideal_hourly_rate": {"min": None, "max": None},
        "location_type": None,
    }]}
    page2 = {"data": []}
    client = JobBoardClient()
    client._http = _PagedPostHttp([page1, page2])
    p = Platform(key="micro1", company="Micro1", access=Access.CUSTOM_JSON)
    rows = await client._micro1(p)
    assert len(rows) == 1
    assert rows[0].pay_min is None
    assert rows[0].pay_source == ""


async def test_greenhouse_captures_full_jd():
    payload = {"jobs": [{
        "id": 1, "title": "Rater", "absolute_url": "https://x/1",
        "location": {"name": "Remote"}, "departments": [], "metadata": [],
        "updated_at": "2026-09-01T00:00:00Z", "content": LONG_HTML,
    }]}
    client = JobBoardClient()
    client._http = _Http(payload)
    p = Platform(key="t", company="T", access=Access.ATS, ats="greenhouse", token="t")
    rows = await client._greenhouse(p)
    assert len(rows[0].description_full) > 1000
    assert len(rows[0].description_excerpt) <= 300


async def test_outlier_location_dict_shape_extracts_name():
    """Observed live on 22 Sep 2026: outlier's job-board API wraps location
    as {"name": "Remote - France"} instead of a plain string (8/8 live
    listings that day, one consistent shape). This crashed sync_boards with
    'dict' object has no attribute 'lower' inside
    normalize.country_from_location — the adapter must emit a string."""
    payload = {"jobs": [{
        "id": "abc123", "title": "Rater",
        "location": {"name": "Remote - France"}, "payRate": "25/hr",
    }]}
    client = JobBoardClient()
    client._http = _PostHttp(payload)
    p = Platform(key="outlier", company="Outlier (Scale AI)", access=Access.CUSTOM_JSON)
    rows = await client._outlier(p)
    assert len(rows) == 1
    assert rows[0].location == "Remote - France"
    assert isinstance(rows[0].location, str)


async def test_outlier_location_missing_name_falls_back_to_remote():
    payload = {"jobs": [{"id": "1", "title": "Rater", "location": {}}]}
    client = JobBoardClient()
    client._http = _PostHttp(payload)
    p = Platform(key="outlier", company="Outlier (Scale AI)", access=Access.CUSTOM_JSON)
    rows = await client._outlier(p)
    assert rows[0].location == "remote"
    assert isinstance(rows[0].location, str)


async def test_outlier_pay_url_and_jd_from_real_payload_shape():
    """Observed live 22 Sep 2026, 8/8 listings: payRate/hourlyRate/rate,
    "url" and "description" do not exist on any real outlier job object
    (this was found by checking structured-pay coverage after the
    mercor/micro1/imerit pay fixes — outlier read 0/8 structured,
    silently, via the exact same wrong-field-name pattern). The real
    fields are "maxHourlyRateUsd" (a plain int, explicitly USD — a
    ceiling, not a floor), "absolute_url", and "content" (the JD HTML)."""
    payload = {"jobs": [{
        "id": "4729394005", "title": "Android AI Evaluator - French",
        "location": {"name": "Remote - France"},
        "absolute_url": "https://app.outlier.ai/en/expert/job/4729394005",
        "maxHourlyRateUsd": 25,
        "content": "<p>Earn up to USD $25.00 per hour for core project work</p>",
    }]}
    client = JobBoardClient()
    client._http = _PostHttp(payload)
    p = Platform(key="outlier", company="Outlier (Scale AI)", access=Access.CUSTOM_JSON)
    rows = await client._outlier(p)
    assert len(rows) == 1
    r = rows[0]
    assert r.pay_min == 25.0
    assert r.pay_unit == "hour" and r.pay_currency == "USD"
    assert r.pay_source == "structured"
    assert r.url == "https://app.outlier.ai/en/expert/job/4729394005"
    assert "core project work" in r.description_full
    assert "core project work" in r.description_excerpt


async def test_outlier_location_plain_string_still_works():
    payload = {"jobs": [{"id": "2", "title": "Rater", "location": "Remote - US"}]}
    client = JobBoardClient()
    client._http = _PostHttp(payload)
    p = Platform(key="outlier", company="Outlier (Scale AI)", access=Access.CUSTOM_JSON)
    rows = await client._outlier(p)
    assert rows[0].location == "Remote - US"


async def test_fetch_all_reports_errors_per_platform(monkeypatch):
    client = JobBoardClient()

    async def boom(key):
        if key == "bad":
            raise RuntimeError("kaput")
        return [Listing(platform="ok", company="OK", title="x")]

    monkeypatch.setattr(client, "fetch", boom)
    ok, errors = await client.fetch_all(["ok", "bad"])
    assert list(ok) == ["ok"] and "bad" in errors and "kaput" in errors["bad"]
