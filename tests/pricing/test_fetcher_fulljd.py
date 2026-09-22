import json

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


def test_full_text_does_not_truncate():
    out = _full_text(LONG_HTML)
    assert len(out) > 1000
    assert "<p>" not in out


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
