"""Swarm mechanics: merge, budget, resume, JSON recovery.

None of these hit the network or an LLM. They cover the logic that decides what
counts as a finding and what a run costs — the parts where a silent bug produces
a confident, wrong report.
"""

from __future__ import annotations

import pytest

from ultimatescrape.llm.budget import BudgetExceeded, Ledger
from ultimatescrape.llm.json_util import JsonRecoveryError, parse_json_loose
from ultimatescrape.store.run import RunStore, safe_key
from ultimatescrape.swarm.merge import dedupe_key, majority_vote, merge_findings, normalize_url
from ultimatescrape.swarm.spec import Dimension, SwarmSpec, Target

# ── JSON recovery ─────────────────────────────────────────────────────────────


def test_parses_fenced_json():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}


def test_parses_json_with_prose_prefix():
    assert parse_json_loose('Here you go:\n{"a": [1, 2]}') == {"a": [1, 2]}


def test_repairs_trailing_commas():
    assert parse_json_loose('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}


def test_unrecoverable_json_raises_rather_than_returning_empty():
    # Returning {} here would read downstream as "the agent found nothing",
    # which is indistinguishable from a genuine empty result.
    with pytest.raises(JsonRecoveryError):
        parse_json_loose("the model refused to answer")
    with pytest.raises(JsonRecoveryError):
        parse_json_loose("")


# ── merge ─────────────────────────────────────────────────────────────────────


def test_url_normalisation_collapses_equivalent_urls():
    variants = [
        "https://example.com/x",
        "http://www.example.com/x/",
        "https://example.com/x?utm_source=news",
    ]
    assert len({normalize_url(u) for u in variants}) == 1


def test_merge_counts_corroboration_and_backfills():
    findings = [
        {"name": "Acme", "url": "https://acme.com", "confidence": "low", "_agent": "a1"},
        {
            "name": "Acme",
            "url": "https://www.acme.com/",
            "confidence": "high",
            "founded": 1999,
            "_agent": "a2",
        },
    ]
    merged = merge_findings(findings, dedupe_fields=("url", "name"))
    assert len(merged) == 1
    record = merged[0]
    # Two independent agents finding the same entity raises confidence — it is
    # signal, not a duplicate to silently drop.
    assert record["_corroborations"] == 2
    assert sorted(record["_sources"]) == ["a1", "a2"]
    assert record["confidence"] == "high"
    assert record["founded"] == 1999


def test_merge_keeps_distinct_entities_apart():
    findings = [
        {"name": "Acme", "url": "https://acme.com"},
        {"name": "Globex", "url": "https://globex.com"},
    ]
    assert len(merge_findings(findings, dedupe_fields=("url", "name"))) == 2


def test_dedupe_falls_back_through_fields():
    assert dedupe_key({"name": "Acme Corp"}, ("url", "name")) == "acmecorp"


def test_majority_vote():
    value, count = majority_vote(["a", "a", "b"])
    assert value == "a" and count == 2
    assert majority_vote([]) == (None, 0)


# ── budget ────────────────────────────────────────────────────────────────────


async def test_ledger_enforces_ceiling_on_the_call_that_crosses_it():
    ledger = Ledger(limit_usd=1.0)
    small = {"prompt_tokens": 100_000, "completion_tokens": 0}  # $0.06 each
    for _ in range(5):
        await ledger.record("moonshotai/kimi-k2.6", small)
    assert ledger.cost_usd < 1.0
    # The overrun is capped at one call's spend, not a whole runaway run.
    with pytest.raises(BudgetExceeded):
        await ledger.record(
            "moonshotai/kimi-k2.6", {"prompt_tokens": 2_000_000, "completion_tokens": 0}
        )


async def test_ledger_tracks_per_model():
    ledger = Ledger(limit_usd=100.0)
    await ledger.record(
        "moonshotai/kimi-k2.6", {"prompt_tokens": 1000, "completion_tokens": 500}
    )
    summary = ledger.summary()
    assert summary["calls"] == 1
    assert summary["prompt_tokens"] == 1000
    assert "moonshotai/kimi-k2.6" in summary["by_model"]
    assert summary["cost_usd"] > 0


# ── spec ──────────────────────────────────────────────────────────────────────


def test_work_matrix_is_the_product_of_targets_and_dimensions():
    spec = SwarmSpec(
        topic="t",
        targets=[Target.of("a"), Target.of("b"), Target.of("c")],
        dimensions=[Dimension("d1", "{label}"), Dimension("d2", "{label}")],
        system_prompt="s",
        output_contract="{}",
    )
    assert len(spec.work_units()) == 6


def test_activation_rules_skip_inapplicable_targets():
    spec = SwarmSpec(
        topic="t",
        targets=[Target("us", "United States"), Target("de", "Germany")],
        dimensions=[
            Dimension("all", "{label}"),
            Dimension("eu_only", "{label}", activates_when=lambda t: t.key == "de"),
        ],
        system_prompt="s",
        output_contract="{}",
    )
    assert len(spec.work_units()) == 3


def test_spec_round_trips_through_the_manifest():
    spec = SwarmSpec(
        topic="t",
        targets=[Target("us", "United States"), Target("de", "Germany")],
        dimensions=[
            Dimension("a", "Ask about {label}", max_tokens=4321),
            Dimension("eu", "EU only {label}", activates_when=lambda t: t.key == "de"),
        ],
        system_prompt="SYS",
        output_contract='{"findings":[]}',
        verifier_votes=3,
    )
    rebuilt = SwarmSpec.from_dict(spec.as_dict())

    # activates_when is a callable and cannot be serialised, so the plan stores
    # its OUTCOME. A resumed run must reproduce the original matrix exactly.
    assert [spec.unit_key(t, d) for t, d in spec.work_units()] == [
        rebuilt.unit_key(t, d) for t, d in rebuilt.work_units()
    ]
    # Prompts must survive, or resume only works for built-in recipes — which is
    # the opposite of the runs that need it.
    assert rebuilt.dimensions[0].prompt == "Ask about {label}"
    assert rebuilt.dimensions[0].max_tokens == 4321
    assert rebuilt.system_prompt == "SYS"
    assert rebuilt.verifier_votes == 3


def test_resume_refuses_a_spec_saved_without_prompts():
    with pytest.raises(ValueError, match="predates prompt serialisation"):
        SwarmSpec.from_dict(
            {"topic": "t", "targets": [{"key": "a", "label": "A"}], "dimensions": [{"key": "d"}]}
        )


def test_dimension_render_reports_missing_context_keys():
    dim = Dimension("d", "Research {label} in {sector}")
    with pytest.raises(KeyError, match="sector"):
        dim.render(Target.of("x"), "topic")


# ── run store ─────────────────────────────────────────────────────────────────


def test_safe_key_avoids_collisions_between_similar_labels():
    # Slugging alone maps both of these to "united-states" and one silently
    # overwrites the other's results on disk.
    assert safe_key("United States") != safe_key("united states")


def test_resume_skips_only_successful_units(tmp_path):
    store = RunStore.create("test run", root=tmp_path)
    store.save_unit("a::x", {"findings": [{"name": "n"}]})
    store.save_unit("b::y", {"error": "timeout"})
    assert store.has_unit("a::x") is True
    # An errored unit is work still to do, not work already done.
    assert store.has_unit("b::y") is False
    assert store.pending(["a::x", "b::y", "c::z"]) == ["b::y", "c::z"]


def test_unit_writes_survive_reload(tmp_path):
    store = RunStore.create("r", root=tmp_path)
    store.save_unit("k", {"findings": [{"name": "x"}]})
    reopened = RunStore.open(store.run_id, root=tmp_path)
    assert reopened.load_unit("k")["findings"][0]["name"] == "x"
