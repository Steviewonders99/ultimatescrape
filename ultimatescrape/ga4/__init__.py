"""GA4 reporting for the market research team — no Google SDK required."""

from .client import GA4Client, GA4Error, ReportResult, resolve_date
from .reports import REPORTS, Recipe, describe, get, run

__all__ = [
    "REPORTS",
    "GA4Client",
    "GA4Error",
    "Recipe",
    "ReportResult",
    "describe",
    "get",
    "resolve_date",
    "run",
]
