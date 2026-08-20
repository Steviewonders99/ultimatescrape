"""Minimum wage layer tests — the offline parts.

These pin the conversions and classifications that would silently produce a
wrong-but-plausible benchmark: a bad unit conversion looks exactly like a real
rate, and a mis-resolved locale benchmarks the wrong country.
"""

from __future__ import annotations

import json

from ultimatescrape.sources.minwage import (
    HOURS_PER_MONTH,
    NO_STATUTORY,
    MinWageClient,
    _LOCAL_PATH,
    local_rates,
    to_iso3,
)


def test_to_iso3_accepts_all_shapes():
    assert to_iso3("JPN") == "JPN"
    assert to_iso3("jp") == "JPN"
    assert to_iso3("ja-JP") == "JPN"
    assert to_iso3("zh_CN") == "CHN"
    assert to_iso3("en-GB") == "GBR"
    # Unknown codes pass through rather than guessing.
    assert to_iso3("XYZ") == "XYZ"


def test_hours_convention_matches_ilo():
    # ILO's own JPN 2024 row: hourly 1,055 JPY published as 182,726 monthly.
    assert round(1055 * HOURS_PER_MONTH) == 182726


def test_benchmark_math():
    rows = [{"iso3": "JPN", "hourly_usd": 7.5}]
    out = MinWageClient.benchmark(15.0, rows, minutes=30)
    assert out[0]["covers_minutes"] == 120
    assert out[0]["implied_hourly_usd"] == 30.0
    assert out[0]["vs_min_wage"] == 4.0
    # No hourly rate -> no derived fields, not a crash and not a zero.
    bare = MinWageClient.benchmark(15.0, [{"iso3": "ITA", "statutory": False}])
    assert "covers_minutes" not in bare[0]


def test_no_statutory_and_local_data_are_consistent():
    # Countries flagged as having no statutory minimum must not also carry
    # curated statutory rows.
    data = json.loads(_LOCAL_PATH.read_text())
    overlap = set(data) & set(NO_STATUTORY)
    assert not overlap, f"contradictory entries: {overlap}"


def test_local_rows_carry_provenance():
    data = json.loads(_LOCAL_PATH.read_text())
    for iso3, rows in data.items():
        if iso3.startswith("_"):
            continue
        for row in rows:
            for field in ("region", "rate", "unit", "currency", "as_of", "source"):
                assert row.get(field) not in (None, ""), f"{iso3}: {row.get('region')} missing {field}"
            assert row["unit"] in ("hour", "day", "month")


def test_local_rates_unknown_country_is_empty():
    assert local_rates("ZZZ") == []
