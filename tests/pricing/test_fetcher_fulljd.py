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


def test_full_text_does_not_truncate():
    out = _full_text(LONG_HTML)
    assert len(out) > 1000
    assert "<p>" not in out


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


async def test_fetch_all_reports_errors_per_platform(monkeypatch):
    client = JobBoardClient()

    async def boom(key):
        if key == "bad":
            raise RuntimeError("kaput")
        return [Listing(platform="ok", company="OK", title="x")]

    monkeypatch.setattr(client, "fetch", boom)
    ok, errors = await client.fetch_all(["ok", "bad"])
    assert list(ok) == ["ok"] and "bad" in errors and "kaput" in errors["bad"]
