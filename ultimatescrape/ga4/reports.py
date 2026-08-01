"""Named GA4 reports for the market research team.

Each recipe is a dimension/metric set someone has already thought about, so the
team picks a report name instead of guessing API field names. All of them stay
under GA4's nine-dimension ceiling and sort by the metric that makes the report
readable.

Add your own by appending to :data:`REPORTS` — a recipe is just data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Recipe:
    key: str
    title: str
    question: str
    dimensions: tuple[str, ...]
    metrics: tuple[str, ...]
    order_by: str
    notes: str = ""
    realtime: bool = False


ENGAGEMENT_METRICS = (
    "activeUsers",
    "sessions",
    "engagedSessions",
    "engagementRate",
    "averageSessionDuration",
)

REPORTS: dict[str, Recipe] = {
    "geo": Recipe(
        key="geo",
        title="Where engagement comes from — country and city",
        question="Which countries and cities give us the most engaged users?",
        dimensions=("country", "city"),
        metrics=(*ENGAGEMENT_METRICS, "newUsers"),
        order_by="engagedSessions",
        notes=(
            "Sorted by engaged sessions rather than raw users on purpose — raw user "
            "counts rank by population and bot traffic, engagement ranks by interest."
        ),
    ),
    "country": Recipe(
        key="country",
        title="Engagement by country",
        question="Which countries perform best overall?",
        dimensions=("country",),
        metrics=(*ENGAGEMENT_METRICS, "newUsers", "screenPageViews"),
        order_by="engagedSessions",
    ),
    "devices": Recipe(
        key="devices",
        title="Device and operating system mix",
        question="What are people using to reach us, and does it change engagement?",
        dimensions=("deviceCategory", "operatingSystem", "browser"),
        metrics=(*ENGAGEMENT_METRICS, "screenPageViews"),
        order_by="sessions",
        notes=(
            "GA4 reports unknown values as the literal string '(not set)', most often "
            "for mobileDeviceModel on desktop. Those are real rows, not errors."
        ),
    ),
    "os-detail": Recipe(
        key="os-detail",
        title="Operating system versions and device models",
        question="Which OS versions and handsets do our users actually run?",
        dimensions=("operatingSystem", "operatingSystemVersion", "mobileDeviceModel", "deviceCategory"),
        metrics=("activeUsers", "sessions", "engagementRate"),
        order_by="activeUsers",
    ),
    "device-by-country": Recipe(
        key="device-by-country",
        title="Device mix by country",
        question="Does device preference differ by market?",
        dimensions=("country", "deviceCategory", "operatingSystem"),
        metrics=("activeUsers", "sessions", "engagedSessions", "engagementRate"),
        order_by="activeUsers",
        notes="The report that decides whether a market needs a mobile-first landing page.",
    ),
    "behaviour": Recipe(
        key="behaviour",
        title="Page behaviour",
        question="Which pages hold attention and which lose it?",
        dimensions=("pagePath", "pageTitle"),
        metrics=("screenPageViews", "activeUsers", "userEngagementDuration", "engagementRate", "bounceRate"),
        order_by="screenPageViews",
    ),
    "landing": Recipe(
        key="landing",
        title="Landing page performance",
        question="Where do sessions start, and do those starts engage?",
        dimensions=("landingPage", "sessionDefaultChannelGroup"),
        metrics=("sessions", "engagedSessions", "engagementRate", "bounceRate", "conversions"),
        order_by="sessions",
    ),
    "acquisition": Recipe(
        key="acquisition",
        title="Acquisition — source, medium, campaign",
        question="What brings people here, and which sources bring engaged people?",
        dimensions=("sessionSource", "sessionMedium", "sessionCampaignName", "sessionDefaultChannelGroup"),
        metrics=("sessions", "engagedSessions", "engagementRate", "conversions", "newUsers"),
        order_by="sessions",
    ),
    "events": Recipe(
        key="events",
        title="Event volume",
        question="What are people actually doing on the site?",
        dimensions=("eventName",),
        metrics=("eventCount", "activeUsers"),
        order_by="eventCount",
    ),
    "events-by-geo": Recipe(
        key="events-by-geo",
        title="Events by country",
        question="Which behaviours differ by market?",
        dimensions=("country", "eventName"),
        metrics=("eventCount", "activeUsers"),
        order_by="eventCount",
    ),
    "language": Recipe(
        key="language",
        title="Language and locale",
        question="What languages do our users browse in?",
        dimensions=("language", "country"),
        metrics=("activeUsers", "sessions", "engagementRate"),
        order_by="activeUsers",
        notes="Pairs with the census language tables for localisation decisions.",
    ),
    "trend": Recipe(
        key="trend",
        title="Daily trend",
        question="How is engagement moving over time?",
        dimensions=("date",),
        metrics=(*ENGAGEMENT_METRICS, "newUsers"),
        order_by="date",
    ),
    "trend-by-country": Recipe(
        key="trend-by-country",
        title="Daily trend by country",
        question="Which markets are growing and which are fading?",
        dimensions=("date", "country"),
        metrics=("activeUsers", "sessions", "engagedSessions"),
        order_by="date",
    ),
    "realtime": Recipe(
        key="realtime",
        title="Right now",
        question="Who is on the site at this moment?",
        dimensions=("country", "deviceCategory", "unifiedScreenName"),
        metrics=("activeUsers",),
        order_by="activeUsers",
        realtime=True,
    ),
}


def get(key: str) -> Recipe:
    if key not in REPORTS:
        raise KeyError(
            f"unknown report {key!r}. Available: {', '.join(sorted(REPORTS))}"
        )
    return REPORTS[key]


def describe() -> list[dict]:
    return [
        {
            "report": r.key,
            "question": r.question,
            "dimensions": ", ".join(r.dimensions),
            "metrics": ", ".join(r.metrics),
        }
        for r in REPORTS.values()
    ]


async def run(
    client,
    report: str,
    *,
    days: int | None = None,
    start_date: str | None = None,
    end_date: str = "today",
    limit: int = 5000,
    property_id: str | None = None,
):
    """Execute a named recipe. Returns the client's ReportResult."""
    recipe = get(report)
    if recipe.realtime:
        return await client.run_realtime(
            recipe.dimensions, recipe.metrics, limit=min(limit, 1000), property_id=property_id
        )
    window = start_date or f"{days or 28}d"
    return await client.run_report(
        recipe.dimensions,
        recipe.metrics,
        start_date=window,
        end_date=end_date,
        limit=limit,
        order_by_metric=None if recipe.order_by == "date" else recipe.order_by,
        descending=recipe.order_by != "date",
        property_id=property_id,
    )
