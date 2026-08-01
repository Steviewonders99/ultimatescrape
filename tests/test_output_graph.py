"""Tests for output formats, the knowledge graph, job-board parsing, and GA4 dates.

No network. These cover the places where a silent bug produces a plausible but
wrong artifact — a spreadsheet with a wrong rate, a graph that merges two
different companies, a report window off by a day.
"""

from __future__ import annotations

import pytest

from ultimatescrape.ga4.client import GA4Error, resolve_date
from ultimatescrape.graph.store import KnowledgeGraph, canonical_key
from ultimatescrape.jobboards.fetchers import numeric_pay, parse_pay
from ultimatescrape.output import dataset_from_rows, render

ROWS = [
    {"name": "Acme", "country": "Germany", "value": 10, "url": "https://a.example",
     "_verdict": "supported", "_corroborations": 3},
    {"name": "Globex", "country": "Japan", "value": 20, "url": "https://b.example",
     "_verdict": "refuted", "_corroborations": 1},
]


# ── output formats ────────────────────────────────────────────────────────────


def test_every_format_renders():
    data = dataset_from_rows("Test", ROWS, summary={"rows": 2}, narrative="Some prose.")
    artifacts = render(
        data, ["markdown", "json", "jsonl", "csv", "html", "mermaid", "xlsx"], basename="t"
    )
    produced = {a.format for a in artifacts}
    assert produced == {"markdown", "json", "jsonl", "csv", "html", "mermaid", "xlsx"}
    assert all(a.content for a in artifacts)


def test_csv_carries_a_bom_for_excel():
    data = dataset_from_rows("Accents café Ñandú", ROWS)
    csv_artifact = next(a for a in render(data, ["csv"], basename="t") if a.format == "csv")
    # Without the BOM, Excel on Windows renders accented characters as mojibake.
    assert csv_artifact.content.startswith(b"\xef\xbb\xbf")


def test_xlsx_is_a_real_workbook():
    data = dataset_from_rows("Test", ROWS)
    xlsx = next(a for a in render(data, ["xlsx"], basename="t") if a.format == "xlsx")
    assert xlsx.content.startswith(b"PK")  # zip magic


def test_provenance_columns_sort_last_and_are_optional():

    data = dataset_from_rows("Test", ROWS)
    columns = data.effective_columns()
    assert columns[-1].startswith("_")
    assert not columns[0].startswith("_")
    assert all(not c.startswith("_") for c in data.effective_columns(include_provenance=False))


def test_unknown_format_is_reported_not_raised():
    data = dataset_from_rows("Test", ROWS)
    artifacts = render(data, ["markdown", "nonsense"], basename="t")
    # One bad format must not cost you the others.
    assert [a.format for a in artifacts] == ["markdown"]
    assert any("nonsense" in e for e in data.meta["render_errors"])


def test_config_rejects_an_invalid_format_name():
    from ultimatescrape.projconfig import _normalize_formats

    with pytest.raises(ValueError, match="unknown output format"):
        _normalize_formats(["markdown", "pdf"])


# ── knowledge graph ───────────────────────────────────────────────────────────


def test_canonical_key_collapses_corporate_suffixes():
    assert canonical_key("Scale AI, Inc.", "company") == canonical_key("scale ai", "company")
    assert canonical_key("Café Ñandú", "company") == canonical_key("Cafe Nandu", "company")


def test_canonical_key_separates_types():
    # A company called Handshake and a product called Handshake are different
    # things; merging them silently corrupts every downstream count.
    assert canonical_key("Handshake", "company") != canonical_key("Handshake", "product")


def test_entity_merge_never_overwrites_with_empty(tmp_path):
    with KnowledgeGraph(tmp_path / "g.db") as kg:
        kg.upsert_entity("Acme", "company", {"founded": 1999, "hq": "Berlin"})
        kg.upsert_entity("Acme Inc.", "company", {"founded": None, "industry": "AI"})
        entity = kg.get("Acme", "company")
    assert entity["attrs"]["founded"] == 1999      # not clobbered by the later None
    assert entity["attrs"]["industry"] == "AI"     # new field merged in
    assert entity["mentions"] == 2


def test_relations_reinforce_rather_than_duplicate(tmp_path):
    with KnowledgeGraph(tmp_path / "g.db") as kg:
        for _ in range(3):
            kg.add_relation("Acme", "competes_with", "Globex", run_id="r1")
        edges = kg.edges()
        stats = kg.stats()
    assert len(edges) == 1
    assert edges[0]["weight"] == 3     # weight reads as corroboration count
    assert stats["observations"] == 3  # provenance is append-only


def test_graph_records_provenance(tmp_path):
    with KnowledgeGraph(tmp_path / "g.db") as kg:
        kg.add_relation(
            "Acme", "acquired", "Globex",
            run_id="run-1", agent="a::b", source_url="https://x.example",
            snippet="Acme acquired Globex in 2025.",
        )
        prov = kg.provenance("Acme", "thing")
    assert prov and prov[0]["source_url"] == "https://x.example"
    assert prov[0]["run_id"] == "run-1"


def test_graph_search_finds_by_alias(tmp_path):
    with KnowledgeGraph(tmp_path / "g.db") as kg:
        kg.upsert_entity("International Business Machines", "company", aliases=["IBM"])
        assert kg.search("IBM")


def test_neighbors_walks_multiple_hops(tmp_path):
    with KnowledgeGraph(tmp_path / "g.db") as kg:
        kg.add_relation("A", "knows", "B")
        kg.add_relation("B", "knows", "C")
        assert len(kg.neighbors("A", depth=1)) == 1
        assert len(kg.neighbors("A", depth=2)) == 2


# ── job-board pay parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "low", "high", "unit"),
    [
        ("$110K – $130K", 110_000, 130_000, "year"),
        ("$256K – $320K • Offers Equity", 256_000, 320_000, "year"),
        ("$15/hr", 15, None, "hour"),
        ("€9.50 per hour", 9.5, None, "hour"),
        ("$25 per task", 25, None, "task"),
        ("$1.2M annually", 1_200_000, None, "year"),
    ],
)
def test_pay_parsing(text, low, high, unit):
    parsed = parse_pay(text)
    assert parsed["pay_min"] == low
    assert parsed["pay_max"] == high
    assert parsed["pay_unit"] == unit


def test_pay_parsing_refuses_ambiguous_bare_numbers():
    # "version 3" or "team of 40" must never become a salary in a pricing
    # comparison. Without a unit, a small number is refused outright.
    assert parse_pay("supports version 3 of the spec") == {}
    assert parse_pay("a team of 40 annotators") == {}


def test_numeric_pay_requires_caller_to_supply_context():
    # iMerit publishes a bare "16" with the currency implied. parse_pay must not
    # invent a currency, so the caller states it explicitly.
    assert numeric_pay("16", currency="local")["pay_currency"] == "local"
    assert numeric_pay("16", currency="local")["pay_unit"] == "hour"
    assert numeric_pay("") == {}
    assert numeric_pay("0") == {}


def test_platform_tokens_are_recorded_not_guessed():
    from ultimatescrape.jobboards import get

    # These are the ones nobody would guess from the company name.
    assert get("invisible").token == "agency"
    assert get("sama").token == "samainc"
    assert get("surge").token == "surge-ai"


# ── GA4 date handling ─────────────────────────────────────────────────────────


def test_ga4_relative_dates():
    from datetime import UTC, datetime, timedelta

    assert resolve_date("today") == "today"
    assert resolve_date("2026-01-15") == "2026-01-15"
    # "7dAgo" is one day, a week back; "7d" is a seven-day window. Easy to swap.
    assert resolve_date("7dAgo") == "7daysAgo"
    expected = (datetime.now(UTC).date() - timedelta(days=6)).isoformat()
    assert resolve_date("7d") == expected


def test_ga4_rejects_unreadable_dates():
    with pytest.raises(GA4Error, match="could not read date"):
        resolve_date("last tuesday")


async def test_ga4_refuses_more_than_nine_dimensions():
    from ultimatescrape.ga4 import GA4Client

    client = GA4Client(property_id="1")
    try:
        with pytest.raises(GA4Error, match="at most 9 dimensions"):
            await client.run_report([f"d{i}" for i in range(10)], ["activeUsers"])
    finally:
        await client.aclose()


def test_every_ga4_recipe_is_within_the_dimension_limit():
    from ultimatescrape.ga4 import REPORTS
    from ultimatescrape.ga4.client import MAX_DIMENSIONS

    for recipe in REPORTS.values():
        assert len(recipe.dimensions) <= MAX_DIMENSIONS, recipe.key
        assert recipe.metrics, recipe.key
